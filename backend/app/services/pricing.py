"""Server-side pricing: what a night costs, decided here rather than by the caller.

Stage 4.5.23, and the first version is deliberately the smallest one that is *correct*.

**What determines a nightly rate.** ``room_types.base_price``, the published rack rate, in
``room_types.currency``. That column has existed since migration 0001 and its own comment
already described this design -- "the INPUT to pricing, never what a guest actually paid" --
so this stage adds no schema, only the engine that finally reads it. A night flagged
complimentary prices at zero; every other night prices at the base rate. That is the whole
rule set, and it is flat on purpose: a rule nobody can state is a rule nobody can audit.

**Current pricing is not contractual pricing.** What a room type costs *today* is
configuration; what a guest agreed to pay is history, and it lives in
``booking_room_nights.rate``, written once when the booking was created. Editing a base price
changes what the next booking will cost and nothing about any booking that already exists.
The two are different questions and the platform keeps two answers -- which is why the base
price is safely editable, and why an integration test proves that editing it leaves existing
night rows untouched.

**Currency is preserved, never converted.** The engine reports the room type's own currency.
If that is not the currency the booking is being written in, the booking is refused with the
project's ordinary validation error. There is no FX here, no rate table and no provider: a
conversion this platform cannot source is a number it should not invent.

**Decimal throughout.** Money is ``Decimal`` from the column to the response, quantised to two
places with ``ROUND_HALF_UP``. No float touches a rate at any point.

**Deliberately NOT implemented, and future scope:** dynamic or AI-driven pricing, demand
forecasting, competitor rates, occupancy-based optimisation, length-of-stay and day-of-week
rules, seasonal calendars, rate plans as first-class rows, promotions, coupons, taxes, and
repricing of bookings that already exist. The engine is shaped so those arrive as new rules
inside :class:`PricingPolicy` rather than as a second pricing path: everything above it asks
for a quote and is handed one rate per night.
"""

from __future__ import annotations

import datetime as dt
import decimal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.errors import NotFoundError, ValidationError
from app.repositories.pricing import PricingRepository

#: Money is carried to two decimal places, matching ``Numeric(14, 2)`` everywhere else.
MONEY_QUANTUM = decimal.Decimal("0.01")

#: What a complimentary night costs. Not ``None`` and not absent: the row still exists, still
#: counts towards occupancy, and is distinguished from an unpriced one by its own flag.
COMPLIMENTARY_RATE = decimal.Decimal("0.00")


@dataclass(frozen=True, slots=True)
class RoomTypeRate:
    """The pricing configuration of one room type: the INPUT to a calculation.

    A value object, not a row. The engine below cannot reach a database through it, which is
    what makes the engine unit-testable without one.
    """

    room_type_code: str
    base_price: decimal.Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class NightRequest:
    """What the caller asks to be priced for one night.

    Note what a caller may say: WHICH night, under which rate plan label, and whether the
    night is comped. Note what it may not say: the amount.
    """

    stay_date: dt.date
    rate_plan_code: str | None = None
    is_complimentary: bool = False


@dataclass(frozen=True, slots=True)
class NightlyRate:
    """One night, priced. Maps one-to-one onto a ``booking_room_nights`` row."""

    stay_date: dt.date
    amount: decimal.Decimal
    currency: str
    rate_plan_code: str | None
    is_complimentary: bool


@dataclass(frozen=True, slots=True)
class StayQuote:
    """Every night of one room's stay, priced individually.

    Never a single opaque total. A four-night stay is four rates, because that is what the
    booking will store, what the daily revenue rollup reads, and what a future rule that
    prices a Saturday differently will need to express.
    """

    room_type_code: str
    currency: str
    nights: tuple[NightlyRate, ...]

    @property
    def total(self) -> decimal.Decimal:
        """The sum of the nights. Convenience for tests and callers; nothing stores it.

        ``bookings.total_amount`` is the CONTRACTED total and is deliberately not defined as
        this sum -- discounts, taxes and packages legitimately break the equality (approved
        decision 10), and this stage does not disturb that.
        """
        return sum((night.amount for night in self.nights), decimal.Decimal("0.00"))


class PricingPolicy:
    """The engine: pricing inputs in, rates out.

    Pure. No session, no HTTP, no clock, no randomness -- the same inputs produce the same
    rates every time, which is what lets a booking be re-derived and checked long afterwards.
    """

    def nightly_rate(self, configured: RoomTypeRate, request: NightRequest) -> NightlyRate:
        """Price one night.

        A negative configured rate raises. ``ck_room_types_base_price_non_negative`` already
        makes it unreachable through the database, so this guards the engine's own contract
        for callers that construct a :class:`RoomTypeRate` directly -- and it raises
        ``ValueError`` rather than a domain error because it is not a client's mistake.
        """
        if configured.base_price < 0:
            raise ValueError("a room type's base price cannot be negative")

        amount = COMPLIMENTARY_RATE if request.is_complimentary else configured.base_price
        return NightlyRate(
            stay_date=request.stay_date,
            amount=amount.quantize(MONEY_QUANTUM, rounding=decimal.ROUND_HALF_UP),
            currency=configured.currency,
            rate_plan_code=request.rate_plan_code,
            is_complimentary=request.is_complimentary,
        )

    def quote_stay(self, configured: RoomTypeRate, requests: Sequence[NightRequest]) -> StayQuote:
        """Price every night of one room's stay, in the order asked.

        One rate per request, always: the caller can line the result up with the nights it
        sent without matching on anything.
        """
        return StayQuote(
            room_type_code=configured.room_type_code,
            currency=configured.currency,
            nights=tuple(self.nightly_rate(configured, request) for request in requests),
        )


class PricingService:
    """Loads a hotel's pricing configuration and applies the policy to it.

    The split is the point: :class:`PricingPolicy` decides what a night costs and can be
    tested with no database at all, while this class knows where configuration lives and
    which hotel is allowed to see it.
    """

    def __init__(self, repository: PricingRepository, policy: PricingPolicy | None = None) -> None:
        self._repository = repository
        self._policy = policy or PricingPolicy()

    def quote_rooms(
        self,
        hotel_id: int,
        requests: Mapping[int, Sequence[NightRequest]],
        *,
        currency: str,
    ) -> dict[int, StayQuote]:
        """Quote every room of one booking, keyed by room id.

        **One query for the whole booking.** Configuration is fetched once and applied to
        every night, so the cost of pricing does not grow with the length of the stay.

        **Tenant scoped at the query.** The repository filters on ``hotel_id``; a room that
        does not belong to this hotel simply is not in the result, and is reported as not
        found rather than priced from another property's configuration.

        **Currency is checked, not converted.** Every room type quoted must already be in the
        booking's currency.
        """
        if not requests:
            return {}

        configured = self._repository.room_type_rates_for_rooms(hotel_id, list(requests))

        quotes: dict[int, StayQuote] = {}
        for room_id, nights in requests.items():
            row = configured.get(room_id)
            if row is None:
                # Not "forbidden": a room of another property is not this hotel's to price.
                raise NotFoundError("Room not found for this hotel.")

            code, base_price, room_currency = row
            if room_currency != currency:
                # Names the two currencies and nothing else -- not the room type, not the
                # configured amount, not any identifier.
                raise ValidationError(
                    f"This room is priced in {room_currency}; the booking is in {currency}."
                )

            quotes[room_id] = self._policy.quote_stay(
                RoomTypeRate(room_type_code=code, base_price=base_price, currency=room_currency),
                nights,
            )
        return quotes


__all__ = [
    "COMPLIMENTARY_RATE",
    "MONEY_QUANTUM",
    "NightRequest",
    "NightlyRate",
    "PricingPolicy",
    "PricingService",
    "RoomTypeRate",
    "StayQuote",
]
