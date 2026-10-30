"""R2.8P: `get_revenue_breakdown`'s server-calculated totals -- per currency, exact, and never
across currencies -- and the figure check, which they feed and do not change.

Offline: pure functions and a fake analytics service. No model, provider, network or database.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Numeric

from app.copilot.grounding import ungrounded_figures
from app.copilot.registry import build_default_registry
from app.copilot.tools import revenue_breakdown
from app.copilot.tools.revenue_breakdown import (
    RevenueBreakdownArguments,
    RevenueBreakdownOutput,
    currency_totals,
)
from app.core.errors import ValidationError
from app.models.finance import Revenue
from app.schemas.analytics import DateRange, RevenueBreakdownResponse, RevenueCategoryBreakdown
from app.services.analytics import MONEY_PLACES

MAY = (dt.date(2026, 5, 1), dt.date(2026, 5, 31))


def category(code: str, currency: str, amount: str, tax: str, entries: int) -> Any:
    return RevenueCategoryBreakdown(
        category_code=code,
        is_room_revenue=code == "ROOM",
        currency=currency,
        amount=Decimal(amount),
        tax_amount=Decimal(tax),
        entry_count=entries,
    )


#: The evaluation fixture's May ledger (`tests/evaluation/fixture_hotel.py`), one currency.
EUR_MAY = [
    category("ROOM", "EUR", "51800.00", "4662.00", 212),
    category("FNB", "EUR", "2450.00", "318.50", 96),
    category("SPA", "EUR", "700.00", "91.00", 14),
]
TWO_CURRENCIES = [
    category("FNB", "EUR", "2450.00", "318.50", 96),
    category("FNB", "USD", "1000.00", "80.00", 10),
    category("ROOM", "EUR", "51800.00", "4662.00", 212),
    category("SPA", "USD", "300.25", "24.02", 3),
]


def as_tuples(totals: list[Any]) -> list[tuple[str, Decimal, Decimal, int]]:
    return [(t.currency, t.amount, t.tax_amount, t.entry_count) for t in totals]


# --- the arithmetic -------------------------------------------------------------------------------


def test_a_single_currency_total_is_the_exact_sum_of_its_categories() -> None:
    assert as_tuples(currency_totals(EUR_MAY)) == [
        ("EUR", Decimal("54950.00"), Decimal("5071.50"), 322)
    ]


def test_each_currency_is_totalled_on_its_own() -> None:
    assert as_tuples(currency_totals(TWO_CURRENCIES)) == [
        ("EUR", Decimal("54250.00"), Decimal("4980.50"), 308),
        ("USD", Decimal("1300.25"), Decimal("104.02"), 13),
    ]


def test_no_total_ever_combines_currencies() -> None:
    totals = currency_totals(TWO_CURRENCIES)
    # One entry per currency present, each labelled with exactly one currency ...
    assert [t.currency for t in totals] == ["EUR", "USD"]
    assert len(totals) == len({c.currency for c in TWO_CURRENCIES})
    # ... each equal to the sum over that currency's categories and no other ...
    for total in totals:
        own = [c for c in TWO_CURRENCIES if c.currency == total.currency]
        assert total.amount == sum((c.amount for c in own), Decimal(0))
        assert total.tax_amount == sum((c.tax_amount for c in own), Decimal(0))
        assert total.entry_count == sum(c.entry_count for c in own)
    # ... and the cross-currency sum appears nowhere.
    combined = sum((c.amount for c in TWO_CURRENCIES), Decimal(0))
    assert combined not in {t.amount for t in totals}
    assert set(type(totals[0]).model_fields) == {"currency", "amount", "tax_amount", "entry_count"}


def test_an_empty_breakdown_has_no_totals() -> None:
    assert currency_totals([]) == []


def test_totals_are_exact_decimals_not_floats() -> None:
    totals = currency_totals(
        [
            category("A", "EUR", "0.10", "0.01", 1),
            category("B", "EUR", "0.20", "0.02", 1),
            category("C", "EUR", "999999999999.70", "0.00", 1),
        ]
    )
    [total] = totals
    assert isinstance(total.amount, Decimal) and isinstance(total.tax_amount, Decimal)
    assert total.amount == Decimal("1000000000000.00")  # 0.1 + 0.2 + … has no float error here
    assert total.tax_amount == Decimal("0.03")


def test_refunds_net_off_and_a_negative_total_is_reported_as_it_is() -> None:
    """Ledger lines may be negative (refunds and corrections); tax may not (a CHECK)."""
    totals = currency_totals(
        [category("FNB", "EUR", "120.00", "10.00", 4), category("REFUND", "EUR", "-200.50", "0", 2)]
    )
    assert as_tuples(totals) == [("EUR", Decimal("-80.50"), Decimal("10.00"), 6)]


def test_the_order_is_by_currency_whatever_the_input_order() -> None:
    assert currency_totals(list(reversed(TWO_CURRENCIES))) == currency_totals(TWO_CURRENCIES)


def test_the_category_amounts_are_exact_sums_so_their_total_is_too() -> None:
    """The totals add the service's per-category `Decimal`s. That is exact because the ledger
    columns hold two decimal places and the service quantises to the same two places: nothing is
    rounded away. If either changed, this fails before a total could drift."""
    for column in (Revenue.__table__.c.amount, Revenue.__table__.c.tax_amount):
        assert isinstance(column.type, Numeric) and column.type.scale == 2
    assert Decimal("0.01") == MONEY_PLACES


# --- the tool -------------------------------------------------------------------------------------


class FakeAnalytics:
    def __init__(self, response: RevenueBreakdownResponse | Exception) -> None:
        self.response = response

    def revenue_breakdown(self, hotel: uuid.UUID, date_from: dt.date, date_to: dt.date) -> Any:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class Context:
    def __init__(self, analytics: FakeAnalytics) -> None:
        self.hotel_public_id = uuid.uuid4()
        self.services = type("Services", (), {"analytics": analytics})()


def response_for(categories: list[Any]) -> RevenueBreakdownResponse:
    return RevenueBreakdownResponse(
        hotel_public_id=uuid.uuid4(),
        range=DateRange(date_from=MAY[0], date_to=MAY[1], days=31),
        categories=categories,
    )


def run(categories: list[Any]) -> RevenueBreakdownOutput:
    arguments = RevenueBreakdownArguments(date_from=MAY[0], date_to=MAY[1])
    return revenue_breakdown.run(Context(FakeAnalytics(response_for(categories))), arguments)  # type: ignore[arg-type]


def test_the_tool_adds_totals_and_leaves_every_category_as_the_service_returned_it() -> None:
    output = run(TWO_CURRENCIES)
    assert output.categories == TWO_CURRENCIES
    assert output.totals == currency_totals(TWO_CURRENCIES)
    dumped = output.model_dump(mode="json")  # what reaches the model and the figure check
    assert dumped["totals"] == [
        {"currency": "EUR", "amount": "54250.00", "tax_amount": "4980.50", "entry_count": 308},
        {"currency": "USD", "amount": "1300.25", "tax_amount": "104.02", "entry_count": 13},
    ]
    assert "hotel_public_id" not in dumped


def test_an_empty_range_returns_empty_categories_and_empty_totals() -> None:
    output = run([])
    assert (output.categories, output.totals) == ([], [])


def test_a_service_refusal_reaches_the_model_unchanged() -> None:
    refused = ValidationError()
    arguments = RevenueBreakdownArguments(date_from=MAY[0], date_to=MAY[1])
    with pytest.raises(ValidationError):
        revenue_breakdown.run(Context(FakeAnalytics(refused)), arguments)  # type: ignore[arg-type]


def test_the_rest_response_is_unchanged_and_the_registry_accepts_the_output() -> None:
    assert "totals" not in RevenueBreakdownResponse.model_fields  # the API is not touched
    assert "get_revenue_breakdown" in {c.name for c in build_default_registry().contracts()}


def test_the_description_tells_the_model_totals_exist_and_never_mix_currencies() -> None:
    text = revenue_breakdown.CONTRACT.description
    assert "Totals per currency are included" in text
    assert "amounts in different currencies are never added together" in text


# --- the figure check: unchanged, and fed by the totals -------------------------------------------


def outputs(categories: list[Any]) -> list[dict[str, Any]]:
    return [run(categories).model_dump(mode="json")]


def test_a_per_currency_total_is_grounded() -> None:
    answer = "Total revenue: €54,950.00 (tax €5,071.50) across 322 entries."
    assert ungrounded_figures(answer, outputs=outputs(EUR_MAY), question="") == ()


def test_a_total_no_tool_returned_is_still_withheld() -> None:
    answer = "Total revenue: €55,000.00."
    assert ungrounded_figures(answer, outputs=outputs(EUR_MAY), question="") == ("55,000.00",)


def test_a_cross_currency_sum_is_still_withheld() -> None:
    combined = sum((c.amount for c in TWO_CURRENCIES), Decimal(0))  # 55550.25: EUR + USD
    answer = f"Revenue across both currencies was {combined:,.2f}."
    assert ungrounded_figures(answer, outputs=outputs(TWO_CURRENCIES), question="") == (
        f"{combined:,.2f}",
    )
    # Each currency's own total, stated as such, is grounded.
    ok = "EUR revenue was 54,250.00 and USD revenue was 1,300.25."
    assert ungrounded_figures(ok, outputs=outputs(TWO_CURRENCIES), question="") == ()
