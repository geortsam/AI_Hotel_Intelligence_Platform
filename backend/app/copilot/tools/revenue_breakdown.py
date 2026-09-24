"""`get_revenue_breakdown` — §7.2 row 3. Ledger revenue by category and currency.

| §7.3 field | Value |
|---|---|
| delegates to | `AnalyticsService.revenue_breakdown`, unchanged |
| min role | viewer — the same as `GET …/analytics/revenue-by-category` |
| input | `date_from`, `date_to` |
| output | `RevenueBreakdownResponse` without `hotel_public_id` |
| side effects | none |
| errors | the service's own `AppError`s, returned to the model |

§7.2 named the method `AnalyticsService.revenue_by_category`. No such service method exists:
`revenue-by-category` is the route's path and `revenue_by_category` the *repository* method, and
the service method the route calls is `revenue_breakdown`. Corrected in Amendment A1 rather than
papered over with an alias.

Categories are reported by their public `category_code`, never by an internal key.
"""

from __future__ import annotations

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


class RevenueBreakdownOutput(ToolOutput):
    range: DateRange
    categories: list[RevenueCategoryBreakdown]


CONTRACT = ToolContract(
    name="get_revenue_breakdown",
    description=(
        "Ledger revenue for the current hotel over an inclusive date range, by revenue "
        "category code and currency, with tax and entry counts. Room revenue from stay nights "
        "is reported by get_hotel_kpis, not here."
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
        response.model_dump(exclude={HOTEL_IDENTIFIER_FIELD})
    )
