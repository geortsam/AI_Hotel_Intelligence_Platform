"""Physical room business logic and unit-of-work boundaries.

Owns the transaction (``get_db`` does not commit) and owns the hierarchy rule: **a room is
reached only through its own hotel and its own room type.** Every public method resolves
``hotel_public_id`` then ``room_type_code`` before touching a room, so a room at hotel A is
not addressable through hotel B, and a room of type Deluxe is not addressable through
Standard.

Knows the domain; knows no SQL and no HTTP.
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
from app.models.hotel import Hotel
from app.models.room import Room, RoomType
from app.repositories.room import RoomRepository
from app.schemas.common import Page
from app.schemas.room import RoomCreate, RoomResponse, RoomUpdate
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class RoomService:
    """Domain operations on physical rooms, always within one hotel and room type."""

    def __init__(
        self, session: Session, repository: RoomRepository, scope: HotelScopeResolver
    ) -> None:
        self._session = session
        self._repository = repository
        self._scope = scope

    # --- reads --------------------------------------------------------------------------

    def get(
        self, hotel_public_id: uuid.UUID, room_type_code: str, room_number: str
    ) -> RoomResponse:
        """Return one room under this exact hotel and room type, or raise 404."""
        hotel, room_type = self._scope.require_hotel_and_room_type(hotel_public_id, room_type_code)
        room = self._require_room(hotel, room_type, room_number)
        return self._to_response(room, hotel, room_type)

    def list(
        self,
        hotel_public_id: uuid.UUID,
        room_type_code: str,
        *,
        page: int,
        page_size: int,
    ) -> Page[RoomResponse]:
        """One page of this room type's rooms, ordered by number.

        A missing hotel or room type is a 404 rather than an empty page: "this type has no
        rooms" and "this type does not exist" are different answers.
        """
        hotel, room_type = self._scope.require_hotel_and_room_type(hotel_public_id, room_type_code)
        total = self._repository.count_in_room_type(hotel.id, room_type.id)
        rows = self._repository.list_page_in_room_type(
            hotel.id, room_type.id, limit=page_size, offset=(page - 1) * page_size
        )
        return Page.build(
            items=[self._to_response(row, hotel, room_type) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    # --- writes -------------------------------------------------------------------------

    def create(
        self, hotel_public_id: uuid.UUID, room_type_code: str, payload: RoomCreate
    ) -> RoomResponse:
        """Create a room under an existing hotel and room type, and commit.

        The room type is resolved *through* the hotel, so its ``id`` can only ever belong to
        that hotel. The client never supplies ``room_type_id``, and could not smuggle in
        another property's type if it tried -- the composite foreign key
        ``(room_type_id, hotel_id)`` would reject it even if this code were wrong.
        """
        hotel, room_type = self._scope.require_hotel_and_room_type(hotel_public_id, room_type_code)

        # Uniqueness is per HOTEL, not per room type: number 101 may exist only once in a
        # property, whichever type owns it. Checked here so the common case is a clear 409
        # naming the conflict rather than a bare constraint violation.
        existing = self._repository.get_in_hotel(hotel.id, payload.room_number)
        if existing is not None:
            raise ConflictError(
                f"This hotel already has a room numbered {payload.room_number!r}. "
                "Room numbers are unique per hotel, not per room type."
            )

        room = Room(hotel_id=hotel.id, room_type_id=room_type.id, **payload.model_dump())
        try:
            created = self._repository.add(room)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc, room_number=payload.room_number) from exc

        return self._to_response(created, hotel, room_type)

    def update(
        self,
        hotel_public_id: uuid.UUID,
        room_type_code: str,
        room_number: str,
        payload: RoomUpdate,
    ) -> RoomResponse:
        """Apply a partial update and commit."""
        hotel, room_type = self._scope.require_hotel_and_room_type(hotel_public_id, room_type_code)
        room = self._require_room(hotel, room_type, room_number)
        changes: dict[str, Any] = payload.model_dump(exclude_unset=True)

        if not changes:
            return self._to_response(room, hotel, room_type)

        try:
            updated = self._repository.apply_changes(room, changes)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc, room_number=room_number) from exc

        return self._to_response(updated, hotel, room_type)

    def delete(self, hotel_public_id: uuid.UUID, room_type_code: str, room_number: str) -> None:
        """Delete a room and commit, honouring the database's RESTRICT policy.

        ``booking_rooms.room_id`` references rooms with ``ON DELETE RESTRICT``. No cascade is
        invented: a room with stay history must fail loudly rather than take that history
        with it, and the refusal becomes a 409 saying so.
        """
        hotel, room_type = self._scope.require_hotel_and_room_type(hotel_public_id, room_type_code)
        room = self._require_room(hotel, room_type, room_number)
        try:
            self._repository.delete(room)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            if sqlstate_of(exc) in SQLSTATE_DEPENDENCY_VIOLATIONS:
                raise ConflictError(
                    "This room cannot be deleted because it is still referenced by existing "
                    "reservations. Deactivate the room instead, or remove those records "
                    "first."
                ) from exc
            raise self._translate(exc, room_number=room_number) from exc

    # --- internals ----------------------------------------------------------------------

    def _require_room(self, hotel: Hotel, room_type: RoomType, room_number: str) -> Room:
        """Resolve a room within this hotel AND this room type, or raise 404.

        The scoped query is what makes ``Hotel A -> Standard -> 101`` a 404 when 101 is a
        Deluxe: the room exists at the hotel, but not under the type the path names, and the
        API reports it as absent rather than quietly serving the wrong parent's child.
        """
        room = self._repository.get_in_room_type(hotel.id, room_type.id, room_number)
        if room is None:
            raise NotFoundError("Room not found for this hotel and room type.")
        return room

    @staticmethod
    def _to_response(room: Room, hotel: Hotel, room_type: RoomType) -> RoomResponse:
        """Build the response, attaching the parents' public identifiers.

        Assembled from rows already in hand rather than through ``room.hotel`` and
        ``room.room_type``: those are lazy relationships, and reading them per item would
        turn a page of 20 into 41 queries.
        """
        return RoomResponse.model_validate(
            {
                "hotel_public_id": hotel.public_id,
                "room_type_code": room_type.code,
                "room_number": room.room_number,
                "floor": room.floor,
                "status": room.status,
                "notes": room.notes,
                "is_active": room.is_active,
                "created_at": room.created_at,
                "updated_at": room.updated_at,
            }
        )

    def _translate(self, exc: IntegrityError, *, room_number: str | None = None) -> Exception:
        """Turn a database integrity error into a domain error, leaking nothing."""
        state = sqlstate_of(exc)
        # No ``exc_info``: the driver renders the offending row into its message, so
        # logging it writes user data into the log. The SQLSTATE is what decides the
        # response, and it is all that is recorded. (Stage 3B.12 hygiene audit.)
        logger.warning("Room integrity error (sqlstate=%s)", state)

        if state == SQLSTATE_UNIQUE_VIOLATION:
            detail = (
                f"This hotel already has a room numbered {room_number!r}."
                if room_number
                else "That value is already taken by another room."
            )
            return ConflictError(detail)
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a room constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("This room is still referenced by other records.")
        return ConflictError("The request conflicts with the current state of the database.")


__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "RoomService"]
