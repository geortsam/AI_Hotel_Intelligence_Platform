"""Data access for `llm_invocations` (Stage 7.7). One write, one expiry, no reads, no user.

The table records who asked, but this repository never learns who that is: it is handed a fully
built `LlmInvocation` and stages it. The caller's identity is bound one layer up, by
`app.services.llm_invocation_log.LlmInvocationLog`, constructed with the authenticated user by
the dependency chain -- the same arrangement `AuditTrail` uses over `AuditRepository`. That keeps
this module outside the short list of identity-aware repositories: it imports no user model and
takes no user, actor or principal argument, which the architecture suite asserts of every
domain repository.

No method reads a record. Stage 7.7 offers no endpoint that lists invocations, and a query
written before its caller would be a query whose scope nobody reviewed. The purge reads only
which hotels hold an expired record -- internal ids, no content.

**Expiry (Issue 4, migration 0017).** A record is kept `llm_invocation_retention_days` after its
`created_at` and then deleted by `purge_expired`, one hotel and one bounded batch at a time. A
retention day is exactly 24 hours, counted by PostgreSQL's clock: the interval is built in hours,
because an interval of days is calendar arithmetic in the session's time zone and would move the
cut-off by an hour across a daylight-saving change. The table's trigger still refuses every
DELETE unless the transaction has declared its retention period, and then refuses any row younger
than that period -- so `purge_expired` declares the period and deletes with the same rule, and a
predicate that drifted from the trigger's would fail loudly rather than delete a live record.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, delete, exists, func, select
from sqlalchemy.orm import Session

from app.models.hotel import Hotel
from app.models.llm_invocation import LlmInvocation

#: The transaction-local setting migration 0017's trigger reads. Set with `is_local => true`, so
#: it ends with the transaction that set it.
RETENTION_DECLARATION = "app.llm_invocation_retention_hours"


def _expired(retention_days: int) -> ColumnElement[bool]:
    """At least `retention_days * 24` hours old, by the database's clock -- the trigger's rule."""
    return LlmInvocation.created_at <= func.now() - func.make_interval(
        0, 0, 0, 0, retention_days * 24
    )


class LlmInvocationRepository:
    """Appends invocation rows and deletes expired ones. Never updates -- the table refuses it."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, invocation: LlmInvocation) -> LlmInvocation:
        """Stage one row and flush, so the database assigns its public id and timestamp.

        ``flush``, not ``commit``: the copilot service owns the transaction, exactly as every
        writing service in this codebase does.
        """
        self._session.add(invocation)
        self._session.flush()
        return invocation

    def hotels_with_expired(self, retention_days: int) -> Sequence[int]:
        """The internal ids of the hotels holding at least one expired record, in id order.

        One probe of the `(hotel_id, created_at)` index per hotel. Identifiers only: no record
        is read.
        """
        has_expired = exists().where(LlmInvocation.hotel_id == Hotel.id, _expired(retention_days))
        return self._session.scalars(select(Hotel.id).where(has_expired).order_by(Hotel.id)).all()

    def purge_expired(self, hotel_id: int, retention_days: int, *, limit: int) -> int:
        """Delete up to *limit* of one hotel's expired records, oldest first. Returns rows deleted.

        Declares the retention period to the trigger for the rest of this transaction, then
        deletes by the same rule. Does not commit: the caller owns the transaction, and a batch
        is one.
        """
        self._session.execute(
            select(func.set_config(RETENTION_DECLARATION, str(retention_days * 24), True))
        )
        batch: Any = (
            select(LlmInvocation.id)
            .where(LlmInvocation.hotel_id == hotel_id, _expired(retention_days))
            .order_by(LlmInvocation.created_at.asc(), LlmInvocation.id.asc())
            .limit(limit)
            .scalar_subquery()
        )
        result = self._session.execute(
            delete(LlmInvocation).where(
                LlmInvocation.hotel_id == hotel_id, LlmInvocation.id.in_(batch)
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)


__all__ = ["RETENTION_DECLARATION", "LlmInvocationRepository"]
