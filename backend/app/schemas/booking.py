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


class BookingRoomNightInput(BaseModel):
    """One priced night for one allocated room."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    stay_date: dt.date
    rate: MoneyField
    rate_plan_code: Annotated[str, Field(max_length=50)] | None = None
    is_complimentary: bool = False


class BookingRoomInput(BaseModel):
    """One room allocated to the booking, with the full set of nights it covers.

    The room is named by ``room_number``, which is unique within a hotel -- the same
    identifier the Room domain exposes. No room id is accepted, so a client cannot reach a
    room belonging to another property.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    room_number: RoomNumberField
    adults: Annotated[int, Field(ge=1, le=99)] = 1
    children: Annotated[int, Field(ge=0, le=99)] = 0
    guest_name: Annotated[str, Field(max_length=200)] | None = None
    nights: Annotated[list[BookingRoomNightInput], Field(min_length=1)]

    @field_validator("room_number", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("nights")
    @classmethod
    def _nights_are_distinct(
        cls, value: list[BookingRoomNightInput]
    ) -> list[BookingRoomNightInput]:
        """Mirror ``uq_booking_room_nights_room_stay_date`` at the edge.

        A duplicated date would otherwise reach the unique index and come back as a 409 the
        client cannot act on as precisely.
        """
        dates = [night.stay_date for night in value]
        if len(dates) != len(set(dates)):
            raise ValueError("each stay_date may appear only once per room")
        return value


class BookingCreate(BaseModel):
    """Payload for POST .../bookings.

    The hotel comes from the URL. The guest is named by *their* public id, and is resolved
    within that same hotel -- a guest of another property is simply not found.
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
    rooms: Annotated[list[BookingRoomInput], Field(min_length=1)]

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
    "BookingRoomInput",
    "BookingRoomNightInput",
    "BookingRoomNightResponse",
    "BookingRoomResponse",
    "BookingUpdate",
]
