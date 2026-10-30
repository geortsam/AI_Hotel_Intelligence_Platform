"""`get_revenue_breakdown` — §7.2 row 3. Ledger revenue by category and currency.

| §7.3 field | Value |
|---|---|
| delegates to | `AnalyticsService.revenue_breakdown`, unchanged |
| min role | viewer — the same as `GET …/analytics/revenue-by-category` |
| input | `date_from`, `date_to` |
| output | `RevenueBreakdownResponse` without `hotel_public_id`, plus `totals` (R2.8P) |
| side effects | none |
| errors | the service's own `AppError`s, returned to the model |

§7.2 named the method `AnalyticsService.revenue_by_category`. No such service method exists:
`revenue-by-category` is the route's path and `revenue_by_category` the *repository* method, and
the service method the route calls is `revenue_breakdown`. Corrected in Amendment A1 rather than
papered over with an alias.

Categories are reported by their public `category_code`, never by an internal key.

## Totals per currency (R2.8P)

The prompt forbids the model to add figures up, and the figure check withholds any answer that
states a sum no tool returned (`app.copilot.grounding`). So a correct breakdown followed by
"Total revenue: …" was withheld whole (R2.8N-U, `revenue_by_category`). The server now does the
adding: `totals` holds one entry per currency -- `amount`, `tax_amount` and `entry_count` summed
over that currency's categories -- and nothing else.

- **Never across currencies.** There is no exchange rate (approved decision 14), so amounts in
  different currencies are never added together, and no total is labelled anything but its own
  currency. A model that adds them states a figure no tool returned, and is withheld as before.
- **Exact, from the same rows.** The categories are the service's own response, built from one
  query; each amount is an exact SQL `SUM` of `Numeric(14, 2)` ledger columns, quantised to
  `MONEY_PLACES` (0.01) without loss. The totals add those `Decimal`s -- never a rounded or
  formatted string -- so a currency's total is the sum of its ledger lines.
- **Entry counts** add the categories' counts: every ledger line is in exactly one category.
- **An empty range** has no categories, so it has no totals: `[]`, as `categories` is.

The REST response (`RevenueBreakdownResponse`) is unchanged: this is the copilot's view only.
"""

from __future__ import annotations

import decimal
from collections.abc import Iterable

from pydantic import BaseModel

from app.copilot.contracts import (
    HOTEL_IDENTIFIER_FIELD,
    DateRangeArguments,
    ToolContext,
    ToolContract,
    ToolOutput,
)
from app.models.enums import HotelRole
from app.schemas.analytics import DateRange, RevenueCategoryBreakdown


class RevenueBreakdownArguments(DateRangeArguments):
    """The range whose ledger revenue to break down."""


class RevenueCurrencyTotal(BaseModel):
    """One currency's totals over the categories in the breakdown. Never mixes currencies."""

    currency: str
    amount: decimal.Decimal
    tax_amount: decimal.Decimal
    entry_count: int


class RevenueBreakdownOutput(ToolOutput):
    range: DateRange
    categories: list[RevenueCategoryBreakdown]
    #: One entry per currency, in currency order (R2.8P). See the module docstring.
    totals: list[RevenueCurrencyTotal]


def currency_totals(categories: Iterable[RevenueCategoryBreakdown]) -> list[RevenueCurrencyTotal]:
    """Each currency's `amount`, `tax_amount` and `entry_count`, summed over its categories with
    exact `Decimal` arithmetic and keyed by currency alone, so no two currencies are ever
    combined. Ordered by currency code, so the output is deterministic."""
    sums: dict[str, tuple[decimal.Decimal, decimal.Decimal, int]] = {}
    for category in categories:
        amount, tax, entries = sums.get(
            category.currency, (decimal.Decimal(0), decimal.Decimal(0), 0)
        )
        sums[category.currency] = (
            amount + category.amount,
            tax + category.tax_amount,
            entries + category.entry_count,
        )
    return [
        RevenueCurrencyTotal(currency=currency, amount=amount, tax_amount=tax, entry_count=count)
        for currency, (amount, tax, count) in sorted(sums.items())
    ]


CONTRACT = ToolContract(
    name="get_revenue_breakdown",
    description=(
        "Ledger revenue for the current hotel over an inclusive date range, by revenue "
        "category code and currency, with tax and entry counts. Room revenue from stay nights "
        "is reported by get_hotel_kpis, not here. Totals per currency are included; amounts in "
        "different currencies are never added together."
    ),
    min_role=HotelRole.VIEWER,
    input_model=RevenueBreakdownArguments,
    output_model=RevenueBreakdownOutput,
    delegates_to="AnalyticsService.revenue_breakdown",
)


def run(context: ToolContext, arguments: RevenueBreakdownArguments) -> RevenueBreakdownOutput:
    response = context.services.analytics.revenue_breakdown(
        context.hotel_public_id, arguments.date_from, arguments.date_to
    )
    return RevenueBreakdownOutput.model_validate(
        {
            **response.model_dump(exclude={HOTEL_IDENTIFIER_FIELD}),
            "totals": [total.model_dump() for total in currency_totals(response.categories)],
        }
    )
