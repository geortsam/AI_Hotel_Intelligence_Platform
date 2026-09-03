"""Booking business logic and unit-of-work boundaries.

This service owns the most constraint-dense transaction in the platform. Four database rules
govern it, and **all four remain the database's to enforce**:

1. **Room overlap** -- the partial GiST ``EXCLUDE`` on ``booking_rooms``. There is no
   check-then-insert here: the constraint is evaluated on INSERT and is race-free, which an
   application query never is. It fires only for ``confirmed``/``checked_in``, so a
   ``pending`` booking holds no inventory and two may coexist on one room.

2. **Night completeness** -- the ``DEFERRABLE INITIALLY DEFERRED`` constraint trigger. It is
   why creation is one atomic operation: the allocation and its nights must both exist when
   the transaction commits, and each HTTP request is one transaction.

3. **Cross-hotel integrity** -- composite foreign keys carrying ``hotel_id``. The guest and
   every room are resolved through the hotel in the URL first, so the ids written can only
   belong to it.

4. **Cancellation consistency** -- ``ck_bookings_cancellation_consistent`` is a
   biconditional, so ``cancelled_at`` is set and cleared in step with ``status`` here rather
   than accepted from a client.

Knows the domain; knows no SQL and no HTTP.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    SQLSTATE_NOT_NULL_VIOLATION,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    NotFoundError,
    sqlstate_of,
)
from app.models.booking import Booking, BookingRoom, BookingRoomNight
from app.models.hotel import Hotel
from app.repositories.booking import BookingRepository
from app.repositories.guest import GuestRepository
from app.schemas.booking import (
    BookingCreate,
    BookingResponse,
    BookingRoomNightResponse,
    BookingRoomResponse,
    BookingUpdate,
)
from app.schemas.common import Page
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: SQLSTATE 23P01. Raised only by an EXCLUDE constraint, which in this schema means exactly
#: one thing: the room is already held for overlapping dates by an active booking.
SQLSTATE_EXCLUSION_VIOLATION = "23P01"

#: Constraint names the service reports on precisely. Taken from diagnostics rather than by
#: parsing the driver message, which carries row values.
OVERLAP_CONSTRAINT = "excl_booking_rooms_room_no_overlap"
REFERENCE_CONSTRAINT = "uq_bookings_hotel_id_reference"


def _constraint_name(exc: IntegrityError) -> str | None:
    """The violated constraint's NAME from the driver diagnostics, never its message."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None


def _is_night_completeness_failure(exc: IntegrityError) -> bool:
    """Whether this is the deferred night-completeness trigger firing at COMMIT.

    The trigger raises with ERRCODE ``integrity_constraint_violation`` (class 23) and a
    message naming night rows. It has no constraint name, so it is identified by its own
    wording -- the only place in the project where a message is inspected, and it is our
    message, raised by our migration, not the driver's rendering of user data.
    """
    return "night row" in str(getattr(exc, "orig", "")).lower()


class BookingService:
    """Domain operations on bookings, always within one hotel."""

    def __init__(
        self,
        session: Session,
        repository: BookingRepository,
        guests: GuestRepository,
        scope: HotelScopeResolver,
    ) -> None:
        self._session = session
        self._repository = repository
        self._guests = guests
        self._scope = scope

    # --- reads --------------------------------------------------------------------------

    def get(self, hotel_public_id: uuid.UUID, booking_public_id: uuid.UUID) -> BookingResponse:
        """Return one booking belonging to this hotel, or raise 404."""
        hotel = self._scope.require_hotel(hotel_public_id)
        return self._to_response(self._require_booking(hotel, booking_public_id), hotel)

    def list(
        self, hotel_public_id: uuid.UUID, *, page: int, page_size: int
    ) -> Page[BookingResponse]:
        """One page of this hotel's bookings, most recent arrival first."""
        hotel = self._scope.require_hotel(hotel_public_id)
        total = self._repository.count_for_hotel(hotel.id)
        rows = self._repository.list_page_for_hotel(
            hotel.id, limit=page_size, offset=(page - 1) * page_size
        )
        # One lookup for the whole page. Letting _to_response do its own would issue a query
        # per booking -- correct, but N+1.
        type_codes = self._repository.room_type_codes_for(
            [allocation.room_id for row in rows for allocation in row.booking_rooms]
        )
        return Page.build(
            items=[self._to_response(row, hotel, type_codes) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    # --- writes -------------------------------------------------------------------------

    def create(self, hotel_public_id: uuid.UUID, payload: BookingCreate) -> BookingResponse:
        """Create a booking with its allocations and priced nights, atomically.

        One transaction covers the header, every allocation and every night, because the
        deferred trigger judges the whole thing at COMMIT. An allocation is legitimately
        incomplete part-way through -- that is exactly what deferring the trigger buys, and
        it is why this cannot be split across requests.

        No availability pre-check: the exclusion constraint decides, and it cannot be raced.
        """
        hotel = self._scope.require_hotel(hotel_public_id)

        guest = self._guests.get_by_hotel_and_public_id(hotel.id, payload.guest_public_id)
        if guest is None:
            # Scoped lookup: a guest of another property is *not found*, not forbidden.
            raise NotFoundError("Guest not found for this hotel.")

        # Rooms are resolved before anything is written, so an unknown room number is a clean
        # 404 rather than a half-built transaction rolled back.
        rooms = {}
        for room_input in payload.rooms:
            room = self._repository.get_room_in_hotel(hotel.id, room_input.room_number)
            if room is None:
                raise NotFoundError(f"Room {room_input.room_number!r} not found for this hotel.")
            rooms[room_input.room_number] = room

        booking = Booking(
            hotel_id=hotel.id,
            guest_id=guest.id,
            reference=payload.reference,
            check_in_date=payload.check_in_date,
            check_out_date=payload.check_out_date,
            status=payload.status,
            adults=payload.adults,
            children=payload.children,
            source=payload.source,
            channel_reference=payload.channel_reference,
            total_amount=payload.total_amount,
            currency=payload.currency,
            special_requests=payload.special_requests,
            cancelled_at=self._cancelled_at_for(payload.status),
        )

        try:
            created = self._repository.add_booking(booking)

            for room_input in payload.rooms:
                room = rooms[room_input.room_number]
                allocation = self._repository.add_room(
                    BookingRoom(
                        booking_id=created.id,
                        room_id=room.id,
                        hotel_id=hotel.id,
                        # Mirrors, kept honest by the composite FK's ON UPDATE CASCADE.
                        check_in_date=created.check_in_date,
                        check_out_date=created.check_out_date,
                        booking_status=created.status,
                        adults=room_input.adults,
                        children=room_input.children,
                        guest_name=room_input.guest_name,
                    )
                )
                self._repository.add_nights(
                    [
                        BookingRoomNight(
                            booking_room_id=allocation.id,
                            hotel_id=hotel.id,
                            check_in_date=created.check_in_date,
                            check_out_date=created.check_out_date,
                            stay_date=night.stay_date,
                            rate=night.rate,
                            rate_plan_code=night.rate_plan_code,
                            is_complimentary=night.is_complimentary,
                        )
                        for night in room_input.nights
                    ]
                )

            # The deferred trigger fires HERE, judging the completed aggregate.
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc, reference=payload.reference) from exc

        return self._to_response(self._require_booking(hotel, created.public_id), hotel)

    def update(
        self,
        hotel_public_id: uuid.UUID,
        booking_public_id: uuid.UUID,
        payload: BookingUpdate,
    ) -> BookingResponse:
        """Apply a partial update and commit.

        A status change is the interesting case: it cascades to every allocation through
        ``ON UPDATE CASCADE`` and re-evaluates the exclusion constraint, so confirming a
        booking whose room has since been taken is refused by the database.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        booking = self._require_booking(hotel, booking_public_id)
        changes: dict[str, Any] = payload.model_dump(exclude_unset=True)

        if not changes:
            return self._to_response(booking, hotel)

        if "status" in changes:
            # ck_bookings_cancellation_consistent is a biconditional: the timestamp must
            # appear exactly when the status is 'cancelled' and vanish otherwise. Derived
            # here rather than accepted from the client, so the two cannot disagree.
            changes["cancelled_at"] = self._cancelled_at_for(
                changes["status"], existing=booking.cancelled_at
            )

        try:
            self._repository.apply_changes(booking, changes)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._to_response(self._require_booking(hotel, booking_public_id), hotel)

    def delete(self, hotel_public_id: uuid.UUID, booking_public_id: uuid.UUID) -> None:
        """Delete a booking and commit, honouring the database's policies.

        Allocations and their nights cascade away with it. What blocks the delete is
        everything downstream of the booking, and -- as with guests -- for two different
        reasons:

        * ``payments`` is ``ON DELETE RESTRICT`` -> 23503/23001.
        * ``revenue`` and ``reviews`` are ``ON DELETE SET NULL`` over composite keys whose
          ``hotel_id`` is NOT NULL, so those policies cannot fire and surface as 23502
          instead. The declared SET NULL is unreachable in this schema.

        No cascade is invented for any of them.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        booking = self._require_booking(hotel, booking_public_id)
        try:
            self._repository.delete(booking)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            state = sqlstate_of(exc)
            if state in SQLSTATE_DEPENDENCY_VIOLATIONS or state == SQLSTATE_NOT_NULL_VIOLATION:
                raise ConflictError(
                    "This booking cannot be deleted because payments, revenue or reviews "
                    "still reference it. Remove those records first, or cancel the booking "
                    "instead."
                ) from exc
            raise self._translate(exc) from exc

    # --- internals ----------------------------------------------------------------------

    @staticmethod
    def _cancelled_at_for(
        status: str, *, existing: dt.datetime | None = None
    ) -> dt.datetime | None:
        """Keep ``cancelled_at`` in step with ``status``, as the CHECK demands."""
        if status == "cancelled":
            return existing or dt.datetime.now(dt.UTC)
        return None

    def _require_booking(self, hotel: Hotel, booking_public_id: uuid.UUID) -> Booking:
        """Resolve a booking **within this hotel**, or raise 404."""
        booking = self._repository.get_by_hotel_and_public_id(hotel.id, booking_public_id)
        if booking is None:
            raise NotFoundError("Booking not found for this hotel.")
        return booking

    def _to_response(
        self, booking: Booking, hotel: Hotel, type_codes: dict[int, str] | None = None
    ) -> BookingResponse:
        """Build the response from rows already loaded, resolving parents' public ids.

        ``type_codes`` lets a caller rendering a whole page resolve room-type codes once for
        every row rather than once per row. Omitted, it is resolved for this booking alone,
        which is what the single-booking paths want.
        """
        allocations = sorted(booking.booking_rooms, key=lambda a: a.id)
        if type_codes is None:
            type_codes = self._repository.room_type_codes_for([a.room_id for a in allocations])

        rooms = [
            BookingRoomResponse(
                room_number=allocation.room.room_number,
                room_type_code=type_codes.get(allocation.room_id, ""),
                adults=allocation.adults,
                children=allocation.children,
                guest_name=allocation.guest_name,
                nights=allocation.nights,
                nightly_rates=[
                    BookingRoomNightResponse.model_validate(night)
                    for night in sorted(allocation.nights_rows, key=lambda n: n.stay_date)
                ],
            )
            for allocation in allocations
        ]

        return BookingResponse.model_validate(
            {
                "hotel_public_id": hotel.public_id,
                "public_id": booking.public_id,
                "guest_public_id": booking.guest.public_id,
                "reference": booking.reference,
                "check_in_date": booking.check_in_date,
                "check_out_date": booking.check_out_date,
                "status": booking.status,
                "adults": booking.adults,
                "children": booking.children,
                "source": booking.source,
                "channel_reference": booking.channel_reference,
                "total_amount": booking.total_amount,
                "currency": booking.currency,
                "special_requests": booking.special_requests,
                "cancelled_at": booking.cancelled_at,
                "cancellation_reason": booking.cancellation_reason,
                "booked_at": booking.booked_at,
                "created_at": booking.created_at,
                "updated_at": booking.updated_at,
                "rooms": rooms,
            }
        )

    def _translate(self, exc: IntegrityError, *, reference: str | None = None) -> Exception:
        """Turn a database integrity error into a domain error, leaking nothing.

        No ``exc_info``: a booking's driver message carries the guest link and the room and
        date values. The SQLSTATE and constraint name are enough to decide what to say.
        """
        state = sqlstate_of(exc)
        constraint = _constraint_name(exc)
        logger.warning("Booking integrity error (sqlstate=%s, constraint=%s)", state, constraint)

        if state == SQLSTATE_EXCLUSION_VIOLATION or constraint == OVERLAP_CONSTRAINT:
            return ConflictError(
                "One of the requested rooms is already booked for overlapping dates. "
                "Choose a different room or different dates."
            )
        if _is_night_completeness_failure(exc):
            return ConflictError(
                "Every allocated room must be priced for exactly the nights of the stay."
            )
        if state == SQLSTATE_UNIQUE_VIOLATION:
            if constraint == REFERENCE_CONSTRAINT and reference:
                return ConflictError(
                    f"A booking with reference {reference!r} already exists at this hotel."
                )
            return ConflictError("That value is already taken by another booking.")
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a booking constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS or state == SQLSTATE_NOT_NULL_VIOLATION:
            return ConflictError("This booking is still referenced by other records.")
        return ConflictError("The request conflicts with the current state of the database.")


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "OVERLAP_CONSTRAINT",
    "SQLSTATE_EXCLUSION_VIOLATION",
    "BookingService",
]
