"""Recording what happened, and reading it back.

Stage 4.5.11 ended with a finding: the platform could move a booking's dates, rooms and
prices, refund a charge, or change who administers a property, and afterwards nothing said who
had done it. This module is that record.

Two classes, and the split is the design.

:class:`AuditTrail` is the **writer**, and it owns no transaction. It has no ``commit`` and no
``rollback``, deliberately: an audit row is staged on the same session, inside the same
transaction, as the mutation it describes, and it becomes durable at that mutation's commit or
vanishes at its rollback. Nothing here schedules, queues, retries or defers -- if the booking
did not commit, the event describing it did not either, and the reason is that they were never
two transactions to begin with.

:class:`AuditQueryService` is the **reader** for one property, hotel-scoped and paginated,
and it writes nothing at all.

:class:`PlatformAuditQueryService` (Stage 4.5.13) is the reader for the events that belong to
no property -- a password change, an edit to one of the three global catalogues. **It is a
separate class rather than a method on the one above, and it is constructed with the
repository alone**: it holds no scope resolver, so it has no collaborator that could reach a
hotel, resolve a membership or compare a role. That mirrors the split between
:class:`~app.services.authorization.HotelAccessPolicy` and
:class:`~app.services.authorization.PlatformAccessPolicy`, which exists for the same reason --
two questions that must never be answered by one object, because the first ``or platform
admin`` somebody writes into a tenant check is a cross-tenant leak.

The two readers are not a second implementation. Both go through the one
:class:`~app.repositories.audit.AuditRepository`, where exactly one COUNT and one page SELECT
exist, and both render through :func:`_common_fields`, so a field or a filter cannot come to
mean one thing on one surface and something else on the other.

**The actor is bound, not passed.** An ``AuditTrail`` carries the authenticated caller it was
constructed with, so a service records an event without ever naming an identity -- and
therefore cannot name the wrong one. It is the same pattern
:class:`~app.services.authorization.HotelAccessPolicy` uses, for the same reason.

**Data minimisation is enforced here, once.** :meth:`AuditTrail.record` refuses a ``details``
key outside :data:`~app.models.enums.SAFE_AUDIT_DETAIL_KEYS`. Every value under those keys is
written from a literal at a call site in a service; none is copied out of a request body. So
the closed key set is a complete description of what an audit row can ever say, and it
contains no password, digest, token, header, cookie, card fragment, processor reference, guest
name, address, or internal key.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from app.core.errors import ValidationError
from app.core.request_id import current_request_id
from app.models.audit import AuditEvent
from app.models.enums import (
    SAFE_AUDIT_DETAIL_KEYS,
    AuditAction,
    AuditResourceType,
    HotelRole,
)
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.schemas.audit import AuditEventResponse, PlatformAuditEventResponse
from app.schemas.common import Page
from app.services.scope import HotelScopeResolver

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: The minimum role that may read a hotel's audit history.
#:
#: MANAGER, matching ``GET /hotels/{id}/members`` exactly -- and matched to it on purpose,
#: because the two disclose the same class of thing. Audit history says who did what, which is
#: management information about the people working at a property rather than operational data
#: about its guests.
#:
#: STAFF is refused. Staff GENERATE most of these events; being able to review the trail of
#: one's own colleagues is a different capability from doing the work, and the conservative
#: direction on a table this sensitive is to start narrow. VIEWER is refused for the same
#: reason and more so. OWNER outranks MANAGER and is admitted by the rank comparison rather
#: than by being listed, which is how every other requirement in this codebase is expressed.
AUDIT_READ_ROLE = HotelRole.MANAGER


class AuditTrail:
    """Records events onto the transaction that is already open.

    Constructed per request with the authenticated caller, or with ``None`` where no caller is
    known at construction time -- see :meth:`for_actor`.
    """

    def __init__(self, repository: AuditRepository, actor: User | None = None) -> None:
        self._repository = repository
        self._actor = actor

    def for_actor(self, actor: User) -> AuditTrail:
        """A trail bound to *actor*, sharing this one's repository and session.

        Exists for exactly one caller: :meth:`app.services.auth.AuthService.change_password`.
        Authentication resolves the token, so the authenticated user cannot be a dependency of
        the service that does the resolving -- and ``change_password`` is handed the caller
        directly by ``CurrentUserDep``, which is why it can bind one here and no other method
        needs to.

        Returns a NEW trail rather than mutating this one, so binding an actor cannot leak
        sideways into another use of the same object.
        """
        return AuditTrail(self._repository, actor)

    def record(
        self,
        action: AuditAction,
        resource_type: AuditResourceType,
        resource_reference: str,
        *,
        hotel_id: int | None = None,
        details: dict[str, object] | None = None,
    ) -> AuditEvent:
        """Stage one event on the caller's open transaction.

        Called from inside a service's ``try`` block, BEFORE its ``commit()``. That ordering is
        the requirement, not a convenience: an event written after the commit would be a second
        transaction that could fail on its own, leaving a change nobody recorded -- or succeed
        on its own, leaving a record of a change that rolled back.

        *action* and *resource_type* are enum members rather than strings, so a typo is a
        ``NameError`` at import rather than a row the CHECK constraint rejects at runtime.

        The request id is read from the ContextVar the existing middleware binds. No second
        correlation system is introduced, and outside a request there is simply no id -- the
        column is nullable for that case rather than filled with a placeholder.
        """
        payload = self._safe_details(details)
        return self._repository.add(
            AuditEvent(
                hotel_id=hotel_id,
                actor_user_id=self._actor.id if self._actor is not None else None,
                action=action.value,
                resource_type=resource_type.value,
                resource_reference=resource_reference,
                request_id=current_request_id(),
                details=payload,
            )
        )

    @staticmethod
    def _safe_details(details: dict[str, object] | None) -> dict[str, object]:
        """Return *details*, refusing any key outside the approved set.

        Raises rather than silently dropping. Every key that reaches here is a literal written
        in a service -- none is client-controlled -- so an unapproved one is a programming
        mistake, and the loud failure surfaces it in the test suite instead of quietly
        shipping a payload nobody reviewed. Dropping it instead would produce an audit row
        that is missing exactly the field somebody thought was important.
        """
        if not details:
            return {}
        unapproved = sorted(set(details) - SAFE_AUDIT_DETAIL_KEYS)
        if unapproved:
            raise ValueError(
                f"audit details may not carry {unapproved}; add the key to "
                "SAFE_AUDIT_DETAIL_KEYS only after deciding it is safe to store."
            )
        return dict(details)


def _common_fields(event: AuditEvent, actor: User | None) -> dict[str, Any]:
    """The nine fields both audit surfaces report, named one by one.

    Explicit rather than ``model_validate(event)``: the row carries ``hotel_id`` and
    ``actor_user_id``, and constructing a response from attributes is how one of them
    eventually reaches a client. Named fields cannot -- neither internal key appears below,
    and neither can be added by accident, because every key here is a literal.

    Shared by both readers so the mapping from a row to a response exists once. A field added
    to the audit contract is added here and appears on both surfaces, or it appears on
    neither; it cannot appear on one.
    """
    return {
        "public_id": event.public_id,
        "occurred_at": event.occurred_at,
        "action": event.action,
        "resource_type": event.resource_type,
        "resource_reference": event.resource_reference,
        "actor_public_id": actor.public_id if actor is not None else None,
        "actor_email": actor.email if actor is not None else None,
        "request_id": event.request_id,
        "details": dict(event.details or {}),
    }


class AuditQueryService:
    """Reads one hotel's audit history. Writes nothing, and has no way to."""

    def __init__(self, repository: AuditRepository, scope: HotelScopeResolver) -> None:
        self._repository = repository
        self._scope = scope

    def list(
        self,
        hotel_public_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
        action: AuditAction | None = None,
        resource_type: AuditResourceType | None = None,
        resource_reference: str | None = None,
        actor_public_id: uuid.UUID | None = None,
        request_id: str | None = None,
        occurred_from: dt.datetime | None = None,
        occurred_to: dt.datetime | None = None,
    ) -> Page[AuditEventResponse]:
        """One page of this hotel's audit history, newest first.

        Authorization is the resolver's, at :data:`AUDIT_READ_ROLE` -- the same call
        ``MembershipService.list`` makes for the member listing, and for the same reason. A
        non-member meets the hotel's own 404 wall before any audit row is considered, so this
        endpoint cannot be used to discover that a property exists.

        The actor filter is given as a PUBLIC id and resolved here. An id that names no
        account resolves to nothing and the page is empty -- not an error, because a filter
        that answered differently for a real account than for an invented one would be an
        account-enumeration oracle wearing a filter's clothes.

        Stage 4.5.22. ``resource_reference`` answers 'what happened to THIS booking', and
        the window is validated rather than silently tolerated. Both come AFTER the scope
        resolution, in that order, for the reason :class:`AnalyticsService` resolves before
        it validates: a caller who is not a member must meet the hotel's 404 wall whatever
        they put in the query string, so a 422 can only ever be seen by somebody already
        entitled to read this history.
        """
        hotel = self._scope.require_hotel_with_role(hotel_public_id, AUDIT_READ_ROLE)
        self._require_window(occurred_from, occurred_to)

        actor_user_id: int | None = None
        if actor_public_id is not None:
            actor_user_id = self._repository.actor_id_for_public_id(actor_public_id)
            if actor_user_id is None:
                return Page.build(items=[], total=0, page=page, page_size=page_size)

        filters = {
            "action": action.value if action is not None else None,
            "resource_type": resource_type.value if resource_type is not None else None,
            "resource_reference": resource_reference,
            "actor_user_id": actor_user_id,
            "request_id": request_id,
            "occurred_from": occurred_from,
            "occurred_to": occurred_to,
        }
        total = self._repository.count_for_hotel(hotel.id, **filters)  # type: ignore[arg-type]
        rows = self._repository.page_for_hotel(
            hotel.id,
            limit=page_size,
            offset=(page - 1) * page_size,
            **filters,  # type: ignore[arg-type]
        )

        return Page.build(
            items=[self._render(event, actor, hotel.public_id) for event, actor in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    @staticmethod
    def _require_window(occurred_from: dt.datetime | None, occurred_to: dt.datetime | None) -> None:
        """Refuse a window that ends before it begins.

        An inverted window used to return an empty page, which is a true answer to a
        question nobody meant to ask: no event occurs after the later bound and before the
        earlier one. An empty page reads like a clean audit history, and a caller checking
        one has every reason to be told the question was malformed instead.

        Rejected rather than swapped, as :class:`AnalyticsService` rejects a reversed date
        range: swapping would answer a different question and report it as the one asked.
        """
        if occurred_from is not None and occurred_to is not None and occurred_to < occurred_from:
            raise ValidationError("occurred_to must not be earlier than occurred_from.")

    @staticmethod
    def _render(
        event: AuditEvent, actor: User | None, hotel_public_id: uuid.UUID
    ) -> AuditEventResponse:
        """The shared nine fields, plus the hotel this page is about.

        ``hotel_public_id`` comes from the hotel resolved from the URL, not from a join --
        every row on this page belongs to that hotel by construction, and joining ``hotels``
        to re-derive a value already in hand would be a query per page for nothing.
        """
        return AuditEventResponse(hotel_public_id=hotel_public_id, **_common_fields(event, actor))


class PlatformAuditQueryService:
    """Reads the audit events that belong to no property (Stage 4.5.13).

    **It has one collaborator and it is the repository.** No scope resolver, no membership
    repository, no access policy, no hotel of any kind -- so there is no object here through
    which a hotel could be resolved, a role compared or a membership read. That is the security
    boundary expressed as a constructor signature, and a static test asserts it.

    **It decides no authorization**, exactly as the catalogue services decide none. Platform
    authority is declared on the route, by ``require_platform_admin``, which is where every
    other platform-guarded operation in this codebase declares it. A service that checked for
    itself would be a second gate, and the second gate is the one that gets forgotten.
    """

    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    def list(
        self,
        *,
        page: int,
        page_size: int,
        action: AuditAction | None = None,
        resource_type: AuditResourceType | None = None,
        actor_public_id: uuid.UUID | None = None,
        request_id: str | None = None,
        occurred_from: dt.datetime | None = None,
        occurred_to: dt.datetime | None = None,
    ) -> Page[PlatformAuditEventResponse]:
        """One page of the platform's audit history, newest first.

        **It takes no hotel identifier and has nowhere to put one.** The scope is
        :data:`~app.repositories.audit.PLATFORM_SCOPE` -- ``hotel_id IS NULL`` in the WHERE
        clause, applied by the database -- so a hotel-scoped row is not fetched and filtered
        out, it is never selected. A caller cannot widen this to another scope, because the
        method exposes no argument that could.

        The filters are the hotel surface's, unchanged, down to the actor filter being given
        as a public id and resolving to an empty page when it names no account. An action that
        only ever occurs at a property -- ``booking.created``, say -- is accepted and returns
        nothing, which is the honest answer rather than a 422 that would make the two surfaces
        disagree about what a valid filter is.
        """
        actor_user_id: int | None = None
        if actor_public_id is not None:
            actor_user_id = self._repository.actor_id_for_public_id(actor_public_id)
            if actor_user_id is None:
                return Page.build(items=[], total=0, page=page, page_size=page_size)

        filters = {
            "action": action.value if action is not None else None,
            "resource_type": resource_type.value if resource_type is not None else None,
            "actor_user_id": actor_user_id,
            "request_id": request_id,
            "occurred_from": occurred_from,
            "occurred_to": occurred_to,
        }
        total = self._repository.count_platform_scoped(**filters)  # type: ignore[arg-type]
        rows = self._repository.page_platform_scoped(
            limit=page_size,
            offset=(page - 1) * page_size,
            **filters,  # type: ignore[arg-type]
        )

        return Page.build(
            items=[
                PlatformAuditEventResponse(**_common_fields(event, actor)) for event, actor in rows
            ],
            total=total,
            page=page,
            page_size=page_size,
        )


__all__ = [
    "AUDIT_READ_ROLE",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "AuditQueryService",
    "AuditTrail",
    "PlatformAuditQueryService",
]
