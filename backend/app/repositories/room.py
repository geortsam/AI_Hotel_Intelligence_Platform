"""Physical room persistence.

**Every lookup is scoped by the full parent chain.** There is deliberately no
``get_by_number(number)``, no ``get_by_id(id)`` and no unscoped ``list_page`` in this module:
each could return a room outside the requested hotel and room type, and the safest way to
prevent that call is for it not to exist.

Two different scopings are offered, and the difference is the schema's, not a preference:

* ``get_in_hotel`` -- ``(hotel_id, room_number)``, which is exactly the unique constraint.
* ``get_in_room_type`` -- the same, narrowed to one room type as well.

Because ``UNIQUE (hotel_id, room_number)`` is per hotel, the room-type-scoped variant can
only ever return the same row or nothing. It exists so the service can answer "does this room
exist here?" and "does it belong to the type named in the path?" as one query rather than
fetching and then comparing.

Nothing here commits. Transaction boundaries belong to the service.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.booking import BookingRoom
from app.models.enums import INVENTORY_HOLDING_STATUSES, OUT_OF_SERVICE_ROOM_STATUSES
from app.models.room import Room, RoomType


class RoomRepository:
    """Data access for the room aggregate, always scoped to a hotel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, room: Room) -> Room:
        """Stage a new room and flush so defaults and server values are populated."""
        self._session.add(room)
        self._session.flush()
        self._session.refresh(room)
        return room

    def get_in_room_type(self, hotel_id: int, room_type_id: int, room_number: str) -> Room | None:
        """The fully scoped lookup: hotel, room type and number must all match."""
        return self._session.scalars(
            select(Room).where(
                Room.hotel_id == hotel_id,
                Room.room_type_id == room_type_id,
                Room.room_number == room_number,
            )
        ).one_or_none()

    def get_in_hotel(self, hotel_id: int, room_number: str) -> Room | None:
        """Lookup at the grain of the unique constraint: one hotel, one number.

        Used to tell "no such room at this hotel" apart from "that number exists here but
        belongs to a different room type" -- two situations that both end in 404 for the
        client but mean different things when reporting a create conflict.
        """
        return self._session.scalars(
            select(Room).where(Room.hotel_id == hotel_id, Room.room_number == room_number)
        ).one_or_none()

    def number_exists_in_hotel(self, hotel_id: int, room_number: str) -> bool:
        """Whether the hotel already uses this number, in ANY room type."""
        return (
            self._session.scalar(
                select(func.count())
                .select_from(Room)
                .where(Room.hotel_id == hotel_id, Room.room_number == room_number)
            )
            or 0
        ) > 0

    def count_in_room_type(self, hotel_id: int, room_type_id: int) -> int:
        """Total rooms of one type at one hotel, for the pagination envelope."""
        return (
            self._session.scalar(
                select(func.count())
                .select_from(Room)
                .where(Room.hotel_id == hotel_id, Room.room_type_id == room_type_id)
            )
            or 0
        )

    def list_page_in_room_type(
        self, hotel_id: int, room_type_id: int, *, limit: int, offset: int
    ) -> list[Room]:
        """One page of a room type's rooms in a stable order.

        Ordered by ``room_number``, which is unique within the hotel and therefore a total
        order on this result set -- no tiebreaker is needed.
        """
        return list(
            self._session.scalars(
                select(Room)
                .where(Room.hotel_id == hotel_id, Room.room_type_id == room_type_id)
                .order_by(Room.room_number.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def available_in_hotel(
        self,
        hotel_id: int,
        check_in: dt.date,
        check_out: dt.date,
        *,
        room_type_id: int | None = None,
        min_occupancy: int | None = None,
    ) -> list[tuple[Room, RoomType]]:
        """Rooms this hotel could sell for ``[check_in, check_out)``, with their type.

        **This predicate is the read-side mirror of the write-side authority**, and the two
        are written to be read side by side. The constraint the database enforces is::

            EXCLUDE USING gist (
                room_id WITH =,
                daterange(check_in_date, check_out_date, '[)') WITH &&
            ) WHERE (booking_status = ANY (ARRAY['confirmed','checked_in']))

        so the NOT EXISTS below matches it term for term: same room, same half-open
        ``daterange`` with the same ``'[)'`` bound, same ``&&`` operator, and the same status
        set -- taken from :data:`INVENTORY_HOLDING_STATUSES`, which is also the constant the
        constraint's own WHERE clause is generated from. A search that used ``<`` and ``>``
        comparisons instead would be a second, subtly different interval definition, and the
        first disagreement would be a room sold twice.

        One statement. The overlap test is a correlated ``NOT EXISTS`` evaluated by
        PostgreSQL, not a booking list walked in Python: the cost is one indexed probe per
        candidate room rather than a scan of the property's booking history.

        Returned with the room type joined rather than lazily loaded, so grouping by type
        costs no further queries.
        """
        overlapping = (
            select(BookingRoom.id)
            .where(
                BookingRoom.room_id == Room.id,
                BookingRoom.booking_status.in_(INVENTORY_HOLDING_STATUSES),
                func.daterange(BookingRoom.check_in_date, BookingRoom.check_out_date, "[)").op(
                    "&&"
                )(func.daterange(check_in, check_out, "[)")),
            )
            .exists()
        )

        statement = (
            select(Room, RoomType)
            .join(RoomType, RoomType.id == Room.room_type_id)
            # Tenancy is asserted on the ROOM, not inferred from the room type: the room is
            # what gets allocated, and `uq_rooms_id_hotel_id` is what the allocation's own
            # foreign key resolves against.
            .where(
                Room.hotel_id == hotel_id,
                RoomType.hotel_id == hotel_id,
                Room.is_active.is_(True),
                RoomType.is_active.is_(True),
                Room.status.not_in(OUT_OF_SERVICE_ROOM_STATUSES),
                ~overlapping,
            )
            .order_by(RoomType.code, Room.room_number)
        )
        if room_type_id is not None:
            statement = statement.where(Room.room_type_id == room_type_id)
        if min_occupancy is not None:
            statement = statement.where(RoomType.max_occupancy >= min_occupancy)

        return [(room, room_type) for room, room_type in self._session.execute(statement).all()]

    def apply_changes(self, room: Room, changes: dict[str, Any]) -> Room:
        """Apply a partial update to a managed instance and flush it."""
        for field, value in changes.items():
            setattr(room, field, value)
        self._session.flush()
        self._session.refresh(room)
        return room

    def delete(self, room: Room) -> None:
        """Delete with a Core statement, so the database's ON DELETE policy is authoritative.

        Deliberately NOT ``session.delete()``, for the reason established in Stage 3B.1: the
        ORM's default cascade would first null out children's ``room_id`` and report a NOT
        NULL violation, so PostgreSQL's ``ON DELETE RESTRICT`` on ``booking_rooms`` would
        never be consulted -- and rooms with real stay history could be silently detached
        from it if that column were ever nullable.
        """
        self._session.execute(sql_delete(Room).where(Room.id == room.id))
        self._session.flush()
        # The row is gone; detach the instance so the identity map cannot serve a ghost.
        self._session.expunge(room)


__all__ = ["RoomRepository"]
