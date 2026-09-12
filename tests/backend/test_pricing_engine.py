"""The pricing engine, with no database anywhere in sight.

Stage 4.5.23. The point of splitting :class:`PricingPolicy` from :class:`PricingService` is
that the rules can be stated and checked as arithmetic: given a configured rate and a night,
what does the night cost. Everything in this file constructs its inputs directly, so a failure
here is a failure of a pricing rule and never of a query, a session or a fixture.

The tenant boundary, the currency refusal and the booking integration are exercised against
real PostgreSQL in ``tests/integration/test_pricing_api.py``; they cannot be checked here
because none of them is arithmetic.
"""

from __future__ import annotations

import datetime as dt
import decimal
import inspect

import pytest

from app.services.pricing import (
    COMPLIMENTARY_RATE,
    MONEY_QUANTUM,
    NightlyRate,
    NightRequest,
    PricingPolicy,
    PricingService,
    RoomTypeRate,
    StayQuote,
)

CHECK_IN = dt.date(2026, 9, 10)


def rate_of(amount: str, currency: str = "EUR", code: str = "DLX") -> RoomTypeRate:
    return RoomTypeRate(room_type_code=code, base_price=decimal.Decimal(amount), currency=currency)


def stay(nights: int, *, complimentary: frozenset[int] = frozenset()) -> list[NightRequest]:
    return [
        NightRequest(
            stay_date=CHECK_IN + dt.timedelta(days=offset),
            is_complimentary=offset in complimentary,
        )
        for offset in range(nights)
    ]


@pytest.fixture
def policy() -> PricingPolicy:
    return PricingPolicy()


# ======================================================================================
# One night
# ======================================================================================


def test_a_night_costs_the_configured_base_price(policy: PricingPolicy) -> None:
    priced = policy.nightly_rate(rate_of("120.00"), NightRequest(stay_date=CHECK_IN))

    assert priced.amount == decimal.Decimal("120.00")
    assert priced.stay_date == CHECK_IN


def test_a_complimentary_night_costs_nothing(policy: PricingPolicy) -> None:
    """Zero, and still a row: a comped night counts towards occupancy and is excluded from
    ADR by its flag, not by being absent."""
    priced = policy.nightly_rate(
        rate_of("120.00"), NightRequest(stay_date=CHECK_IN, is_complimentary=True)
    )

    assert priced.amount == COMPLIMENTARY_RATE
    assert priced.is_complimentary is True


def test_a_configured_zero_is_a_price_not_a_missing_one(policy: PricingPolicy) -> None:
    """``base_price >= 0`` permits zero, so a room type may legitimately be free. That is a
    priced night at 0.00, distinguished from a comped one by the flag it does not carry."""
    priced = policy.nightly_rate(rate_of("0.00"), NightRequest(stay_date=CHECK_IN))

    assert priced.amount == decimal.Decimal("0.00")
    assert priced.is_complimentary is False


def test_a_negative_configured_rate_is_refused(policy: PricingPolicy) -> None:
    """``ck_room_types_base_price_non_negative`` makes this unreachable through the database.
    The engine still refuses it, because it is reachable through the engine."""
    with pytest.raises(ValueError, match="cannot be negative"):
        policy.nightly_rate(rate_of("-0.01"), NightRequest(stay_date=CHECK_IN))


def test_the_rate_plan_label_is_carried_through_untouched(policy: PricingPolicy) -> None:
    """Provenance, not arithmetic: the label says why a rate was what it was, and the engine
    neither interprets it nor lets it change the amount."""
    labelled = policy.nightly_rate(
        rate_of("120.00"), NightRequest(stay_date=CHECK_IN, rate_plan_code="CORP")
    )
    unlabelled = policy.nightly_rate(rate_of("120.00"), NightRequest(stay_date=CHECK_IN))

    assert labelled.rate_plan_code == "CORP"
    assert labelled.amount == unlabelled.amount


# ======================================================================================
# A stay is many nights, and stays many nights
# ======================================================================================


@pytest.mark.parametrize("length", [1, 2, 4, 7, 30])
def test_every_stay_date_produces_exactly_one_rate(policy: PricingPolicy, length: int) -> None:
    """A four-night stay is four rates. The result is never collapsed into a total, because a
    total cannot be written to ``booking_room_nights`` and cannot express a Saturday."""
    quote = policy.quote_stay(rate_of("120.00"), stay(length))

    assert len(quote.nights) == length
    assert [night.stay_date for night in quote.nights] == [
        CHECK_IN + dt.timedelta(days=offset) for offset in range(length)
    ]


def test_the_nights_come_back_in_the_order_they_were_asked_for(policy: PricingPolicy) -> None:
    """The caller lines the quote up with its own payload positionally, so order is contract."""
    requests = [
        NightRequest(stay_date=dt.date(2026, 9, 12)),
        NightRequest(stay_date=dt.date(2026, 9, 10)),
        NightRequest(stay_date=dt.date(2026, 9, 11)),
    ]

    quote = policy.quote_stay(rate_of("120.00"), requests)

    assert [night.stay_date for night in quote.nights] == [
        request.stay_date for request in requests
    ]


def test_a_four_night_stay_totals_four_nights_of_the_base_price(policy: PricingPolicy) -> None:
    quote = policy.quote_stay(rate_of("120.00"), stay(4))

    assert quote.total == decimal.Decimal("480.00")


def test_comped_nights_are_free_and_the_rest_are_not(policy: PricingPolicy) -> None:
    quote = policy.quote_stay(rate_of("120.00"), stay(4, complimentary=frozenset({1, 2})))

    assert [night.amount for night in quote.nights] == [
        decimal.Decimal("120.00"),
        decimal.Decimal("0.00"),
        decimal.Decimal("0.00"),
        decimal.Decimal("120.00"),
    ]
    assert quote.total == decimal.Decimal("240.00")


def test_an_empty_stay_quotes_nothing(policy: PricingPolicy) -> None:
    quote = policy.quote_stay(rate_of("120.00"), [])

    assert quote.nights == ()
    assert quote.total == decimal.Decimal("0.00")


# ======================================================================================
# Money
# ======================================================================================


def test_every_amount_is_a_decimal(policy: PricingPolicy) -> None:
    quote = policy.quote_stay(rate_of("120.03"), stay(3))

    assert all(isinstance(night.amount, decimal.Decimal) for night in quote.nights)
    assert isinstance(quote.total, decimal.Decimal)


def test_a_price_no_float_can_hold_survives_exactly(policy: PricingPolicy) -> None:
    """120.03 has no exact binary representation. Three of them is 360.09 in decimal and
    360.09000000000003 in float -- so this assertion fails the moment money becomes a float."""
    quote = policy.quote_stay(rate_of("120.03"), stay(3))

    assert quote.total == decimal.Decimal("360.09")
    assert str(quote.total) == "360.09"


def test_a_hundred_nights_of_a_third_of_a_cent_do_not_drift(policy: PricingPolicy) -> None:
    """The failure mode a float would produce is accumulation, so accumulate."""
    quote = policy.quote_stay(rate_of("0.07"), stay(100))

    assert quote.total == decimal.Decimal("7.00")


def test_amounts_are_quantised_to_the_cent(policy: PricingPolicy) -> None:
    """Configuration is ``Numeric(14, 2)`` so this is belt and braces, and it is the line that
    decides what happens if a future rule ever produces a third decimal place."""
    priced = policy.nightly_rate(rate_of("120.005"), NightRequest(stay_date=CHECK_IN))

    assert priced.amount == decimal.Decimal("120.01")
    assert priced.amount.as_tuple().exponent == MONEY_QUANTUM.as_tuple().exponent


# ======================================================================================
# Determinism and currency
# ======================================================================================


def test_the_same_inputs_price_the_same_way_every_time(policy: PricingPolicy) -> None:
    """Not a tautology: it is what lets a booking be re-derived and checked years later, and
    it is the property a clock, a random tie-break or a cached counter would break."""
    configured, requests = rate_of("120.00"), stay(5)

    first = policy.quote_stay(configured, requests)
    second = PricingPolicy().quote_stay(configured, requests)

    assert first == second


def test_the_engine_reads_nothing_outside_its_arguments() -> None:
    """The signatures are the whole input surface: a configured rate and the nights asked for.

    No session, no clock, no request, no settings. Checked rather than asserted in prose,
    because 'pure' is a claim that decays quietly.
    """
    assert set(inspect.signature(PricingPolicy.nightly_rate).parameters) == {
        "self",
        "configured",
        "request",
    }
    assert set(inspect.signature(PricingPolicy.quote_stay).parameters) == {
        "self",
        "configured",
        "requests",
    }


@pytest.mark.parametrize("currency", ["EUR", "USD", "GBP", "JPY"])
def test_the_quote_is_in_the_room_types_own_currency(policy: PricingPolicy, currency: str) -> None:
    """Carried, never converted. There is no FX in this platform and this is where that shows."""
    quote = policy.quote_stay(rate_of("120.00", currency=currency), stay(2))

    assert quote.currency == currency
    assert {night.currency for night in quote.nights} == {currency}


def test_the_engine_converts_nothing_between_currencies(policy: PricingPolicy) -> None:
    """The same number in two currencies stays the same number. A conversion would show here."""
    euros = policy.quote_stay(rate_of("120.00", currency="EUR"), stay(3))
    yen = policy.quote_stay(rate_of("120.00", currency="JPY"), stay(3))

    assert euros.total == yen.total
    assert euros.currency != yen.currency


# ======================================================================================
# Shape of the result
# ======================================================================================


def test_a_quote_maps_one_to_one_onto_night_rows(policy: PricingPolicy) -> None:
    """Every field a ``booking_room_nights`` row needs from pricing is on the result, and the
    booking service copies them across without deciding anything itself."""
    quote = policy.quote_stay(
        rate_of("120.00"),
        [NightRequest(stay_date=CHECK_IN, rate_plan_code="RACK", is_complimentary=False)],
    )
    night = quote.nights[0]

    assert (night.stay_date, night.amount, night.rate_plan_code, night.is_complimentary) == (
        CHECK_IN,
        decimal.Decimal("120.00"),
        "RACK",
        False,
    )


def test_the_results_are_immutable(policy: PricingPolicy) -> None:
    """A quote that could be edited after it was calculated would not be a quote."""
    quote = policy.quote_stay(rate_of("120.00"), stay(1))

    with pytest.raises((AttributeError, TypeError)):
        quote.nights[0].amount = decimal.Decimal("1.00")  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        quote.currency = "USD"  # type: ignore[misc]


def test_a_night_request_cannot_carry_an_amount() -> None:
    """The engine's input type has no field for a price, which is the same guarantee the API
    schema makes and the reason a client cannot route one through."""
    assert "amount" not in NightRequest.__dataclass_fields__
    assert "rate" not in NightRequest.__dataclass_fields__
    assert set(NightRequest.__dataclass_fields__) == {
        "stay_date",
        "rate_plan_code",
        "is_complimentary",
    }


def test_the_service_cannot_be_built_without_a_repository() -> None:
    """Configuration has one source. A pricing service with a default repository would be one
    that could silently price from somewhere else."""
    parameters = inspect.signature(PricingService.__init__).parameters

    assert parameters["repository"].default is inspect.Parameter.empty


def test_the_types_the_engine_returns_are_the_ones_it_documents() -> None:
    assert issubclass(NightlyRate, object) and NightlyRate.__dataclass_fields__
    assert set(StayQuote.__dataclass_fields__) == {"room_type_code", "currency", "nights"}
