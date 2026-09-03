"""Hotel business logic and unit-of-work boundaries.

This layer owns the transaction. ``get_db`` deliberately does not commit (Stage 3A), so each
mutating method here commits its own complete unit of work and rolls back if anything in it
fails. It knows the domain rules; it knows no SQL and no HTTP.

It is also where PostgreSQL's integrity errors become domain errors. Letting an
``IntegrityError`` escape would surface as a generic 503 through the Stage 3A database
handler, which is wrong twice over: a duplicate slug is the client's fault, not an outage,
and the driver message would carry the constraint name and the failing SQL.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    NotFoundError,
    sqlstate_of,
)
from app.models.enums import HotelRole
from app.models.hotel import Hotel
from app.models.membership import UserHotel
from app.repositories.hotel import HotelRepository
from app.repositories.membership import MembershipRepository
from app.schemas.common import Page
from app.schemas.hotel import HotelCreate, HotelResponse, HotelUpdate
from app.services.authorization import HotelAccessPolicy

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class HotelService:
    """Domain operations on hotels."""

    def __init__(
        self,
        session: Session,
        repository: HotelRepository,
        memberships: MembershipRepository,
        policy: HotelAccessPolicy,
    ) -> None:
        self._session = session
        self._repository = repository
        self._memberships = memberships
        # This is the ONE domain that resolves its own hotel rather than going through
        # HotelScopeResolver -- it IS the hotel resource. It therefore consults the same
        # policy object directly, so there is still exactly one authorization decision in
        # the codebase rather than two implementations of the same rule.
        self._policy = policy

    # --- reads --------------------------------------------------------------------------

    def get(self, public_id: uuid.UUID) -> HotelResponse:
        """Return one hotel, or raise :class:`NotFoundError`."""
        return HotelResponse.model_validate(self._require(public_id))

    def list(self, *, page: int, page_size: int) -> Page[HotelResponse]:
        """One page of the hotels the caller is a member of.

        **Filtered, not merely authenticated.** Listing every property would disclose the
        whole portfolio -- names, addresses and public ids -- to anyone who can register an
        account, which is a worse leak than any single cross-tenant read: it hands over the
        identifiers every other endpoint is keyed by.

        The count is filtered by the same predicate as the page, so the total cannot
        contradict the rows.
        """
        offset = (page - 1) * page_size
        user_id = self._policy.user.id
        total = self._memberships.count_for_user(user_id)
        rows = self._memberships.hotels_page_for_user(user_id, limit=page_size, offset=offset)
        return Page.build(
            items=[HotelResponse.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    # --- writes -------------------------------------------------------------------------

    def create(self, payload: HotelCreate) -> HotelResponse:
        """Create a hotel and commit.

        The slug is checked before the insert so the common case produces a clear 409 rather
        than relying on the constraint. The constraint is still the authority -- two
        concurrent requests can both pass the check -- so the IntegrityError path below
        remains necessary, not redundant.
        """
        if self._repository.slug_exists(payload.slug):
            raise ConflictError(f"A hotel with slug {payload.slug!r} already exists.")

        hotel = Hotel(**payload.model_dump())
        try:
            created = self._repository.add(hotel)
            # The creator becomes the owner in the SAME transaction. If this failed
            # separately the caller would have created a hotel they cannot reach -- an
            # orphan no one can administer and which RESTRICT then makes hard to remove.
            self._memberships.add(
                UserHotel(
                    user_id=self._policy.user.id,
                    hotel_id=created.id,
                    role=HotelRole.OWNER.value,
                )
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc, slug=payload.slug) from exc

        return HotelResponse.model_validate(created)

    def update(self, public_id: uuid.UUID, payload: HotelUpdate) -> HotelResponse:
        """Apply a partial update and commit.

        ``exclude_unset`` is what makes this a PATCH rather than a PUT: only fields the
        client actually sent are written, so an omitted field keeps its stored value while
        an explicit ``null`` still clears a nullable column.
        """
        hotel = self._require(public_id, required=HotelRole.OWNER)
        changes: dict[str, Any] = payload.model_dump(exclude_unset=True)

        if not changes:
            # Nothing to do. Returning the current state is friendlier than a 400 and keeps
            # the endpoint idempotent.
            return HotelResponse.model_validate(hotel)

        try:
            updated = self._repository.apply_changes(hotel, changes)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return HotelResponse.model_validate(updated)

    def delete(self, public_id: uuid.UUID) -> None:
        """Delete a hotel and commit, honouring the database's RESTRICT policy.

        No cascade is invented here. The schema deliberately protects room types, rooms,
        guests, bookings, revenue and expenses with ``ON DELETE RESTRICT``: deleting a hotel
        that has operating history must fail loudly rather than silently erase it. That
        refusal is translated into a 409 explaining why.
        """
        hotel = self._require(public_id, required=HotelRole.OWNER)
        try:
            # Access metadata goes first, in the same transaction. The hotel's own
            # memberships are ON DELETE RESTRICT, so leaving them would make every hotel
            # permanently undeletable -- including by the owner who just created it.
            self._memberships.delete_for_hotel(hotel.id)
            self._repository.delete(hotel)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            if sqlstate_of(exc) in SQLSTATE_DEPENDENCY_VIOLATIONS:
                raise ConflictError(
                    "This hotel cannot be deleted because other records still reference it. "
                    "Remove its room types, rooms, guests, bookings, revenue and expenses "
                    "first, or deactivate the hotel instead."
                ) from exc
            raise self._translate(exc) from exc

    # --- internals ----------------------------------------------------------------------

    def _require(self, public_id: uuid.UUID, *, required: HotelRole | None = None) -> Hotel:
        """Resolve a hotel, require membership, and optionally require a role.

        The same two-step order the scope resolver uses: a hotel that does not exist and one
        the caller is not a member of produce the identical 404, so neither reveals the
        other. ``required`` adds the rank check for the writing paths.
        """
        hotel = self._repository.get_by_public_id(public_id)
        if hotel is None:
            raise NotFoundError("Hotel not found.")
        if required is None:
            self._policy.authorize(hotel)
        else:
            self._policy.require_role(hotel, required)
        return hotel

    def _translate(self, exc: IntegrityError, *, slug: str | None = None) -> Exception:
        """Turn a database integrity error into a domain error, leaking nothing.

        The driver message carries the constraint name, the failing statement and the
        parameter values. It is logged and discarded; only a written-for-clients sentence
        is returned.
        """
        state = sqlstate_of(exc)
        # No ``exc_info``: the driver renders the offending row into its message, so
        # logging it writes user data into the log. The SQLSTATE is what decides the
        # response, and it is all that is recorded. (Stage 3B.12 hygiene audit.)
        logger.warning("Hotel integrity error (sqlstate=%s)", state)

        if state == SQLSTATE_UNIQUE_VIOLATION:
            detail = (
                f"A hotel with slug {slug!r} already exists."
                if slug
                else ("That value is already taken by another hotel.")
            )
            return ConflictError(detail)
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a hotel constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("This hotel is still referenced by other records.")
        return ConflictError("The request conflicts with the current state of the database.")


__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "HotelService"]
