"""Booking API contracts.

``bookings`` has ``public_id``, so the URL identity is
``/hotels/{hotel_public_id}/bookings/{booking_public_id}``. Internal BIGINTs never appear.

**Why rooms and nights are nested rather than separate sub-resources.**

``booking_rooms`` carries a ``DEFERRABLE INITIALLY DEFERRED`` constraint trigger requiring
``COUNT(booking_room_nights) == booking_rooms.nights`` at COMMIT. Every HTTP request is its
own transaction, so a request that created an allocation *without* its nights would commit
zero nights against an N-night stay and be rejected -- and no later request could repair it,
because the first one already failed.

The frozen schema therefore forces allocation and pricing to be one atomic operation. They
are nested in the create payload, not exposed as independently writable resources.

Nightly rates are supplied by the client. Deriving them would be pricing logic, which belongs
to the financial domains, not here.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Mirrors BookingStatus in app.models.enums, which generates the CHECK constraint.
BookingStatusLiteral = Literal[
    "pending", "confirmed", "checked_in", "checked_out", "cancelled", "no_show"
]
#: Mirrors BookingSource.
BookingSourceLiteral = Literal[
    "direct", "website", "phone", "walk_in", "booking_com", "expedia", "airbnb", "agoda", "other"
]

ReferenceField = Annotated[str, Field(min_length=1, max_length=50)]
CurrencyField = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]
MoneyField = Annotated[decimal.Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
RoomNumberField = Annotated[
    str, Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9][A-Z0-9._-]*$")
]


class NightInputBase(BaseModel):
    """What every night payload carries, whoever decides the amount.

    Stage 4.5.23 split the two cases. Creation prices on the server and so does not
    accept an amount; modification still carries one, because repricing an existing
    booking is a financial policy this stage was told not to decide. The shared fields
    are declared once so the two cannot drift.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    stay_date: dt.date
    rate_plan_code: Annotated[str, Field(max_length=50)] | None = None
    #: A comped night. A pricing INPUT, not an amount: it selects the zero rate rather
    #: than naming one, so a caller still cannot choose what a night costs.
    is_complimentary: bool = False


class BookingRoomNightRequest(NightInputBase):
    """One night to be priced BY THE SERVER, for a new booking.

    **There is no ``rate`` field, and its absence is the contract.** The model forbids
    extras, so a client that sends one is told so with a 422 naming the field rather
    than having its number silently ignored -- which would be worse, because the caller
    would believe it had set a price the server never charged.
    """


class RoomInputBase[NightT: NightInputBase](BaseModel):
    """One room allocated to the booking, with the full set of nights it covers.

    The room is named by ``room_number``, which is unique within a hotel -- the same
    identifier the Room domain exposes. No room id is accepted, so a client cannot reach a
    room belonging to another property.

    Generic in its night type so that creation and modification share every rule about
    rooms and nights -- the distinctness check below, the occupancy bounds, the room
    number's shape -- and differ in exactly one thing: whether a night carries an amount.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    room_number: RoomNumberField
    adults: Annotated[int, Field(ge=1, le=99)] = 1
    children: Annotated[int, Field(ge=0, le=99)] = 0
    guest_name: Annotated[str, Field(max_length=200)] | None = None
    nights: Annotated[list[NightT], Field(min_length=1)]

    @field_validator("room_number", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("nights")
    @classmethod
    def _nights_are_distinct(cls, value: list[NightT]) -> list[NightT]:
        """Mirror ``uq_booking_room_nights_room_stay_date`` at the edge.

        A duplicated date would otherwise reach the unique index and come back as a 409 the
        client cannot act on as precisely.
        """
        dates = [night.stay_date for night in value]
        if len(dates) != len(set(dates)):
            raise ValueError("each stay_date may appear only once per room")
        return value


class BookingRoomRequest(RoomInputBase[BookingRoomNightRequest]):
    """A room on a NEW booking: the nights it covers, none of them carrying a price."""


class BookingCreate(BaseModel):
    """Payload for POST .../bookings.

    The hotel comes from the URL. The guest is named by *their* public id, and is resolved
    within that same hotel -- a guest of another property is simply not found.

    **Nightly rates are not in this payload.** Stage 4.5.23 moved them to the server:
    the rate charged for a night is calculated from the room type's ``base_price`` and
    written to ``booking_room_nights``, and there is no field here through which a
    caller can propose an amount. ``total_amount`` remains the CONTRACTED total and is
    still supplied -- it is not defined as the sum of the nights (approved decision 10),
    and redefining it would be a reconciliation change this stage does not make.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    guest_public_id: uuid.UUID
    reference: ReferenceField
    check_in_date: dt.date
    check_out_date: dt.date
    #: Defaults to the database default. `pending` holds no inventory, so it cannot collide
    #: with another booking; `confirmed` can and will be refused by the exclusion constraint.
    status: BookingStatusLiteral = "pending"
    adults: Annotated[int, Field(ge=1, le=99)] = 1
    children: Annotated[int, Field(ge=0, le=99)] = 0
    source: BookingSourceLiteral = "direct"
    channel_reference: Annotated[str, Field(max_length=100)] | None = None
    #: The contracted total. NOT defined as the sum of the nightly rates: discounts, taxes
    #: and packages legitimately break that equality (approved decision 10).
    total_amount: MoneyField
    currency: CurrencyField
    special_requests: str | None = None
    rooms: Annotated[list[BookingRoomRequest], Field(min_length=1)]

    @field_validator("currency", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _stay_is_coherent(self) -> BookingCreate:
        """Mirror ``ck_bookings_stay_dates_ordered`` and the night-completeness trigger.

        The trigger remains the authority -- it is deferred and fires at COMMIT regardless of
        what happens here. Validating at the edge simply turns the common mistake into a
        field-level 422 naming the missing dates, instead of an opaque conflict.
        """
        if self.check_out_date <= self.check_in_date:
            raise ValueError("check_out_date must be after check_in_date")

        expected = {
            self.check_in_date + dt.timedelta(days=offset)
            for offset in range((self.check_out_date - self.check_in_date).days)
        }
        for room in self.rooms:
            supplied = {night.stay_date for night in room.nights}
            if supplied != expected:
                missing = sorted(str(d) for d in expected - supplied)
                extra = sorted(str(d) for d in supplied - expected)
                raise ValueError(
                    f"room {room.room_number} must price exactly the nights "
                    f"[{self.check_in_date}, {self.check_out_date}); "
                    f"missing={missing} unexpected={extra}"
                )

        numbers = [room.room_number for room in self.rooms]
        if len(numbers) != len(set(numbers)):
            raise ValueError("each room may be allocated only once per booking")
        return self


class StayModification(BaseModel):
    """A replacement stay: new dates, and the complete allocation that goes with them.

    **The whole stay is restated, not patched.** Moving a booking's dates already forces every
    allocation and every night row to be rewritten -- ``booking_room_nights.stay_date`` is
    CHECK-constrained to lie inside the stay, and the deferred trigger requires exactly one
    row per night per room. A partial payload would leave the server guessing which nights the
    caller meant to keep, so it asks for all of them, in the same shape ``BookingCreate``
    already uses.

    **Rates come from the server** (Stage 4.5.24). This payload carries no amount, exactly as
    creation carries none: the nights are re-priced from the room type's configured rate,
    and the financial consequence of that -- more owed, something refundable, or nothing
    at all -- is reported back rather than acted on. No payment is taken and no refund is
    issued; see :mod:`app.services.repricing` for why a calculation is not a transaction.

    What this CANNOT change is everything it does not mention: the booking's public id, its
    hotel, its guest, its reference, its status, its payments, its reviews. Those are absent
    from the payload, so no request can reach them.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    check_in_date: dt.date
    check_out_date: dt.date
    rooms: Annotated[list[BookingRoomRequest], Field(min_length=1)]

    @model_validator(mode="after")
    def _stay_is_coherent(self) -> StayModification:
        """The same edge rule ``BookingCreate`` applies, for the same reason.

        The deferred trigger remains the authority; this turns the common mistake into a
        422 that names the offending dates instead of an opaque conflict at COMMIT.
        """
        if self.check_out_date <= self.check_in_date:
            raise ValueError("check_out_date must be after check_in_date")

        expected = {
            self.check_in_date + dt.timedelta(days=offset)
            for offset in range((self.check_out_date - self.check_in_date).days)
        }
        for room in self.rooms:
            supplied = {night.stay_date for night in room.nights}
            if supplied != expected:
                missing = sorted(str(d) for d in expected - supplied)
                extra = sorted(str(d) for d in supplied - expected)
                raise ValueError(
                    f"room {room.room_number} must price exactly the nights "
                    f"[{self.check_in_date}, {self.check_out_date}); "
                    f"missing={missing} unexpected={extra}"
                )

        numbers = [room.room_number for room in self.rooms]
        if len(numbers) != len(set(numbers)):
            raise ValueError("each room may be allocated only once per booking")
        return self


class StayExtension(BaseModel):
    """A request to keep an in-house guest longer: one date, and nothing else.

    Stage 4.5.27. Contrast :class:`StayModification`, which restates the WHOLE
    stay. This payload has exactly one field, and the fields it does not have are
    the contract:

    * no ``check_in_date`` -- the guest arrived; that date is history;
    * no ``rooms`` -- the room holds their belongings and is not reassignable
      through a date change;
    * no ``rate`` and no ``total_amount`` -- the added nights are priced by the
      server from the room type's configured rate, exactly as creation prices,
      and the nights already slept keep the rates they were sold at.

    ``extra='forbid'`` is what makes those absences enforceable rather than
    merely documented: a client that sends ``rate`` or ``total_amount`` is told
    so with a 422 naming the field, instead of having its number silently
    ignored -- which would be worse, because the caller would believe it had set
    a price the server never charged.

    The date is not validated against the booking here, because the booking is
    not visible here. Whether it is genuinely LATER than the current departure is
    a question about committed state, and it is asked under the booking's row
    lock where the answer cannot go stale between the asking and the writing.
    """

    model_config = ConfigDict(extra="forbid")

    #: The new departure date. Must be strictly later than the current one.
    check_out_date: dt.date


class StayRepricing(BaseModel):
    """What a stay modification did to the money.

    Every figure is server-derived from the two authoritative sums and the payment
    ledger. Note what this is NOT: a receipt. ``additional_amount_due`` has not been
    charged and ``refundable_amount`` has not been refunded -- the platform owns no
    payment processor, and settling either remains a separate, deliberate act through
    the payment endpoints.
    """

    model_config = ConfigDict(extra="forbid")

    currency: str
    #: The stay's value before the modification: the sum of its old nightly rates.
    previous_total: decimal.Decimal
    #: And after it. Both are ``SUM(booking_room_nights.rate)``, the figure
    #: reconciliation already treats as authoritative.
    new_total: decimal.Decimal
    #: ``new_total - previous_total``. Negative when the stay got cheaper.
    difference: decimal.Decimal
    #: Owed as a result of this change. Zero unless the stay got dearer.
    additional_amount_due: decimal.Decimal
    #: Returnable as a result of this change, capped by what was actually collected: a
    #: reduction against a booking that has paid nothing refunds nothing.
    refundable_amount: decimal.Decimal
    #: The booking's balance afterwards. Positive still owed, negative overpaid.
    outstanding_after: decimal.Decimal
    adjustment: Literal["none", "amount_due", "refundable"]


class StayModificationResponse(BaseModel):
    """The modified booking, and what it cost.

    A distinct model rather than extra fields on :class:`BookingResponse`, because the
    financial consequence belongs to the ACT of modifying and not to the booking: a GET
    of the same booking a moment later has no repricing to report, and a response shape
    that carried empty pricing fields everywhere would say otherwise.

    Shared with the in-house extension (Stage 4.5.27) rather than copied. The two
    operations differ in what they may change; they do not differ in what a caller
    needs to be told afterwards, which is the booking as it now stands and what the
    change did to the money.
    """

    model_config = ConfigDict(extra="forbid")

    booking: BookingResponse
    repricing: StayRepricing


class BookingUpdate(BaseModel):
    """Payload for PATCH. Every field optional; omitted fields are left untouched.

    Deliberately narrow. Absent, and why:

    * ``check_in_date`` / ``check_out_date`` -- changing them cascades to ``booking_rooms``
      and on to the night rows, but the night *count* would not change, so the deferred
      trigger would reject the commit. Re-dating a stay is a re-pricing operation, not a
      field edit.
    * ``reference``, ``guest_public_id``, ``rooms`` -- identity and allocation. Re-rooming is
      an atomic operation for the same reason as creation.
    * ``cancelled_at`` -- derived. ``ck_bookings_cancellation_consistent`` is a biconditional,
      so the service sets and clears it in step with ``status``.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    status: BookingStatusLiteral | None = None
    adults: Annotated[int, Field(ge=1, le=99)] | None = None
    children: Annotated[int, Field(ge=0, le=99)] | None = None
    source: BookingSourceLiteral | None = None
    channel_reference: Annotated[str, Field(max_length=100)] | None = None
    total_amount: MoneyField | None = None
    special_requests: str | None = None
    cancellation_reason: Annotated[str, Field(max_length=500)] | None = None


class BookingRoomNightResponse(BaseModel):
    """One priced night, as returned."""

    model_config = ConfigDict(from_attributes=True)

    stay_date: dt.date
    rate: decimal.Decimal
    rate_plan_code: str | None
    is_complimentary: bool


class BookingRoomResponse(BaseModel):
    """One allocated room, with its nights.

    ``room_type_code`` is included so a caller can locate the room in the Room domain's
    hierarchy without a second lookup. ``nights`` is the generated column, so it always
    agrees with the dates.
    """

    model_config = ConfigDict(from_attributes=True)

    room_number: str
    room_type_code: str
    adults: int
    children: int
    guest_name: str | None
    nights: int
    nightly_rates: list[BookingRoomNightResponse]


class BookingResponse(BaseModel):
    """What the API returns.

    Carries ``hotel_public_id`` and ``guest_public_id`` so a client can reach both parents
    from the payload alone. No internal key appears anywhere in it.
    """

    model_config = ConfigDict(from_attributes=True)

    hotel_public_id: uuid.UUID
    public_id: uuid.UUID
    guest_public_id: uuid.UUID
    reference: str
    check_in_date: dt.date
    check_out_date: dt.date
    status: BookingStatusLiteral
    adults: int
    children: int
    source: BookingSourceLiteral
    channel_reference: str | None
    total_amount: decimal.Decimal
    currency: str
    special_requests: str | None
    cancelled_at: dt.datetime | None
    cancellation_reason: str | None
    booked_at: dt.datetime
    created_at: dt.datetime
    updated_at: dt.datetime
    rooms: list[BookingRoomResponse]


__all__ = [
    "BookingCreate",
    "BookingResponse",
    "BookingRoomNightRequest",
    "BookingRoomNightResponse",
    "BookingRoomRequest",
    "BookingRoomResponse",
    "BookingUpdate",
    "NightInputBase",
    "RoomInputBase",
    "StayExtension",
    "StayModification",
    "StayModificationResponse",
    "StayRepricing",
]
