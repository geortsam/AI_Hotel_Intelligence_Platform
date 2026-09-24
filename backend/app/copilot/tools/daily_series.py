"""`get_daily_series` — §7.2 row 2. The overview's measures, one row per day.

| §7.3 field | Value |
|---|---|
| delegates to | `AnalyticsService.daily`, unchanged |
| min role | viewer — the same as `GET …/analytics/daily` |
| input | `date_from`, `date_to`; the service caps the span |
| output | `DailySeriesResponse` without `hotel_public_id` |
| side effects | none |
| errors | the service's own `AppError`s, returned to the model |
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
from app.schemas.analytics import DailyMetricsRow, DateRange


class DailySeriesArguments(DateRangeArguments):
    """The range to break down by day."""


class DailySeriesOutput(ToolOutput):
    range: DateRange
    days: list[DailyMetricsRow]


CONTRACT = ToolContract(
    name="get_daily_series",
    description=(
        "A gap-free daily series for the current hotel over an inclusive date range: occupied "
        "and available room nights, occupancy, room and other revenue, expenses, arrivals, "
        "departures, bookings created and cancellations for each day."
    ),
    min_role=HotelRole.VIEWER,
    input_model=DailySeriesArguments,
    output_model=DailySeriesOutput,
    delegates_to="AnalyticsService.daily",
)


def run(context: ToolContext, arguments: DailySeriesArguments) -> DailySeriesOutput:
    response = context.services.analytics.daily(
        context.hotel_public_id, arguments.date_from, arguments.date_to
    )
    return DailySeriesOutput.model_validate(response.model_dump(exclude={HOTEL_IDENTIFIER_FIELD}))
