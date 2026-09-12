"""Audit-trail persistence.

Stage 4.5.12. Two halves, and they are deliberately asymmetric:

* **one write**, :meth:`AuditRepository.add` -- ``session.add`` and ``flush``, exactly as
  every other repository writes. It does not commit, so the audit row lives or dies with the
  business transaction that staged it. That is the whole of the transactional-integrity
  guarantee, and it is one line rather than a mechanism.
* **reads**, every one of which states its scope, because an audit row is the most sensitive
  thing this schema holds and a query that could be asked without a scope is a query that will
  eventually be asked without one.

  Stage 4.5.13 added the second scope. There are now exactly two, both built here and neither
  reachable from a caller: ``hotel_id = :hotel_id`` for one property's history, and
  ``hotel_id IS NULL`` for the events that belong to no property. The scope is a required
  positional argument to :meth:`_count` and :meth:`_page`, so it cannot be omitted, defaulted
  or reordered away -- an unscoped read of this table is not expressible.

**There is no update method and no delete method.** Not because they are unused -- because
they must not exist. Migration 0007 installs a trigger that refuses UPDATE and DELETE on this
table anyway; the absence here is the same rule stated at the layer a programmer reads first.

**This repository knows that users exist**, which almost none of the others may. It is on the
``IDENTITY_AWARE`` list in the architecture audit for the same reason
``app.repositories.membership`` is: recording and reporting WHO acted is not an incidental
capability it grew, it is the table's entire purpose. The guard that matters -- that nothing
here decides whether a caller MAY act -- still holds: this module compares no role, reads no
membership and raises no error.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.user import User

#: The platform scope: events that belong to no property (Stage 4.5.13).
#:
#: ``hotel_id IS NULL`` is the whole definition, and it is a database predicate rather than a
#: Python filter -- a platform read must never load hotel-scoped rows and discard them, both
#: because that is unbounded work and because "loaded then discarded" is one refactor away
#: from "loaded then returned".
#:
#: ``.is_(None)`` rather than ``== None``: the two render the same SQL, and only one of them
#: survives a linter that is right to be suspicious of the other.
PLATFORM_SCOPE: ColumnElement[bool] = AuditEvent.hotel_id.is_(None)


def hotel_scope(hotel_id: int) -> ColumnElement[bool]:
    """The tenant scope: one property's own history."""
    return AuditEvent.hotel_id == hotel_id


class AuditRepository:
    """Data access for the audit trail. Appends; never rewrites."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- the one write ------------------------------------------------------------------

    def add(self, event: AuditEvent) -> AuditEvent:
        """Stage one event and flush, so the database assigns its key and its timestamp.

        ``flush`` rather than ``commit``, as everywhere else in this layer -- and here the
        distinction carries the stage's central requirement. The row is written on the
        business transaction's connection, so it becomes visible only when that transaction
        commits, and it disappears with it on rollback. A mutation that failed therefore
        cannot leave an event claiming it succeeded, and no separate connection, autonomous
        transaction or after-commit hook is involved.

        **No ``refresh``**, unlike the other repositories in this layer, and the difference is
        measured rather than assumed: SQLAlchemy already returns ``public_id`` and
        ``occurred_at`` in the INSERT's RETURNING clause, so a refresh would be a second round
        trip that fetches values the object already holds. Auditing then costs exactly ONE
        statement per mutation -- which is the difference between a trail that is affordable
        on every write and one that is not. A static test asserts the refresh stays absent.
        """
        self._session.add(event)
        self._session.flush()
        return event

    # --- reads --------------------------------------------------------------------------

    def count_for_hotel(
        self,
        hotel_id: int,
        *,
        action: str | None = None,
        resource_type: str | None = None,
        resource_reference: str | None = None,
        actor_user_id: int | None = None,
        request_id: str | None = None,
        occurred_from: dt.datetime | None = None,
        occurred_to: dt.datetime | None = None,
    ) -> int:
        """How many of this hotel's events match, for the pagination envelope."""
        return self._count(
            hotel_scope(hotel_id),
            action=action,
            resource_type=resource_type,
            resource_reference=resource_reference,
            actor_user_id=actor_user_id,
            request_id=request_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )

    def page_for_hotel(
        self,
        hotel_id: int,
        *,
        limit: int,
        offset: int,
        action: str | None = None,
        resource_type: str | None = None,
        resource_reference: str | None = None,
        actor_user_id: int | None = None,
        request_id: str | None = None,
        occurred_from: dt.datetime | None = None,
        occurred_to: dt.datetime | None = None,
    ) -> list[tuple[AuditEvent, User | None]]:
        """One page of this hotel's events, newest first, each with the account that acted."""
        return self._page(
            hotel_scope(hotel_id),
            limit=limit,
            offset=offset,
            action=action,
            resource_type=resource_type,
            resource_reference=resource_reference,
            actor_user_id=actor_user_id,
            request_id=request_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )

    def count_platform_scoped(
        self,
        *,
        action: str | None = None,
        resource_type: str | None = None,
        actor_user_id: int | None = None,
        request_id: str | None = None,
        occurred_from: dt.datetime | None = None,
        occurred_to: dt.datetime | None = None,
    ) -> int:
        """How many events belonging to no property match (Stage 4.5.13).

        It takes no ``hotel_id`` and has nowhere to put one: the scope is
        :data:`PLATFORM_SCOPE`, fixed here, so this method cannot be pointed at a tenant.
        """
        return self._count(
            PLATFORM_SCOPE,
            action=action,
            resource_type=resource_type,
            # Stage 4.5.22 added this filter to the HOTEL surface only. Named here rather
            # than defaulted, so the platform reader's arguments stay a complete list of
            # what it asks for -- and so this line has to be considered if that changes.
            resource_reference=None,
            actor_user_id=actor_user_id,
            request_id=request_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )

    def page_platform_scoped(
        self,
        *,
        limit: int,
        offset: int,
        action: str | None = None,
        resource_type: str | None = None,
        actor_user_id: int | None = None,
        request_id: str | None = None,
        occurred_from: dt.datetime | None = None,
        occurred_to: dt.datetime | None = None,
    ) -> list[tuple[AuditEvent, User | None]]:
        """One page of the events belonging to no property, newest first (Stage 4.5.13).

        **The existing index answers the scope but not the ordering, and that was measured
        rather than assumed.** Against 5 000 rows PostgreSQL 18.6 plans this as a sequential
        scan with a Sort, while the hotel listing next door gets a clean ordered Index Only
        Scan on the same index:

            hotel_id = 1       ->  Index Only Scan ... Index Cond: (hotel_id = 1)
            hotel_id IS NULL   ->  Sort -> Seq Scan  ... Filter: (hotel_id IS NULL)

        The reason is that ``IS NULL`` is a ``NullTest``, not an equality: the planner never
        concludes that ``hotel_id`` is constant across the scan, so the leading index column
        stays in the path's sort order and ``occurred_at DESC, id DESC`` is not a usable
        prefix. An equality on the same column does pin it, which is why the hotel side is
        served perfectly by the very same index.

        **This stage still adds no index, deliberately.** A partial
        ``(occurred_at DESC, id DESC) WHERE hotel_id IS NULL`` would remove the sort, and it
        needs a migration -- which this stage was told to avoid and to raise rather than
        create quietly. The cost is bounded and small in the meantime: platform-scoped rows are
        password changes and catalogue edits, the rarest events the trail records, and the sort
        is over that subset alone rather than over the whole table. It is in the report as a
        finding, with the plans above, for a decision rather than a silent commit.
        """
        return self._page(
            PLATFORM_SCOPE,
            limit=limit,
            offset=offset,
            action=action,
            resource_type=resource_type,
            resource_reference=None,
            actor_user_id=actor_user_id,
            request_id=request_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )

    def actor_id_for_public_id(self, public_id: uuid.UUID) -> int | None:
        """The internal key of the account named by a public id, or None.

        Exists so the actor FILTER can be expressed in public identifiers: a client names a
        person by the id it already sees in the listing, and the internal key never leaves
        this layer. Returning None for an unknown account is what makes an unknown actor an
        empty page rather than an error -- the filter must not become a way to discover which
        accounts exist.
        """
        return self._session.scalars(
            select(User.id).where(User.public_id == public_id)
        ).one_or_none()

    # --- internals ----------------------------------------------------------------------
    #
    # Exactly one COUNT statement and exactly one page SELECT exist in this project. Both
    # surfaces -- one hotel's history and the platform's -- differ only in the scope predicate
    # they are handed, so there is no second audit query implementation to drift from the
    # first, and a filter cannot come to mean one thing here and another there.

    def _count(
        self,
        scope: ColumnElement[bool],
        *,
        action: str | None,
        resource_type: str | None,
        resource_reference: str | None,
        actor_user_id: int | None,
        request_id: str | None,
        occurred_from: dt.datetime | None,
        occurred_to: dt.datetime | None,
    ) -> int:
        """The total behind a page.

        Filtered by :meth:`_filtered`, the SAME helper the page uses, so a total can never
        contradict the rows it accompanies -- the failure this project has already fixed once
        in the ledger. Counted by the database; nothing is loaded to be measured.
        """
        statement = self._filtered(
            select(func.count()).select_from(AuditEvent),
            scope,
            action=action,
            resource_type=resource_type,
            resource_reference=resource_reference,
            actor_user_id=actor_user_id,
            request_id=request_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        return int(self._session.scalar(statement) or 0)

    def _page(
        self,
        scope: ColumnElement[bool],
        *,
        limit: int,
        offset: int,
        action: str | None,
        resource_type: str | None,
        resource_reference: str | None,
        actor_user_id: int | None,
        request_id: str | None,
        occurred_from: dt.datetime | None,
        occurred_to: dt.datetime | None,
    ) -> list[tuple[AuditEvent, User | None]]:
        """One page of events, newest first, each with the account that acted.

        **Ordered by ``occurred_at DESC, id DESC``**, matching
        ``ix_audit_events_hotel_id_occurred_at`` term for term. The ``id`` is not decoration:
        several events can share a timestamp to the microsecond -- a booking created and its
        first payment posted by the same request do -- and an order that is not total lets a
        row appear on two pages or on none.

        **An OUTER join**, so an event whose actor is null is still returned rather than
        silently dropped from the history. Joined in one statement rather than resolving each
        actor afterwards: this is a listing, and the N+1 shape is the one this project has
        had to fix before.

        ``LIMIT``/``OFFSET`` are the database's. Nothing is paginated in Python, and nothing
        outside the requested page is fetched.
        """
        statement = self._filtered(
            select(AuditEvent, User).outerjoin(User, User.id == AuditEvent.actor_user_id),
            scope,
            action=action,
            resource_type=resource_type,
            resource_reference=resource_reference,
            actor_user_id=actor_user_id,
            request_id=request_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        rows = self._session.execute(
            statement.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [(event, user) for event, user in rows]

    @staticmethod
    def _filtered[StatementT: Select[Any]](
        statement: StatementT,
        scope: ColumnElement[bool],
        *,
        action: str | None,
        resource_type: str | None,
        resource_reference: str | None,
        actor_user_id: int | None,
        request_id: str | None,
        occurred_from: dt.datetime | None,
        occurred_to: dt.datetime | None,
    ) -> StatementT:
        """Apply the scope and then the optional filters, in that order.

        ``scope`` is required and positional -- it is not one of the optional arguments. A
        caller cannot omit it, pass None for it, or reorder it away, which is the point: every
        read of this table states whether it is asking about one property or about the events
        that belong to none, and that is expressed by the signature rather than by remembering.

        The two scopes are the only ones that exist, both built above, and a static test
        asserts no third is constructed anywhere in the codebase.
        """
        statement = statement.where(scope)
        if action is not None:
            statement = statement.where(AuditEvent.action == action)
        if resource_type is not None:
            statement = statement.where(AuditEvent.resource_type == resource_type)
        if resource_reference is not None:
            # Equality, never a pattern match. A LIKE here would turn the filter into a
            # way to sweep the column for references the caller has not been given.
            statement = statement.where(AuditEvent.resource_reference == resource_reference)
        if actor_user_id is not None:
            statement = statement.where(AuditEvent.actor_user_id == actor_user_id)
        if request_id is not None:
            statement = statement.where(AuditEvent.request_id == request_id)
        if occurred_from is not None:
            statement = statement.where(AuditEvent.occurred_at >= occurred_from)
        if occurred_to is not None:
            statement = statement.where(AuditEvent.occurred_at <= occurred_to)
        return statement


__all__ = ["PLATFORM_SCOPE", "AuditRepository", "hotel_scope"]
