"""Audit-archive persistence.

Stage 4.5.14. The archival half of the audit trail's lifecycle: copy events older than a cutoff
into ``audit_events_archive``, verify the copy, and count what is left.

**Every operation here happens in the database.** The archival statement is one
``INSERT ... SELECT`` with a ``LIMIT`` -- the eligible events are never fetched into Python,
never iterated, and never filtered there. That is not a micro-optimisation: an audit table is
the one table in this schema that only grows, and a retention job that loaded its backlog into
a list would fail exactly when it was most needed.

**Nothing here deletes anything.** There is no delete method on this repository and none on
:class:`~app.repositories.audit.AuditRepository`, because ``audit_events`` is append-only at
the database level and Stage 4.5.14 was told not to weaken that to implement retention. The
archive is additive; see :mod:`app.services.retention` for the full reasoning.

**Nothing here commits.** The service owns the transaction, as everywhere else in this layer,
which is what lets a batch's copy and its verification succeed or fail together.

**This module knows that users exist**, like ``repositories.audit`` and for the same reason: an
audit event records WHO acted, and the archive denormalises that actor's public id so a row
stays meaningful without a join. It is on the ``IDENTITY_AWARE`` list for that, and the guard
that matters is unchanged -- it compares no role, reads no membership, and raises no error.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent, AuditEventArchive
from app.models.hotel import Hotel
from app.models.user import User

#: The columns copied, in the order the INSERT expects them. Declared once so the SELECT and
#: the INSERT cannot drift apart -- a mismatch there would silently write the right values into
#: the wrong columns, which is the kind of bug an archive would hide for years.
ARCHIVED_COLUMNS = (
    "audit_event_id",
    "public_id",
    "hotel_id",
    "hotel_public_id",
    "actor_user_id",
    "actor_public_id",
    "action",
    "resource_type",
    "resource_reference",
    "request_id",
    "details",
    "occurred_at",
)


class AuditArchiveRepository:
    """Data access for the audit archive. Copies in; never rewrites, never removes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- the archival write ---------------------------------------------------------------

    def archive_batch(self, cutoff: dt.datetime, *, limit: int) -> list[int]:
        """Copy up to *limit* eligible events into the archive; return the ids actually copied.

        One statement. The eligible set is chosen, joined, ordered, limited and inserted by
        PostgreSQL, and the only thing that crosses back into Python is a list of the ids this
        call inserted.

        **``ORDER BY audit_events.id`` is load-bearing for concurrency**, not cosmetic. Two
        workers running at once select overlapping sets; because both insert in the same key
        order they contend on the same rows in the same sequence, so one waits for the other
        rather than deadlocking against it.

        **``ON CONFLICT DO NOTHING`` is the idempotency guarantee**, and it is the database's
        rather than this code's. The primary key is the original event id, so an event that is
        already archived cannot be archived twice -- by a repeat run, by a concurrent worker,
        or by a bug here. ``RETURNING`` then reports only the rows that were really inserted,
        which is what makes the caller's "archived" count true rather than optimistic.

        The ``NOT EXISTS`` filter is what makes repeated runs make PROGRESS. Without it a
        second run would re-select the same already-archived leading rows, insert nothing, and
        never reach the events behind them; the conflict clause alone gives correctness but not
        advancement.
        """
        source = (
            select(
                AuditEvent.id,
                AuditEvent.public_id,
                AuditEvent.hotel_id,
                Hotel.public_id,
                AuditEvent.actor_user_id,
                User.public_id,
                AuditEvent.action,
                AuditEvent.resource_type,
                AuditEvent.resource_reference,
                AuditEvent.request_id,
                AuditEvent.details,
                AuditEvent.occurred_at,
            )
            .select_from(AuditEvent)
            # OUTER joins: a platform-scoped event has no hotel and an unattributed event has
            # no actor. An inner join would silently drop exactly the events this stage exists
            # to preserve.
            .outerjoin(Hotel, Hotel.id == AuditEvent.hotel_id)
            .outerjoin(User, User.id == AuditEvent.actor_user_id)
            .where(*self._pending(cutoff))
            .order_by(AuditEvent.id)
            .limit(limit)
        )

        statement = (
            pg_insert(AuditEventArchive)
            .from_select(list(ARCHIVED_COLUMNS), source)
            .on_conflict_do_nothing(index_elements=["audit_event_id"])
            .returning(AuditEventArchive.audit_event_id)
        )
        return list(self._session.scalars(statement).all())

    # --- verification ---------------------------------------------------------------------

    def count_faithful_copies(self, audit_event_ids: list[int]) -> int:
        """How many of *audit_event_ids* are archived with values matching their source.

        Field by field, joined in the database. This is the verification step: the caller
        compares this against the batch size and rolls the whole transaction back on a
        mismatch, so a partial or corrupted copy never becomes the state of record.

        ``details`` is compared as JSONB, so key order does not make an identical payload look
        different. The nullable columns are compared with ``IS NOT DISTINCT FROM``, because
        ``NULL = NULL`` is unknown in SQL and a plain ``=`` would report every platform-scoped
        event -- the ones with no hotel -- as unfaithful.
        """
        if not audit_event_ids:
            return 0

        statement = (
            select(func.count())
            .select_from(AuditEventArchive)
            .join(AuditEvent, AuditEvent.id == AuditEventArchive.audit_event_id)
            .where(
                AuditEventArchive.audit_event_id.in_(audit_event_ids),
                AuditEventArchive.public_id == AuditEvent.public_id,
                AuditEventArchive.action == AuditEvent.action,
                AuditEventArchive.resource_type == AuditEvent.resource_type,
                AuditEventArchive.resource_reference == AuditEvent.resource_reference,
                AuditEventArchive.occurred_at == AuditEvent.occurred_at,
                AuditEventArchive.hotel_id.is_not_distinct_from(AuditEvent.hotel_id),
                AuditEventArchive.actor_user_id.is_not_distinct_from(AuditEvent.actor_user_id),
                AuditEventArchive.request_id.is_not_distinct_from(AuditEvent.request_id),
                AuditEventArchive.details == AuditEvent.details,
            )
        )
        return int(self._session.scalar(statement) or 0)

    # --- counts, for the operational report -----------------------------------------------

    def count_eligible(self, cutoff: dt.datetime) -> int:
        """Events older than *cutoff*, archived or not."""
        return self._count(select(func.count()).select_from(AuditEvent).where(*self._older(cutoff)))

    def count_pending(self, cutoff: dt.datetime) -> int:
        """Events older than *cutoff* that are not in the archive yet."""
        return self._count(
            select(func.count()).select_from(AuditEvent).where(*self._pending(cutoff))
        )

    def count_archived(self) -> int:
        """Everything in the archive, whatever cutoff put it there."""
        return self._count(select(func.count()).select_from(AuditEventArchive))

    # --- internals ------------------------------------------------------------------------

    def _count(self, statement: Select[tuple[int]]) -> int:
        return int(self._session.scalar(statement) or 0)

    @staticmethod
    def _older(cutoff: dt.datetime) -> tuple[ColumnElement[bool], ...]:
        """Eligibility, expressed once.

        Strictly ``<``. An event occurring exactly ON the cutoff is retained, so the boundary
        belongs to the active window rather than to the archive -- and the rule is stated in
        one place, so the count and the copy cannot disagree about which events they mean.
        """
        return (AuditEvent.occurred_at < cutoff,)

    @classmethod
    def _pending(cls, cutoff: dt.datetime) -> tuple[ColumnElement[bool], ...]:
        """Eligible AND not yet archived. The same predicate the copy uses."""
        already = (
            select(AuditEventArchive.audit_event_id)
            .where(AuditEventArchive.audit_event_id == AuditEvent.id)
            .exists()
        )
        return (*cls._older(cutoff), ~already)


__all__ = ["ARCHIVED_COLUMNS", "AuditArchiveRepository"]
