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
import decimal
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    GENERIC_CONFLICT_MESSAGE,
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    SQLSTATE_NOT_NULL_VIOLATION,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    NotFoundError,
    ValidationError,
    constraint_name_of,
    internal_fault,
    is_audit_integrity_failure,
    relation_of,
    sqlstate_of,
)
from app.models.booking import Booking, BookingRoom, BookingRoomNight
from app.models.enums import (
    EXTENDABLE_BOOKING_STATUSES,
    MODIFIABLE_BOOKING_STATUSES,
    TERMINAL_BOOKING_STATUSES,
    AuditAction,
    AuditResourceType,
    is_booking_transition_allowed,
)
from app.models.hotel import Hotel
from app.models.room import Room
from app.repositories.booking import BookingRepository
from app.repositories.guest import GuestRepository
from app.repositories.payment import PaymentRepository
from app.schemas.booking import (
    BookingCreate,
    BookingResponse,
    BookingRoomNightResponse,
    BookingRoomResponse,
    BookingUpdate,
    RoomInputBase,
    StayExtension,
    StayModification,
    StayModificationResponse,
    StayRepricing,
)
from app.schemas.common import Page
from app.services.audit import AuditTrail
from app.services.pricing import NightRequest, PricingService
from app.services.repricing import RepricingOutcome, RepricingPolicy
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: Declared at module scope on purpose: inside the service, ``list`` is a method name, so a
#: ``list[str]`` annotation there resolves to the method rather than to the builtin.
type ChangedFields = list[str]

#: The same trap, and the same remedy: `list[dt.date]` written inside the service would
#: resolve to the `list` METHOD rather than to the builtin.
type ExtensionNights = list[dt.date]

#: SQLSTATE 23P01. Raised only by an EXCLUDE constraint, which in this schema means exactly
#: one thing: the room is already held for overlapping dates by an active booking.
SQLSTATE_EXCLUSION_VIOLATION = "23P01"

#: Constraint names the service reports on precisely. Taken from diagnostics rather than by
#: parsing the driver message, which carries row values.
OVERLAP_CONSTRAINT = "excl_booking_rooms_room_no_overlap"

#: The most nights one in-house extension may add (Stage 4.5.27).
#:
#: Mirrors ``MAX_STAY_NIGHTS`` in the availability service, and for a sharper reason. The
#: extension payload is a single DATE, so unlike every other booking write there is no
#: list whose length bounds the work: a departure date in the next century would ask the
#: server to price and insert a million night rows from four bytes of input. The query
#: COUNT would still be bounded -- that is not the same thing as the work being bounded.
#:
#: Bounds one extension, not a stay: a guest who genuinely stays longer extends again,
#: and each such request is a separately audited act. That is the honest reading of what
#: this limit protects.
MAX_EXTENSION_NIGHTS = 366
REFERENCE_CONSTRAINT = "uq_bookings_hotel_id_reference"

#: The relations that can legitimately block a booking's deletion (Stage 4.5.16).
#:
#: Measured against PostgreSQL 18.6 rather than reasoned about, because the three do not fail
#: the same way and the difference decides how they can be recognised::
#:
#:     payments  ON DELETE RESTRICT   -> 23001, constraint fk_payments_..., table payments
#:     revenue   composite SET NULL   -> 23502, constraint None,            table revenue
#:     reviews   composite SET NULL   -> 23502, constraint None,            table reviews
#:
#: Two of the three carry NO constraint name at all: a not-null violation reports the column
#: and the table instead. So the RELATION is the discriminator -- it is the one field present
#: in every case -- and matching on the constraint name alone would leave both SET NULL paths
#: unattributable.
#:
#: What this excludes is the point. Since Stage 4.5.12 a booking deletion also writes an audit
#: event in the same transaction, so ``audit_events`` can fail inside the same ``except``; it
#: is not in this set, so it can no longer be reported as a booking dependency.
BOOKING_DEPENDENT_RELATIONS = frozenset({"payments", "revenue", "reviews"})


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
        audit: AuditTrail,
        pricing: PricingService,
        payments: PaymentRepository,
    ) -> None:
        self._session = session
        self._repository = repository
        self._guests = guests
        self._scope = scope
        # Stage 4.5.23. Required, not optional, for the same reason the audit
        # trail is: a booking service constructible without one would be a
        # booking service that could write a price the server never calculated.
        self._pricing = pricing
        # Stage 4.5.24. Read-only here: repricing needs to know what has been
        # collected in order to say what is refundable, and it never writes to
        # the ledger -- settling a difference stays a separate, deliberate act.
        self._payments = payments
        self._repricing = RepricingPolicy()
        # Stage 4.5.12. Required, not optional: a booking service that could be constructed
        # without one would be a booking service that could write unaudited.
        self._audit = audit

    # --- reads --------------------------------------------------------------------------

    def get(self, hotel_public_id: uuid.UUID, booking_public_id: uuid.UUID) -> BookingResponse:
        """Return one booking belonging to this hotel, or raise 404."""
        hotel = self._scope.require_hotel(hotel_public_id)
        return self._to_response(self._require_booking_to_render(hotel, booking_public_id), hotel)

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

        **The rates are the server's** (Stage 4.5.23). The payload carries no
        amount to carry: every night is priced from the room type's configured
        base price before anything is written, so a currency mismatch or an
        unpriceable room is a clean refusal rather than a half-built transaction.
        """
        hotel = self._scope.require_hotel(hotel_public_id)

        guest = self._guests.get_by_hotel_and_public_id(hotel.id, payload.guest_public_id)
        if guest is None:
            # Scoped lookup: a guest of another property is *not found*, not forbidden.
            raise NotFoundError("Guest not found for this hotel.")

        # Rooms are resolved before anything is written, so an unknown room number is a clean
        # 404 rather than a half-built transaction rolled back.
        rooms = self._resolve_rooms(hotel, payload.rooms)

        # Priced before the write, in ONE query for the whole booking however many
        # rooms and nights it has. The quotes are keyed by room id and each holds
        # one rate per night, in the order the payload listed them.
        quotes = self._pricing.quote_rooms(
            hotel.id,
            {
                rooms[room_input.room_number].id: [
                    NightRequest(
                        stay_date=night.stay_date,
                        rate_plan_code=night.rate_plan_code,
                        is_complimentary=night.is_complimentary,
                    )
                    for night in room_input.nights
                ]
                for room_input in payload.rooms
            },
            currency=payload.currency,
        )

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
                # ``priced``, not ``room_input.nights``: the amount comes from the
                # quote. This is the line that makes the rate authoritative, and
                # the one a mutation test puts back to prove it.
                priced = quotes[room.id].nights
                self._repository.add_nights(
                    [
                        BookingRoomNight(
                            booking_room_id=allocation.id,
                            hotel_id=hotel.id,
                            check_in_date=created.check_in_date,
                            check_out_date=created.check_out_date,
                            stay_date=night.stay_date,
                            rate=night.amount,
                            rate_plan_code=night.rate_plan_code,
                            is_complimentary=night.is_complimentary,
                        )
                        for night in priced
                    ]
                )

            self._audit.record(
                AuditAction.BOOKING_CREATED,
                AuditResourceType.BOOKING,
                str(created.public_id),
                hotel_id=hotel.id,
                details={
                    "reference": created.reference,
                    "status": created.status,
                    "check_in_date": created.check_in_date.isoformat(),
                    "check_out_date": created.check_out_date.isoformat(),
                    "rooms": len(payload.rooms),
                },
            )

            # The deferred trigger fires HERE, judging the completed aggregate -- and it
            # judges the audit row with it. Both are staged; one COMMIT decides both.
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
            # Re-read through the render loader: `booking` above came from the
            # write reader and would reach its rooms one lazy query at a time.
            return self._to_response(
                self._require_booking_to_render(hotel, booking_public_id), hotel
            )

        # Read under the lock below, and only meaningful for a status change. `None` means
        # "this update did not touch the status", which is what decides whether an event is
        # recorded at all -- an occupancy or source edit is not a lifecycle change.
        previous_status: str | None = None

        if "status" in changes:
            # Lock BEFORE reading the status the transition is judged against. Two requests
            # that each read `confirmed` and then transition independently would otherwise
            # both be validated against a status neither of them still holds. The lock is
            # taken only for a status change, so ordinary field edits are unaffected.
            booking = self._repository.lock_for_update(booking)
            self._require_allowed_transition(booking.status, changes["status"])
            if booking.status != changes["status"]:
                # A PATCH asking for the status the booking already holds is permitted and
                # is a no-op (see BOOKING_STATUS_TRANSITIONS on idempotence). Recording it
                # would put "confirmed -> confirmed" in the history, which is a claim that
                # something happened when nothing did.
                previous_status = booking.status

            # ck_bookings_cancellation_consistent is a biconditional: the timestamp must
            # appear exactly when the status is 'cancelled' and vanish otherwise. Derived
            # here rather than accepted from the client, so the two cannot disagree.
            changes["cancelled_at"] = self._cancelled_at_for(
                changes["status"], existing=booking.cancelled_at
            )

        try:
            self._repository.apply_changes(booking, changes)
            if previous_status is not None:
                # AFTER the change is staged, so a status the exclusion constraint refuses on
                # cascade takes the audit row down with it -- there is no ordering in which a
                # refused transition leaves an event saying it happened.
                self._audit.record(
                    AuditAction.BOOKING_STATUS_CHANGED,
                    AuditResourceType.BOOKING,
                    str(booking.public_id),
                    hotel_id=hotel.id,
                    details={"old_status": previous_status, "new_status": changes["status"]},
                )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._to_response(self._require_booking_to_render(hotel, booking_public_id), hotel)

    def modify_stay(
        self,
        hotel_public_id: uuid.UUID,
        booking_public_id: uuid.UUID,
        payload: StayModification,
    ) -> StayModificationResponse:
        """Replace a booking's stay -- dates and allocation -- atomically.

        One transaction, in this order, and the order is the design:

        1. resolve the hotel and the booking, so a foreign booking is a 404 before anything
           else is considered;
        2. LOCK the booking row, so two modifications of the same booking serialise and the
           second decides against what the first actually committed;
        3. check the status against `MODIFIABLE_BOOKING_STATUSES`, under the lock;
        4. resolve every room number, so an unknown room is a clean 404 rather than a
           half-built transaction;
        5. DELETE the existing allocations -- see `delete_allocations` for why this comes
           before anything is written, and why it is what makes a booking able to move
           within its own room without conflicting with itself;
        6. move the header's dates;
        7. re-insert the allocations and their nights.

        The exclusion constraint is untouched and remains the authority: steps 5-7 do not
        weaken it, defer it, or work around it -- they simply stop the booking's own soon-to-
        be-deleted rows from being what it collides with. Another booking's overlapping
        allocation still refuses this one, at INSERT, as a 409.

        The night-completeness trigger is deferred, so the intermediate state after step 5 --
        a booking with no allocations at all -- is legal until COMMIT, which is exactly what
        deferring it buys and why this cannot be split across requests.

        **Stage 4.5.24 adds the money to that sequence, and puts it inside the same
        transaction and the same lock.** Between steps 4 and 5 the stay's current value is
        read while the old nights are still there; the new nights are priced by the
        server, as creation prices them; and the difference between the two authoritative
        sums is reported against the payment ledger. The lock taken in step 2 is what
        makes that difference true: it is computed and applied without another
        modification landing in between, so two concurrent repricings cannot both measure
        from the same starting point.

        Nothing is charged and nothing is refunded. The consequence is calculated and
        returned; settling it is a separate act through the payment endpoints.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        booking = self._require_booking(hotel, booking_public_id)

        booking = self._repository.lock_for_update(booking)
        self._require_modifiable(booking.status)

        rooms = self._resolve_rooms(hotel, payload.rooms)

        # Computed BEFORE anything is written, while the old stay is still readable. A safe
        # summary of WHAT moved -- not the payload, and not the before-and-after of every
        # nightly rate, which would put the whole commercial history of the booking into a
        # table nobody prunes.
        changed_fields = self._changed_stay_fields(booking, payload, rooms)

        # The stay's value BEFORE anything is touched, read under the lock while the old
        # night rows are still there. After step 5 they are gone and this is unknowable.
        previous_total = self._repository.accommodation_total(booking.id)

        # A booking whose ledger is in another currency cannot have its financial
        # consequence stated at all -- the figures would not add. Refused here, before
        # any row moves, on the same terms reconciliation refuses to report one.
        self._require_single_currency(booking.id, booking.currency)

        # Priced by the server, exactly as creation prices. One query for the whole
        # booking, and a currency mismatch or unknown room raises before the delete.
        quotes = self._pricing.quote_rooms(
            hotel.id,
            {
                rooms[room_input.room_number].id: [
                    NightRequest(
                        stay_date=night.stay_date,
                        rate_plan_code=night.rate_plan_code,
                        is_complimentary=night.is_complimentary,
                    )
                    for night in room_input.nights
                ]
                for room_input in payload.rooms
            },
            currency=booking.currency,
        )

        charged, refunded = self._payments.ledger_totals_for_booking(booking.id)
        new_total = sum(
            (quotes[rooms[room_input.room_number].id].total for room_input in payload.rooms),
            decimal.Decimal("0.00"),
        )
        outcome = self._repricing.outcome(
            currency=booking.currency,
            previous_total=previous_total,
            new_total=new_total,
            net_paid=charged - refunded,
        )

        try:
            self._repository.delete_allocations(booking.id)
            self._repository.apply_changes(
                booking,
                {
                    "check_in_date": payload.check_in_date,
                    "check_out_date": payload.check_out_date,
                },
            )

            for room_input in payload.rooms:
                allocation = self._repository.add_room(
                    BookingRoom(
                        booking_id=booking.id,
                        room_id=rooms[room_input.room_number].id,
                        hotel_id=hotel.id,
                        check_in_date=booking.check_in_date,
                        check_out_date=booking.check_out_date,
                        booking_status=booking.status,
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
                            check_in_date=booking.check_in_date,
                            check_out_date=booking.check_out_date,
                            stay_date=night.stay_date,
                            rate=night.amount,
                            rate_plan_code=night.rate_plan_code,
                            is_complimentary=night.is_complimentary,
                        )
                        for night in quotes[rooms[room_input.room_number].id].nights
                    ]
                )

            self._audit.record(
                AuditAction.BOOKING_STAY_MODIFIED,
                AuditResourceType.BOOKING,
                str(booking.public_id),
                hotel_id=hotel.id,
                details={
                    "changed_fields": changed_fields,
                    "check_in_date": payload.check_in_date.isoformat(),
                    "check_out_date": payload.check_out_date.isoformat(),
                    "rooms": len(payload.rooms),
                    # Stage 4.5.24. Decimal STRINGS, never floats, so the audited figure
                    # is the figure that was calculated. No payment detail appears here:
                    # what was charged or refunded belongs to the payment events.
                    "previous_amount": str(outcome.previous_total),
                    "new_amount": str(outcome.new_total),
                    "difference": str(outcome.difference),
                    "currency": outcome.currency,
                },
            )

            self._session.commit()
        except IntegrityError as exc:
            # Rolls back the WHOLE thing: the deleted allocations come back, the dates revert,
            # and the audit event goes with them. There is no state in which a booking has new
            # dates and old nights, and none in which a refused modification is recorded as a
            # successful one.
            self._session.rollback()
            raise self._translate(exc) from exc

        return StayModificationResponse(
            booking=self._to_response(self._require_booking(hotel, booking_public_id), hotel),
            repricing=self._render_repricing(outcome),
        )

    def extend_stay(
        self,
        hotel_public_id: uuid.UUID,
        booking_public_id: uuid.UUID,
        payload: StayExtension,
    ) -> StayModificationResponse:
        """Keep an in-house guest longer: push ``check_out`` outward, and only outward.

        Stage 4.5.27. A guest is in the room. Everything about this operation follows
        from that one fact, and the first consequence is that it is NOT
        :meth:`modify_stay` with a wider status set. That method replaces a stay; here
        the beginning of the stay has already happened, so there is nothing to replace
        and a great deal that must be left exactly as it is.

        **The nights already slept are historical facts.** They are not re-read, not
        re-quoted and not rewritten -- not because a rule forbids it, but because no
        statement here touches them. What a guest was charged on Tuesday cannot change on
        Friday because they decided to stay until Sunday.

        **The extension is one UPDATE, and the database does the rest.** Moving
        ``bookings.check_out_date`` propagates down two composite foreign keys, both
        ``ON UPDATE CASCADE``: to every ``booking_rooms`` row, and through those to every
        ``booking_room_nights`` row. That single statement is also where the EXCLUDE
        constraint is re-evaluated, so an allocation whose widened ``daterange`` now
        collides with another booking is refused right there, by the database, as a 409.

        **So the booking cannot conflict with itself, and no trick is needed to arrange
        it.** :meth:`modify_stay` has to delete its allocations first, because a stay
        MOVING within its own room overlaps its own old rows. An extension does not move
        rows -- it widens them in place -- and an exclusion constraint never compares a
        row with itself. The delete-and-reinsert dance is therefore absent here, which is
        a simplification the schema earned rather than a shortcut taken.

        **Only the added nights are priced**, by the same :class:`PricingService`
        creation uses, in one query for the whole booking however many rooms and however
        many nights it covers. The payload carries no amount, so a client cannot name a
        rate for the nights it is adding any more than it can for the ones already slept.

        The order below is the design, and it is the same discipline
        :meth:`modify_stay` follows:

        1. the hotel, then the booking -- a foreign booking is a 404 before a date is
           even looked at;
        2. LOCK the booking row, and decide nothing until it is held;
        3. under the lock: is it ``checked_in``, and is the new date genuinely later?
           Both are questions about committed state, so both are asked where the answer
           cannot go stale;
        4. still under the lock, read what the stay is worth NOW -- the sum of its
           existing nightly rates, which is the only financial starting point this
           operation is allowed to measure from;
        5. price the added nights;
        6. move the date, insert the added nights, and read the new authoritative sum
           back out of the table rather than assuming it;
        7. audit, and commit.

        Nothing is charged and nothing is refunded. An extension that costs money
        produces a statement that money is owed; settling it stays a separate act
        through the payment endpoints, exactly as Stage 4.5.24 decided.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        booking = self._require_booking(hotel, booking_public_id)

        # Before ANY of the three decisions below. A status read outside the lock, a
        # departure date read outside it, or a starting total read outside it would each
        # be a value another transaction could already have replaced.
        booking = self._repository.lock_for_update(booking)
        self._require_extendable(booking.status)

        previous_check_out = booking.check_out_date
        added = self._extension_nights(previous_check_out, payload.check_out_date)

        # The authoritative accommodation value as it stands, read under the lock. NOT
        # ``bookings.total_amount``: that is the CONTRACTED figure (approved decision 10)
        # and is deliberately not the sum of the nights, so measuring a difference from
        # it would report the size of a discount as the price of an extension.
        previous_total = self._repository.accommodation_total(booking.id)
        self._require_single_currency(booking.id, booking.currency)

        # The rooms the guest is already in. Not re-resolved from a payload, because the
        # payload has no rooms: an extension continues the allocation it found.
        allocations = list(booking.booking_rooms)
        quotes = self._pricing.quote_rooms(
            hotel.id,
            {
                allocation.room_id: [NightRequest(stay_date=day) for day in added]
                for allocation in allocations
            },
            currency=booking.currency,
        )

        charged, refunded = self._payments.ledger_totals_for_booking(booking.id)

        try:
            # One UPDATE. The cascade widens every allocation and every existing night
            # row's stay window, and re-evaluates the exclusion constraint while doing
            # it -- so a room taken by someone else over the added nights fails HERE.
            self._repository.apply_changes(booking, {"check_out_date": payload.check_out_date})

            # Every added night of every room in ONE insert, so the statement count does
            # not grow with the length of the extension or the size of the party.
            self._repository.add_nights(
                [
                    BookingRoomNight(
                        booking_room_id=allocation.id,
                        hotel_id=hotel.id,
                        check_in_date=booking.check_in_date,
                        check_out_date=booking.check_out_date,
                        stay_date=night.stay_date,
                        rate=night.amount,
                        rate_plan_code=night.rate_plan_code,
                        is_complimentary=night.is_complimentary,
                    )
                    for allocation in allocations
                    for night in quotes[allocation.room_id].nights
                ]
            )

            # Read back rather than computed as ``previous_total + added``. The same sum,
            # if and only if the nights already slept were left alone -- which is exactly
            # the invariant worth having the database confirm rather than assert.
            new_total = self._repository.accommodation_total(booking.id)
            outcome = self._repricing.outcome(
                currency=booking.currency,
                previous_total=previous_total,
                new_total=new_total,
                net_paid=charged - refunded,
            )

            self._audit.record(
                AuditAction.BOOKING_STAY_MODIFIED,
                AuditResourceType.BOOKING,
                str(booking.public_id),
                hotel_id=hotel.id,
                details={
                    # Always exactly this one field: an extension is defined by being
                    # unable to change anything else.
                    "changed_fields": ["check_out_date"],
                    "previous_check_out_date": previous_check_out.isoformat(),
                    "check_out_date": payload.check_out_date.isoformat(),
                    # How many nights were ADDED, not how long the stay now is.
                    "nights": len(added),
                    "rooms": len(allocations),
                    # Decimal STRINGS, never floats, so the audited figure is the
                    # figure that was calculated.
                    "previous_amount": str(outcome.previous_total),
                    "new_amount": str(outcome.new_total),
                    "difference": str(outcome.difference),
                    "currency": outcome.currency,
                },
            )

            # The cascade above rewrote rows SQLAlchemy never issued an UPDATE for.
            # Without this the response would report the generated night count as it was
            # before the extension, beside the nightly rows as they are after it.
            self._repository.refresh_allocations(booking.id)
            self._session.commit()
        except IntegrityError as exc:
            # The whole extension goes: the departure date reverts, the added nights
            # vanish, and the audit event goes with them. There is no state in which a
            # booking is longer but unpriced, and none in which a refused extension is
            # recorded as a successful one.
            self._session.rollback()
            raise self._translate(exc) from exc

        return StayModificationResponse(
            booking=self._to_response(self._require_booking(hotel, booking_public_id), hotel),
            repricing=self._render_repricing(outcome),
        )

    @staticmethod
    def _render_repricing(outcome: RepricingOutcome) -> StayRepricing:
        """The financial consequence, field by field.

        Named rather than constructed from the dataclass, so a field added to the internal
        outcome cannot reach a client merely by existing.
        """
        return StayRepricing(
            currency=outcome.currency,
            previous_total=outcome.previous_total,
            new_total=outcome.new_total,
            difference=outcome.difference,
            additional_amount_due=outcome.additional_amount_due,
            refundable_amount=outcome.refundable_amount,
            outstanding_after=outcome.outstanding_after,
            adjustment=outcome.adjustment,
        )

    def _require_single_currency(self, booking_id: int, currency: str) -> None:
        """Refuse to reprice a booking whose ledger is in more than one currency.

        The same refusal :class:`ReconciliationService` makes, for the same reason and in
        the same words: a difference measured against a ledger that adds EUR to USD is a
        number that means nothing. No conversion is performed and none is introduced.
        """
        currencies = self._payments.currencies_for_booking(booking_id)
        foreign = sorted(code for code in currencies if code != currency)
        if not foreign:
            return
        listed = foreign[0]
        for code in foreign[1:]:
            listed = f"{listed}, {code}"
        raise ConflictError(
            "This booking cannot be repriced: it has payments in "
            f"{listed} but the booking is in {currency}. No conversion is performed."
        )

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

        **The deletion is audited** (Stage 4.5.15), and it is the only booking operation whose
        audit event outlives its subject: cancelling leaves a row to inspect, deleting does
        not. The event is recorded on this transaction, between the delete and the commit, so
        a refused deletion records nothing and a failed commit takes the record with it.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        booking = self._require_booking(hotel, booking_public_id)

        # Stage 4.5.15. Read BEFORE the delete, and not out of tidiness: `delete` expunges the
        # instance, so `booking.public_id` afterwards would raise. These four values are the
        # whole of what survives the row -- the event has to carry them or the deletion leaves
        # nothing behind at all.
        deleted_public_id = booking.public_id
        deleted_status = booking.status
        deleted_reference = booking.reference

        try:
            if self._repository.delete(booking) == 0:
                # Another transaction deleted it while this one was resolving it. Not found is
                # the honest answer, and raising here is what keeps the event count at one:
                # recording a deletion this transaction did not perform would put two
                # `booking.deleted` events in the trail for one booking.
                raise NotFoundError("Booking not found for this hotel.")

            # AFTER the delete and BEFORE the commit. After, so a booking the database refuses
            # to remove -- payments still reference it -- never reaches this line. Before, so
            # the event and the deletion are one transaction: if the commit fails, both go.
            self._audit.record(
                AuditAction.BOOKING_DELETED,
                AuditResourceType.BOOKING,
                str(deleted_public_id),
                hotel_id=hotel.id,
                details={"status": deleted_status, "reference": deleted_reference},
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            state = sqlstate_of(exc)
            # Stage 4.5.16. The SQLSTATE alone is not enough to say WHAT refused: an audit
            # INSERT failing on its actor foreign key is also a 23503, and before this check
            # it was reported to the client as "payments still reference this booking" --
            # false, and unactionable. The relation the failure is about decides.
            if relation_of(exc) in BOOKING_DEPENDENT_RELATIONS and (
                state in SQLSTATE_DEPENDENCY_VIOLATIONS or state == SQLSTATE_NOT_NULL_VIOLATION
            ):
                raise ConflictError(
                    "This booking cannot be deleted because payments, revenue or reviews "
                    "still reference it. Remove those records first, or cancel the booking "
                    "instead."
                ) from exc
            raise self._translate(exc) from exc

    # --- internals ----------------------------------------------------------------------

    def _resolve_rooms(
        self, hotel: Hotel, room_inputs: Sequence[RoomInputBase[Any]]
    ) -> dict[str, Room]:
        """Resolve every room the payload names, or raise 404 for the first that is missing.

        Stage 4.5.29. One query for the whole payload instead of one per room,
        and one implementation instead of the two identical loops creation and
        modification used to carry.

        **The refusal is unchanged, and keeping it unchanged is the point.** The
        loop this replaced stopped at the FIRST unknown room in payload order and
        named it. Resolving them together makes every miss visible at once, which
        is exactly the temptation to resist: reporting a different room -- the
        last one, or an arbitrary one from a set -- would change what a client is
        told for a payload it did not change. So the misses are re-walked in
        payload order and the first one still wins.

        Resolved BEFORE anything is written, on both paths, so an unknown room
        number is a clean 404 rather than a half-built transaction rolled back.
        """
        found = self._repository.rooms_in_hotel(
            hotel.id, [room_input.room_number for room_input in room_inputs]
        )
        for room_input in room_inputs:
            if room_input.room_number not in found:
                raise NotFoundError(f"Room {room_input.room_number!r} not found for this hotel.")
        return found

    @staticmethod
    def _changed_stay_fields(
        booking: Booking, payload: StayModification, rooms: dict[str, Any]
    ) -> ChangedFields:
        """Which parts of the stay this modification actually moves.

        Compared against the booking as it stands, so a request that restates the current
        dates and the current rooms reports only what genuinely differs. ``rooms`` counts as
        changed when the SET of allocated rooms differs -- an occupancy or rate edit within
        the same rooms is a change to the allocation too, and is reported as ``rooms`` rather
        than enumerated, because enumerating it would mean recording rates.

        Sorted, so the recorded value is stable for a given change rather than dependent on
        dictionary ordering.
        """
        changed: set[str] = set()
        if booking.check_in_date != payload.check_in_date:
            changed.add("check_in_date")
        if booking.check_out_date != payload.check_out_date:
            changed.add("check_out_date")
        if {allocation.room_id for allocation in booking.booking_rooms} != {
            room.id for room in rooms.values()
        }:
            changed.add("rooms")
        return sorted(changed)

    @staticmethod
    def _require_modifiable(status: str) -> None:
        """Refuse to rewrite a stay the lifecycle has closed or a guest is already living in.

        Read under the booking's row lock, so the status this judges is the committed one and
        not a value another transaction is in the middle of changing. See
        `MODIFIABLE_BOOKING_STATUSES` for why `checked_in` is refused here rather than
        supported approximately.
        """
        if status in MODIFIABLE_BOOKING_STATUSES:
            return
        raise ConflictError(f"A booking that is {status} cannot have its stay changed.")

    @staticmethod
    def _require_extendable(status: str) -> None:
        """Refuse to extend a stay nobody is currently living in.

        `EXTENDABLE_BOOKING_STATUSES` holds the decision; this only enforces it, so the
        policy is stated once and the two operations cannot drift into agreement by
        accident. The message names the status and nothing else -- it is public API
        vocabulary the client can already see on the booking.

        A ``confirmed`` booking is refused HERE and served by :meth:`modify_stay`
        instead, which can do strictly more to it. The refusal is a redirection, not a
        dead end, and the message says so.
        """
        if status in EXTENDABLE_BOOKING_STATUSES:
            return
        raise ConflictError(f"Only a checked-in stay can be extended; this booking is {status}.")

    @staticmethod
    def _extension_nights(current: dt.date, requested: dt.date) -> ExtensionNights:
        """The nights an extension adds, or a refusal.

        The half-open interval decides what "added" means, as it does everywhere else in
        the platform: a stay ``[check_in, check_out)`` prices no night on its departure
        day, so pushing departure from the 15th to the 18th adds the 15th, 16th and 17th
        -- three nights, the first of which was previously the day the guest left.

        Both refusals below are conflicts rather than malformed input: the date is a
        perfectly well-formed date, and what makes it unacceptable is the booking's
        committed state, which is why this is asked under the row lock.

        An equal date is refused rather than treated as a no-op. It is the shape a
        retried request takes, and answering it with a cheerful 200 would tell a caller
        that a second extension succeeded when nothing happened at all.
        """
        if requested <= current:
            raise ConflictError(
                f"An extension must move check-out later than {current.isoformat()};"
                f" {requested.isoformat()} does not. Shortening a stay in progress is"
                " not an extension."
            )
        nights = (requested - current).days
        if nights > MAX_EXTENSION_NIGHTS:
            raise ValidationError(
                f"An extension may add at most {MAX_EXTENSION_NIGHTS} nights;"
                f" this one adds {nights}."
            )
        return [current + dt.timedelta(days=offset) for offset in range(nights)]

    @staticmethod
    def _require_allowed_transition(current: str, requested: str) -> None:
        """Refuse a status change the lifecycle does not permit.

        The decision belongs to `BOOKING_STATUS_TRANSITIONS`; this only enforces it, so the
        graph is stated once and no caller can disagree with it. The message names the two
        statuses and nothing else: both are public API vocabulary the client just sent or can
        already see, so it explains the refusal without describing the schema behind it.
        """
        if is_booking_transition_allowed(current, requested):
            return
        if current in TERMINAL_BOOKING_STATUSES:
            raise ConflictError(f"This booking is {current} and can no longer change status.")
        raise ConflictError(f"A booking cannot move from {current} to {requested}.")

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

    def _require_booking_to_render(self, hotel: Hotel, booking_public_id: uuid.UUID) -> Booking:
        """The same 404 wall, for a path that is about to RENDER the booking.

        Stage 4.5.28. Identical tenant predicate and identical refusal -- the only
        difference is which relationships come back loaded, which is a performance
        question and never an authorization one. Used by the two paths that measured a
        room-count-proportional N+1: reading one booking, and re-rendering it after an
        update. The write paths deliberately keep the plain reader; see
        :meth:`~app.repositories.booking.BookingRepository.get_for_response`.
        """
        booking = self._repository.get_for_response(hotel.id, booking_public_id)
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
        constraint = constraint_name_of(exc)
        logger.warning("Booking integrity error (sqlstate=%s, constraint=%s)", state, constraint)

        if is_audit_integrity_failure(exc):
            # Stage 4.5.16. The audit layer failed, not this domain -- so nothing below may
            # claim it. Before this guard, a booking deletion whose audit INSERT failed told
            # the client that payments still referenced the booking, which was false and
            # unactionable.
            return internal_fault(exc)

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
        if relation_of(exc) in BOOKING_DEPENDENT_RELATIONS and (
            state in SQLSTATE_DEPENDENCY_VIOLATIONS or state == SQLSTATE_NOT_NULL_VIOLATION
        ):
            return ConflictError("This booking is still referenced by other records.")
        return ConflictError(GENERIC_CONFLICT_MESSAGE)


__all__ = [
    "BOOKING_DEPENDENT_RELATIONS",
    "DEFAULT_PAGE_SIZE",
    "MAX_EXTENSION_NIGHTS",
    "MAX_PAGE_SIZE",
    "OVERLAP_CONSTRAINT",
    "SQLSTATE_EXCLUSION_VIOLATION",
    "BookingService",
]
