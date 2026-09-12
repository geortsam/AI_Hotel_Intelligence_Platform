"""Room type business logic and unit-of-work boundaries.

Owns the transaction (``get_db`` does not commit) and owns one rule the router must never
have to think about: **a room type is reached only through its owning hotel.** Every public
method takes ``hotel_public_id`` and resolves it to an internal ``hotel_id`` first. A code
that exists at hotel A is simply not found under hotel B, because the lookup that would
have found it is never issued.

Knows the domain; knows no SQL and no HTTP.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    GENERIC_CONFLICT_MESSAGE,
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    sqlstate_of,
)
from app.models.hotel import Hotel
from app.models.room import RoomType
from app.repositories.room_type import RoomTypeRepository
from app.schemas.common import Page
from app.schemas.room_type import RoomTypeCreate, RoomTypeResponse, RoomTypeUpdate
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class RoomTypeService:
    """Domain operations on room types, always within one hotel."""

    def __init__(
        self,
        session: Session,
        repository: RoomTypeRepository,
        scope: HotelScopeResolver,
    ) -> None:
        self._session = session
        self._repository = repository
        self._scope = scope

    # --- reads --------------------------------------------------------------------------

    def get(self, hotel_public_id: uuid.UUID, code: str) -> RoomTypeResponse:
        """Return one room type belonging to this hotel, or raise 404."""
        hotel = self._require_hotel(hotel_public_id)
        return self._to_response(self._require_room_type(hotel, code), hotel)

    def list(
        self, hotel_public_id: uuid.UUID, *, page: int, page_size: int
    ) -> Page[RoomTypeResponse]:
        """One page of this hotel's room types, ordered by code.

        A missing hotel is a 404 rather than an empty page: "this hotel has no room types"
        and "this hotel does not exist" are different answers, and a client that cannot
        tell them apart will hide its own bugs.
        """
        hotel = self._require_hotel(hotel_public_id)
        total = self._repository.count_for_hotel(hotel.id)
        rows = self._repository.list_page_for_hotel(
            hotel.id, limit=page_size, offset=(page - 1) * page_size
        )
        return Page.build(
            items=[self._to_response(row, hotel) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    # --- writes -------------------------------------------------------------------------

    def create(self, hotel_public_id: uuid.UUID, payload: RoomTypeCreate) -> RoomTypeResponse:
        """Create a room type under an existing hotel and commit.

        The hotel is verified first and never created implicitly: a typo in the path must
        produce a 404, not a new property.
        """
        hotel = self._require_hotel(hotel_public_id)

        if self._repository.code_exists(hotel.id, payload.code):
            raise ConflictError(f"This hotel already has a room type with code {payload.code!r}.")

        room_type = RoomType(hotel_id=hotel.id, **payload.model_dump())
        try:
            created = self._repository.add(room_type)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc, code=payload.code) from exc

        return self._to_response(created, hotel)

    def update(
        self, hotel_public_id: uuid.UUID, code: str, payload: RoomTypeUpdate
    ) -> RoomTypeResponse:
        """Apply a partial update and commit."""
        hotel = self._require_hotel(hotel_public_id)
        room_type = self._require_room_type(hotel, code)
        changes: dict[str, Any] = payload.model_dump(exclude_unset=True)

        if not changes:
            return self._to_response(room_type, hotel)

        self._check_occupancy_after(room_type, changes)

        try:
            updated = self._repository.apply_changes(room_type, changes)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc, code=code) from exc

        return self._to_response(updated, hotel)

    def delete(self, hotel_public_id: uuid.UUID, code: str) -> None:
        """Delete a room type and commit, honouring the database's RESTRICT policy.

        Rooms reference their room type with ``ON DELETE RESTRICT``. No cascade is invented:
        a room type still in use must fail loudly rather than silently take its rooms with
        it, and that refusal becomes a 409 explaining what to clear first.
        """
        hotel = self._require_hotel(hotel_public_id)
        room_type = self._require_room_type(hotel, code)
        try:
            self._repository.delete(room_type)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            if sqlstate_of(exc) in SQLSTATE_DEPENDENCY_VIOLATIONS:
                raise ConflictError(
                    "This room type cannot be deleted because rooms are still assigned to "
                    "it. Reassign or remove those rooms first, or deactivate the room type "
                    "instead."
                ) from exc
            raise self._translate(exc, code=code) from exc

    # --- internals ----------------------------------------------------------------------

    def _require_hotel(self, hotel_public_id: uuid.UUID) -> Hotel:
        """Delegate to the shared resolver: the parent chain is resolved in one place."""
        return self._scope.require_hotel(hotel_public_id)

    def _require_room_type(self, hotel: Hotel, code: str) -> RoomType:
        """Delegate to the shared resolver."""
        return self._scope.require_room_type(hotel, code)

    def _check_occupancy_after(self, room_type: RoomType, changes: dict[str, Any]) -> None:
        """Validate the occupancy rule against the post-update state.

        The update schema can only compare the two values when a client sends both. When
        only one is supplied, the other comes from the stored row -- so the check has to
        happen here, where both are known. Without it, lowering ``max_occupancy`` alone
        would reach the database and come back as an opaque check-constraint conflict.
        """
        max_occupancy = changes.get("max_occupancy", room_type.max_occupancy)
        standard = changes.get("standard_occupancy", room_type.standard_occupancy)
        if standard > max_occupancy:
            raise ConflictError(
                f"standard_occupancy must not exceed max_occupancy ({standard} > {max_occupancy})."
            )

    @staticmethod
    def _to_response(room_type: RoomType, hotel: Hotel) -> RoomTypeResponse:
        """Build the response, attaching the parent's public id.

        Done here rather than by ``from_attributes`` alone because ``hotel_public_id`` is not
        a column on the row -- reading it through ``room_type.hotel`` would emit a lazy load
        per item and turn a page of 20 into 21 queries.
        """
        return RoomTypeResponse.model_validate(
            {
                "hotel_public_id": hotel.public_id,
                "code": room_type.code,
                "name": room_type.name,
                "description": room_type.description,
                "max_occupancy": room_type.max_occupancy,
                "standard_occupancy": room_type.standard_occupancy,
                "bed_count": room_type.bed_count,
                "bed_configuration": room_type.bed_configuration,
                "size_sqm": room_type.size_sqm,
                "base_price": room_type.base_price,
                "currency": room_type.currency,
                "is_active": room_type.is_active,
                "created_at": room_type.created_at,
                "updated_at": room_type.updated_at,
            }
        )

    def _translate(self, exc: IntegrityError, *, code: str | None = None) -> Exception:
        """Turn a database integrity error into a domain error, leaking nothing."""
        state = sqlstate_of(exc)
        # No ``exc_info``: the driver renders the offending row into its message, so
        # logging it writes user data into the log. The SQLSTATE is what decides the
        # response, and it is all that is recorded. (Stage 3B.12 hygiene audit.)
        logger.warning("Room type integrity error (sqlstate=%s)", state)

        if state == SQLSTATE_UNIQUE_VIOLATION:
            detail = (
                f"This hotel already has a room type with code {code!r}."
                if code
                else "That value is already taken by another room type."
            )
            return ConflictError(detail)
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a room type constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("This room type is still referenced by other records.")
        return ConflictError(GENERIC_CONFLICT_MESSAGE)


__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "RoomTypeService"]
