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

import decimal
import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, func, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.orm import Session, joinedload, selectinload

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

    def lock_for_update(self, booking: Booking) -> Booking:
        """Re-read this booking under a row lock, refreshing it in place.

        Used by both mutations that must not interleave: a status transition (Stage 4.5.7)
        and a stay modification (Stage 4.5.10). Both read the booking, decide from what they
        read, and write -- which is only safe if nothing else may write between the reading
        and the deciding.

        ``SELECT ... FOR UPDATE`` on the header row. Two requests that both read a booking as
        ``confirmed`` and then transition it independently would otherwise each validate
        against the same stale status and both commit -- measured, not assumed: without this,
        ``confirmed -> checked_in`` and ``confirmed -> no_show`` racing produced a final
        ``no_show`` that no one ever validated the ``checked_in`` predecessor of.

        With the lock, the second reader blocks until the first commits and then sees the new
        status, because READ COMMITTED re-evaluates a locked row after acquiring it. The
        service therefore always validates against a status no other transaction can be
        holding open.

        ``populate_existing`` matters: the instance is already in the identity map, and
        without it SQLAlchemy would hand back the stale attributes it loaded earlier and the
        lock would protect nothing anyone reads.

        The same pattern the membership service already uses for the owner-count invariant.
        No commit here -- the caller's transaction owns the lock until it ends.
        """
        return self._session.scalars(
            select(Booking)
            .where(Booking.id == booking.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()

    def refresh_allocations(self, booking_id: int) -> list[BookingRoom]:
        """Re-read a booking's allocations and nights from the database.

        Stage 4.5.27, and it exists because of a cascade the ORM cannot see. An
        in-house extension updates ``bookings.check_out_date`` and PostgreSQL
        propagates it down two composite foreign keys, rewriting every allocation
        and every night row inside that one statement. SQLAlchemy issued no UPDATE
        against those tables, so it has no reason to think the objects it is
        holding are stale -- and ``booking_rooms.nights`` is a GENERATED column,
        so the stalest value of all is the night count a response would report.

        ``populate_existing`` is the whole method: without it the identity map
        hands back the attributes it loaded before the cascade, and an extended
        booking would render its new nightly rows against its old night count.

        The room is eagerly loaded alongside the nights because the response builder
        reads ``allocation.room.room_number``, and reaching it lazily would be a query
        per allocated room -- so a party of four would cost more statements than a
        single guest for a payload of the same shape.

        No commit. The caller's transaction still owns the change.
        """
        return list(
            self._session.scalars(
                select(BookingRoom)
                .where(BookingRoom.booking_id == booking_id)
                .options(
                    selectinload(BookingRoom.nights_rows),
                    selectinload(BookingRoom.room),
                )
                .execution_options(populate_existing=True)
            ).unique()
        )

    def delete_allocations(self, booking_id: int) -> None:
        """Remove every allocation of one booking, and flush.

        The nights go with them: ``fk_brn_booking_room_id_hotel_id_booking_rooms`` is
        ON DELETE CASCADE, so one statement clears both levels.

        **Deleting before re-inserting is what solves the self-conflict problem.** A booking
        moving from ``[Sep 10, Sep 14)`` to ``[Sep 11, Sep 15)`` in the same room overlaps
        ITSELF, and an exclusion constraint cannot tell that the old row is about to go away.
        Clearing first means the new rows are only ever compared against OTHER bookings --
        no self-exclusion predicate, no temporary weakening of the constraint, and no window
        in which another transaction sees the room as free, because this all happens inside
        one transaction holding the booking's row lock.

        A Core statement rather than ``session.delete()`` per row, for the same reason
        :meth:`delete` uses one: the database's cascades stay authoritative and the ORM does
        not get to run its own nullify pass first.
        """
        self._session.execute(sql_delete(BookingRoom).where(BookingRoom.booking_id == booking_id))
        self._session.flush()

    def delete(self, booking: Booking) -> int:
        """Delete with a Core statement, returning how many rows went.

        Core rather than ``session.delete()`` so the database's policies are authoritative:
        ``booking_rooms`` and their nights cascade away with the booking and ``payments``
        RESTRICT it, where the ORM would apply its own nullify pass first and pre-empt both.

        **The row count is returned because zero is a real answer** (Stage 4.5.15). Two
        concurrent deletions of one booking both resolve it, and the second blocks on the
        first's row lock; when it wakes the row is gone and its ``DELETE`` removes nothing at
        all -- silently, because deleting no rows is not an error in SQL. The service turns
        that into the 404 it is, which is also what stops a second audit event being recorded
        for a booking this transaction did not delete.

        The instance is expunged afterwards, so every value a caller still needs -- the public
        id above all -- must be read BEFORE this is called.
        """
        # `Session.execute` is typed as returning `Result`; a DML statement really returns a
        # `CursorResult`, which is the type that carries `rowcount`. Narrowed rather than
        # ignored, so the cast documents why it is safe instead of silencing the checker.
        result = cast(
            "CursorResult[Any]",
            self._session.execute(sql_delete(Booking).where(Booking.id == booking.id)),
        )
        self._session.flush()
        self._session.expunge(booking)
        return int(result.rowcount)

    # --- reads --------------------------------------------------------------------------

    def get_by_hotel_and_public_id(self, hotel_id: int, public_id: uuid.UUID) -> Booking | None:
        """The scoped lookup, with allocations and nights eagerly loaded.

        ``selectinload`` rather than lazy access: rendering a booking always needs its
        rooms and their nights, and letting the response builder walk lazy relationships
        would emit a query per room and per night.

        Deliberately NOT the loader the render paths use -- see
        :meth:`get_for_response`. This one serves the write paths, which resolve their
        own rooms and would pay a second query for eager loading them.

        **The guest is joined, not selected in** (Stage 4.5.30). ``Booking.guest`` is
        many-to-one, so ``joinedload`` folds it into the SELECT that is already being
        issued and costs NOTHING -- where ``selectinload`` would emit a second
        statement for a single row. That difference is why the guest could be added
        here after Stage 4.5.28 measured the naive version as a net loss: the write
        paths render after committing, and reaching a cold ``guest`` lazily cost them
        one statement each.

        Row duplication is not a risk for a many-to-one join, and that is the whole
        reason the collections below stay on ``selectinload``: joining a COLLECTION
        multiplies the parent row once per child.
        """
        return self._session.scalars(
            select(Booking)
            .where(Booking.hotel_id == hotel_id, Booking.public_id == public_id)
            .options(
                joinedload(Booking.guest),
                selectinload(Booking.booking_rooms).selectinload(BookingRoom.nights_rows),
            )
        ).one_or_none()

    def get_for_response(self, hotel_id: int, public_id: uuid.UUID) -> Booking | None:
        """The same scoped lookup, loading everything the RESPONSE BUILDER walks.

        Stage 4.5.28, and a second method rather than more options on the first,
        because the measurement said the two callers want different things.

        ``_to_response`` reaches four relationships: the allocations, each allocation's
        nights, each allocation's ``room`` (for its number) and the booking's ``guest``
        (for its public id). A caller that renders a booking it did not just write has
        none of them warm, so reaching them lazily costs one SELECT per allocated room
        plus one for the guest -- measured at 1, 2 and 4 extra statements for 1, 2 and 4
        rooms before this stage.

        **A write path is the opposite case and must NOT use this.** After creating or
        modifying a stay the allocations and their rooms are already in the identity
        map, so eager loading re-fetches rows the session is holding: measured at two
        extra statements on create, modify-stay and extend, for nothing. Which loader a
        path wants is a fact about that path, so it is the path that says.

        The option list is the one :meth:`list_page_for_hotel` has carried since the
        Stage 3B.12 query audit -- the listing feeds the SAME builder and was simply
        audited first. Nothing here changes which rows are visible or to whom: the
        tenant predicate is identical, and eager loading changes when rows are fetched,
        never which.
        """
        return self._session.scalars(
            select(Booking)
            .where(Booking.hotel_id == hotel_id, Booking.public_id == public_id)
            .options(
                joinedload(Booking.guest),
                selectinload(Booking.booking_rooms).joinedload(BookingRoom.room),
                selectinload(Booking.booking_rooms).selectinload(BookingRoom.nights_rows),
            )
        ).one_or_none()

    def accommodation_total(self, booking_id: int) -> decimal.Decimal:
        """The booking's server-derived accommodation value: the sum of its nightly rates.

        ``booking_room_nights`` is the atomic financial unit of the platform, and this sum is
        the only monetary figure for a booking that the server itself produces. Summed in the
        database rather than by walking ``booking_rooms -> nights_rows`` in Python, which
        would be a query per room and a query per night for a number nobody needs the rows of.

        Completeness is not assumed: ``trg_booking_room_nights_complete`` is a DEFERRABLE
        constraint trigger asserting that each allocation has exactly one row per night of its
        stay, so this sum cannot silently omit a night that was never priced.

        ``0.00`` for a booking with no allocations, which is a real answer rather than a
        missing one.
        """
        total = self._session.scalar(
            select(func.coalesce(func.sum(BookingRoomNight.rate), 0)).where(
                BookingRoomNight.booking_room_id.in_(
                    select(BookingRoom.id).where(BookingRoom.booking_id == booking_id)
                )
            )
        )
        return decimal.Decimal(total or 0)

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
                    joinedload(Booking.guest),
                    selectinload(Booking.booking_rooms).joinedload(BookingRoom.room),
                    selectinload(Booking.booking_rooms).selectinload(BookingRoom.nights_rows),
                )
            ).all()
        )

    def rooms_in_hotel(self, hotel_id: int, room_numbers: Sequence[str]) -> dict[str, Room]:
        """Resolve every room a payload names, by hotel-unique number, in one query.

        Scoped to the hotel, so an allocation can never name another property's room.
        The composite foreign key would refuse it anyway; resolving here makes the
        failure a clean 404 instead of an integrity error.

        **Batched deliberately** (Stage 4.5.29). This replaced a per-room lookup that
        cost one SELECT for every room in the payload -- measured at 1, 2, 4 and 8
        statements for parties of 1, 2, 4 and 8. A booking names its rooms all at once
        and they are all in one table, so there was never a reason to ask one at a time.

        Returns a mapping keyed by room number, and it is deliberately PARTIAL: a
        number that names no room of this hotel is simply absent. Deciding what a
        missing room means -- which one to name, and in which order -- is the service's
        to make, not a repository's.

        The single-room reader it replaced is gone rather than kept beside it. Leaving
        an N+1-shaped method on the repository is how the N+1 comes back.
        """
        if not room_numbers:
            return {}
        rows = self._session.scalars(
            select(Room).where(Room.hotel_id == hotel_id, Room.room_number.in_(list(room_numbers)))
        ).all()
        return {room.room_number: room for room in rows}

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
