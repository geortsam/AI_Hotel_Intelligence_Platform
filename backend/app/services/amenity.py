"""Amenity business logic and unit-of-work boundaries.

Two services, matching the two natures of the data:

* :class:`AmenityService` -- the global catalogue. No hotel is involved, so no scope resolver
  is needed and none is pretended.
* :class:`RoomTypeAmenityService` -- associations, which are reached only through the hotel
  and room type in the URL, using the same :class:`HotelScopeResolver` as rooms and room
  types.

Both own their transactions (``get_db`` does not commit) and translate PostgreSQL integrity
errors through the shared SQLSTATE vocabulary.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    NotFoundError,
    sqlstate_of,
)
from app.models.room import Amenity
from app.repositories.amenity import AmenityRepository, RoomTypeAmenityRepository
from app.schemas.amenity import (
    AmenityAssignment,
    AmenityCreate,
    AmenityResponse,
    AmenityUpdate,
)
from app.schemas.common import Page
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class AmenityService:
    """Domain operations on the global amenity catalogue."""

    def __init__(self, session: Session, repository: AmenityRepository) -> None:
        self._session = session
        self._repository = repository

    # --- reads --------------------------------------------------------------------------

    def get(self, code: str) -> AmenityResponse:
        """Return one amenity, or raise 404."""
        return AmenityResponse.model_validate(self._require(code))

    def list(self, *, page: int, page_size: int) -> Page[AmenityResponse]:
        """Return one page of the catalogue, ordered by code."""
        total = self._repository.count()
        rows = self._repository.list_page(limit=page_size, offset=(page - 1) * page_size)
        return Page.build(
            items=[AmenityResponse.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    # --- writes -------------------------------------------------------------------------

    def create(self, payload: AmenityCreate) -> AmenityResponse:
        """Create a catalogue entry and commit."""
        if self._repository.code_exists(payload.code):
            raise ConflictError(f"An amenity with code {payload.code!r} already exists.")

        amenity = Amenity(**payload.model_dump())
        try:
            created = self._repository.add(amenity)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc, code=payload.code) from exc

        return AmenityResponse.model_validate(created)

    def update(self, code: str, payload: AmenityUpdate) -> AmenityResponse:
        """Apply a partial update and commit."""
        amenity = self._require(code)
        changes: dict[str, Any] = payload.model_dump(exclude_unset=True)

        if not changes:
            return AmenityResponse.model_validate(amenity)

        try:
            updated = self._repository.apply_changes(amenity, changes)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc, code=code) from exc

        return AmenityResponse.model_validate(updated)

    def delete(self, code: str) -> None:
        """Delete a catalogue entry and commit, honouring the database's RESTRICT policy.

        ``room_type_amenities.amenity_id`` is ``ON DELETE RESTRICT``. No cascade is invented:
        an amenity in use by any room type -- at any hotel -- must fail loudly rather than
        quietly disappear from every room type that references it.
        """
        amenity = self._require(code)
        try:
            self._repository.delete(amenity)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            if sqlstate_of(exc) in SQLSTATE_DEPENDENCY_VIOLATIONS:
                raise ConflictError(
                    "This amenity cannot be deleted because it is still assigned to one or "
                    "more room types. Remove those assignments first."
                ) from exc
            raise self._translate(exc, code=code) from exc

    # --- internals ----------------------------------------------------------------------

    def _require(self, code: str) -> Amenity:
        """Fetch an amenity or raise the 404 the router will render."""
        amenity = self._repository.get_by_code(code)
        if amenity is None:
            raise NotFoundError("Amenity not found.")
        return amenity

    def _translate(self, exc: IntegrityError, *, code: str | None = None) -> Exception:
        """Turn a database integrity error into a domain error, leaking nothing."""
        state = sqlstate_of(exc)
        # No ``exc_info``: the driver renders the offending row into its message, so
        # logging it writes user data into the log. The SQLSTATE is what decides the
        # response, and it is all that is recorded. (Stage 3B.12 hygiene audit.)
        logger.warning("Amenity integrity error (sqlstate=%s)", state)

        if state == SQLSTATE_UNIQUE_VIOLATION:
            detail = (
                f"An amenity with code {code!r} already exists."
                if code
                else "That value is already taken by another amenity."
            )
            return ConflictError(detail)
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("This amenity is still referenced by other records.")
        return ConflictError("The request conflicts with the current state of the database.")


class RoomTypeAmenityService:
    """Assignment of catalogue amenities to a room type, within one hotel."""

    def __init__(
        self,
        session: Session,
        associations: RoomTypeAmenityRepository,
        amenities: AmenityRepository,
        scope: HotelScopeResolver,
    ) -> None:
        self._session = session
        self._associations = associations
        self._amenities = amenities
        self._scope = scope

    def list(
        self,
        hotel_public_id: uuid.UUID,
        room_type_code: str,
        *,
        page: int,
        page_size: int,
    ) -> Page[AmenityResponse]:
        """One page of the amenities assigned to this room type."""
        _, room_type = self._scope.require_hotel_and_room_type(hotel_public_id, room_type_code)
        total = self._associations.count_for_room_type(room_type.id)
        rows = self._associations.list_page_for_room_type(
            room_type.id, limit=page_size, offset=(page - 1) * page_size
        )
        return Page.build(
            items=[AmenityResponse.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def assign(
        self, hotel_public_id: uuid.UUID, room_type_code: str, payload: AmenityAssignment
    ) -> AmenityResponse:
        """Attach an existing catalogue amenity to this room type and commit.

        The room type is resolved *through* the hotel, so the ``room_type_id`` written here
        can only ever belong to the hotel in the path. A client cannot name a room type
        another property owns, because it never supplies a room type id at all.

        The amenity must already exist: assignment references the catalogue, it does not
        extend it. Creating one implicitly would let a typo silently populate the shared
        catalogue with junk that every hotel then sees.
        """
        _, room_type = self._scope.require_hotel_and_room_type(hotel_public_id, room_type_code)
        amenity = self._require_amenity(payload.code)

        if self._associations.is_assigned(room_type.id, amenity.id):
            raise ConflictError(f"Amenity {amenity.code!r} is already assigned to this room type.")

        try:
            self._associations.assign(room_type.id, amenity.id)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            # The composite primary key is the real authority on duplicates; the check above
            # only turns the common case into a clearer message.
            if sqlstate_of(exc) == SQLSTATE_UNIQUE_VIOLATION:
                raise ConflictError(
                    f"Amenity {amenity.code!r} is already assigned to this room type."
                ) from exc
            logger.warning("Assignment integrity error (sqlstate=%s)", sqlstate_of(exc))
            raise ConflictError(
                "The request conflicts with the current state of the database."
            ) from exc

        return AmenityResponse.model_validate(amenity)

    def unassign(self, hotel_public_id: uuid.UUID, room_type_code: str, amenity_code: str) -> None:
        """Remove one assignment and commit.

        Deletes the junction row only. The amenity stays in the catalogue, still available
        to every other room type -- which is the whole point of a shared catalogue.
        """
        _, room_type = self._scope.require_hotel_and_room_type(hotel_public_id, room_type_code)
        amenity = self._require_amenity(amenity_code)

        if not self._associations.is_assigned(room_type.id, amenity.id):
            raise NotFoundError("This amenity is not assigned to this room type.")

        self._associations.unassign(room_type.id, amenity.id)
        self._session.commit()

    def _require_amenity(self, code: str) -> Amenity:
        """Resolve a catalogue amenity, or raise 404."""
        amenity = self._amenities.get_by_code(code)
        if amenity is None:
            raise NotFoundError("Amenity not found.")
        return amenity


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "AmenityService",
    "RoomTypeAmenityService",
]
