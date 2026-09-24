"""`get_hotel_kpis` — §7.2 row 1. The KPI snapshot for one bounded date range.

| §7.3 field | Value |
|---|---|
| delegates to | `AnalyticsService.overview`, unchanged |
| min role | viewer — the same as `GET …/analytics/overview` |
| input | `date_from`, `date_to`; nothing else is accepted |
| output | `OverviewResponse` without `hotel_public_id` |
| side effects | none |
| errors | the service's own `AppError`s (a bad range is a validation error), to the model |

`get_occupancy` and `get_revenue_metrics` from the original brief are folded in here, as §7.2
decided: the overview already returns both, and a second tool would be a second name for one call.
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
from app.schemas.analytics import (
    BookingStatusCounts,
    DateRange,
    MoneyByCurrency,
    OccupancyMetrics,
    ReviewMetrics,
    RoomRevenueByCurrency,
    StayFlowMetrics,
)


class HotelKpisArguments(DateRangeArguments):
    """The range to summarise."""


class HotelKpisOutput(ToolOutput):
    range: DateRange
    bookings_created: BookingStatusCounts
    bookings_by_stay: BookingStatusCounts
    stay_flow: StayFlowMetrics
    occupancy: OccupancyMetrics
    room_revenue: list[RoomRevenueByCurrency]
    other_revenue: list[MoneyByCurrency]
    ledger_room_revenue: list[MoneyByCurrency]
    total_expenses: list[MoneyByCurrency]
    net_operating_result: list[MoneyByCurrency]
    is_multi_currency: bool
    reviews: ReviewMetrics


CONTRACT = ToolContract(
    name="get_hotel_kpis",
    description=(
        "Key figures for the current hotel over an inclusive date range: bookings by status, "
        "arrivals and departures, occupancy, room revenue with ADR and RevPAR per currency, "
        "other revenue, expenses, net operating result and review totals."
    ),
    min_role=HotelRole.VIEWER,
    input_model=HotelKpisArguments,
    output_model=HotelKpisOutput,
    delegates_to="AnalyticsService.overview",
)


def run(context: ToolContext, arguments: HotelKpisArguments) -> HotelKpisOutput:
    response = context.services.analytics.overview(
        context.hotel_public_id, arguments.date_from, arguments.date_to
    )
    return HotelKpisOutput.model_validate(response.model_dump(exclude={HOTEL_IDENTIFIER_FIELD}))
