"""Booking persistence, including its allocations and priced nights.

**Every lookup is scoped by ``hotel_id``**, as in every other domain: ``public_id`` is
globally unique, so an unscoped lookup would work and would be able to reach another
property's booking. The method does not exist.

This repository owns three tables because they are one aggregate: a booking is not valid
without its allocations, and an allocation is not valid without its nights -- the deferred
trigger says so. Splitting them across repositories would invite a caller to write one
without the others.

Nothing here commits, and nothing here pre-checks room availability: the GiST exclusion
constraint on ``booking_rooms`` is the authority, and asking first would be a race it already
forecloses.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.booking import Booking, BookingRoom, BookingRoomNight
from app.models.room import Room


class BookingRepository:
    """Data access for the booking aggregate, always scoped to one hotel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- writes -------------------------------------------------------------------------

    def add_booking(self, booking: Booking) -> Booking:
        """Stage the booking header and flush so the database assigns its keys."""
        self._session.add(booking)
        self._session.flush()
        self._session.refresh(booking)
        return booking

    def add_room(self, booking_room: BookingRoom) -> BookingRoom:
        """Stage one allocation and flush.

        The flush is what surfaces an overlap: the exclusion constraint is evaluated on
        INSERT, not deferred, so a conflicting room is refused here rather than at commit.
        """
        self._session.add(booking_room)
        self._session.flush()
        self._session.refresh(booking_room)
        return booking_room

    def add_nights(self, nights: list[BookingRoomNight]) -> None:
        """Stage the priced nights for one allocation and flush.

        No commit: the night-completeness trigger is DEFERRED, so it judges the final state
        at COMMIT. The service holds that boundary.
        """
        self._session.add_all(nights)
        self._session.flush()

    def apply_changes(self, booking: Booking, changes: dict[str, Any]) -> Booking:
        """Apply a partial update to the header and flush it.

        A status change propagates to ``booking_rooms.booking_status`` through the composite
        foreign key's ``ON UPDATE CASCADE``, which re-evaluates the exclusion constraint in
        the same statement -- so confirming a booking whose room is already taken fails here.
        """
        for field, value in changes.items():
            setattr(booking, field, value)
        self._session.flush()
        self._session.refresh(booking)
        return booking

    def delete(self, booking: Booking) -> None:
        """Delete with a Core statement, so the database's policies are authoritative.

        ``booking_rooms`` and their nights cascade away with the booking; ``payments``
        RESTRICT it. Using ``session.delete()`` would let the ORM apply its own nullify pass
        first and pre-empt both.
        """
        self._session.execute(sql_delete(Booking).where(Booking.id == booking.id))
        self._session.flush()
        self._session.expunge(booking)

    # --- reads --------------------------------------------------------------------------

    def get_by_hotel_and_public_id(self, hotel_id: int, public_id: uuid.UUID) -> Booking | None:
        """The scoped lookup, with allocations and nights eagerly loaded.

        ``selectinload`` rather than lazy access: rendering a booking always needs its rooms
        and their nights, and letting the response builder walk lazy relationships would emit
        a query per room and per night.
        """
        return self._session.scalars(
            select(Booking)
            .where(Booking.hotel_id == hotel_id, Booking.public_id == public_id)
            .options(selectinload(Booking.booking_rooms).selectinload(BookingRoom.nights_rows))
        ).one_or_none()

    def count_for_hotel(self, hotel_id: int) -> int:
        """Total bookings at one hotel, for the pagination envelope."""
        return (
            self._session.scalar(
                select(func.count()).select_from(Booking).where(Booking.hotel_id == hotel_id)
            )
            or 0
        )

    def list_page_for_hotel(self, hotel_id: int, *, limit: int, offset: int) -> list[Booking]:
        """One page of a hotel's bookings in a stable order.

        Ordered by check-in date descending -- the arrivals view staff actually want -- then
        by internal id, because several bookings share a date and PostgreSQL guarantees no
        order among equal rows. The id is used for ordering only and never leaves.
        """
        return list(
            self._session.scalars(
                select(Booking)
                .where(Booking.hotel_id == hotel_id)
                .order_by(Booking.check_in_date.desc(), Booking.id.asc())
                .limit(limit)
                .offset(offset)
                .options(
                    # Every relationship the response builder walks is loaded here.
                    # Without `guest` and `room` the listing lazy-loads one row at a
                    # time -- an N+1 the Stage 3B.12 query audit measured at 18 SELECTs
                    # for a 6-row page.
                    selectinload(Booking.guest),
                    selectinload(Booking.booking_rooms).selectinload(BookingRoom.room),
                    selectinload(Booking.booking_rooms).selectinload(BookingRoom.nights_rows),
                )
            ).all()
        )

    def get_room_in_hotel(self, hotel_id: int, room_number: str) -> Room | None:
        """Resolve a physical room by its hotel-unique number.

        Scoped to the hotel, so an allocation can never name another property's room. The
        composite foreign key would refuse it anyway; this makes the failure a clean 404
        instead of an integrity error.
        """
        return self._session.scalars(
            select(Room).where(Room.hotel_id == hotel_id, Room.room_number == room_number)
        ).one_or_none()

    def room_type_codes_for(self, room_ids: list[int]) -> dict[int, str]:
        """Map room id -> room type code, for rendering allocations.

        One query for the whole page rather than a lazy hop per allocation.
        """
        if not room_ids:
            return {}
        from app.models.room import RoomType

        # .tuples() types the rows as (int, str); .all() materialises them. Without .all()
        # the result is still a lazy Result, and dict() would try to read it as a mapping --
        # which type-checks cleanly and fails only at runtime.
        rows = (
            self._session.execute(
                select(Room.id, RoomType.code)
                .join(RoomType, RoomType.id == Room.room_type_id)
                .where(Room.id.in_(room_ids))
            )
            .tuples()
            .all()
        )
        return dict(rows)


__all__ = ["BookingRepository"]
