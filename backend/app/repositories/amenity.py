"""Amenity and room-type-association persistence.

Two repositories with deliberately different scoping rules, because the two tables have
different natures:

* :class:`AmenityRepository` -- the catalogue is **global**. ``code`` is unique across the
  whole table, so an unscoped ``get_by_code`` is correct here, not a leak. This is the one
  place in the project where an unscoped lookup is the right answer, and it is right because
  the schema says so: ``uq_amenities_code`` has no tenant column in it.

* :class:`RoomTypeAmenityRepository` -- associations are **always scoped to one resolved
  ``room_type_id``**. Every method takes it; there is no "list all assignments" and no
  lookup by amenity alone that could reach another hotel's room type.

Nothing here commits. Transaction boundaries belong to the services.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.room import Amenity, RoomTypeAmenity


class AmenityRepository:
    """Data access for the global amenity catalogue."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, amenity: Amenity) -> Amenity:
        """Stage a new amenity and flush so the database assigns its key."""
        self._session.add(amenity)
        self._session.flush()
        self._session.refresh(amenity)
        return amenity

    def get_by_code(self, code: str) -> Amenity | None:
        """Fetch by the globally unique code, or None."""
        return self._session.scalars(select(Amenity).where(Amenity.code == code)).one_or_none()

    def code_exists(self, code: str) -> bool:
        """Cheaper than fetching the row when only existence matters."""
        return (
            self._session.scalar(
                select(func.count()).select_from(Amenity).where(Amenity.code == code)
            )
            or 0
        ) > 0

    def count(self) -> int:
        """Total amenities, for the pagination envelope."""
        return self._session.scalar(select(func.count()).select_from(Amenity)) or 0

    def list_page(self, *, limit: int, offset: int) -> list[Amenity]:
        """One page of the catalogue in a stable order.

        Ordered by ``code``, which is globally unique and therefore a total order on its
        own -- no tiebreaker is needed.
        """
        return list(
            self._session.scalars(
                select(Amenity).order_by(Amenity.code.asc()).limit(limit).offset(offset)
            ).all()
        )

    def apply_changes(self, amenity: Amenity, changes: dict[str, Any]) -> Amenity:
        """Apply a partial update to a managed instance and flush it."""
        for field, value in changes.items():
            setattr(amenity, field, value)
        self._session.flush()
        self._session.refresh(amenity)
        return amenity

    def delete(self, amenity: Amenity) -> None:
        """Delete with a Core statement, so the database's ON DELETE policy is authoritative.

        ``room_type_amenities.amenity_id`` is ``ON DELETE RESTRICT``: an amenity assigned to
        any room type must not be deletable. Using ``session.delete()`` would let the ORM
        clear the junction rows first and destroy exactly the protection the schema declares.
        """
        self._session.execute(sql_delete(Amenity).where(Amenity.id == amenity.id))
        self._session.flush()
        self._session.expunge(amenity)


class RoomTypeAmenityRepository:
    """Data access for room-type/amenity associations, always scoped to one room type."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def assign(self, room_type_id: int, amenity_id: int) -> None:
        """Create the association row and flush.

        A repeat assignment violates the composite primary key, which the service turns into
        a 409. The database is the authority on duplicates, not a prior read.
        """
        self._session.add(RoomTypeAmenity(room_type_id=room_type_id, amenity_id=amenity_id))
        self._session.flush()

    def is_assigned(self, room_type_id: int, amenity_id: int) -> bool:
        """Whether this exact pair already exists."""
        return (
            self._session.scalar(
                select(func.count())
                .select_from(RoomTypeAmenity)
                .where(
                    RoomTypeAmenity.room_type_id == room_type_id,
                    RoomTypeAmenity.amenity_id == amenity_id,
                )
            )
            or 0
        ) > 0

    def count_for_room_type(self, room_type_id: int) -> int:
        """Total amenities assigned to one room type."""
        return (
            self._session.scalar(
                select(func.count())
                .select_from(RoomTypeAmenity)
                .where(RoomTypeAmenity.room_type_id == room_type_id)
            )
            or 0
        )

    def list_page_for_room_type(
        self, room_type_id: int, *, limit: int, offset: int
    ) -> list[Amenity]:
        """One page of the amenities assigned to this room type, ordered by code.

        Joins through the junction rather than reading ``room_type.amenities``: the
        relationship would load every association at once, and pagination has to happen in
        the database, not in Python.
        """
        return list(
            self._session.scalars(
                select(Amenity)
                .join(RoomTypeAmenity, RoomTypeAmenity.amenity_id == Amenity.id)
                .where(RoomTypeAmenity.room_type_id == room_type_id)
                .order_by(Amenity.code.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def unassign(self, room_type_id: int, amenity_id: int) -> None:
        """Remove one association and flush.

        Deletes only the junction row. The amenity itself is untouched -- it is a shared
        catalogue entry that other room types may still be using.

        Whether the pair existed is the service's question, answered by ``is_assigned``: a
        404 should state a fact the code checked, not infer one from a driver row counter.
        """
        self._session.execute(
            sql_delete(RoomTypeAmenity).where(
                RoomTypeAmenity.room_type_id == room_type_id,
                RoomTypeAmenity.amenity_id == amenity_id,
            )
        )
        self._session.flush()


__all__ = ["AmenityRepository", "RoomTypeAmenityRepository"]
