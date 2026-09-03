"""Analytics endpoints -- read-only, hotel-scoped.

``/hotels/{hotel_public_id}/analytics/...``. There is no flat ``/analytics``, no ``/reports``
and no ``/dashboard``: the hotel segment is not decoration, it is where tenant isolation is
established, and a portfolio-wide analytics route would sit outside it.

**GET only.** Every route here observes; none changes anything. In particular nothing
populates ``daily_hotel_metrics`` -- that table is an empty snapshot awaiting a job that does
not exist, and filling it from a read request would make analytics mutate the business it is
supposed to be measuring.

Responses are typed projections. No raw rows, no internal BIGINT, and every monetary figure
arrives as per-currency buckets because the ledger stores currency per line.

HTTP concerns only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from app.api.deps import AnalyticsServiceDep
from app.schemas.analytics import (
    MAX_RANGE_DAYS,
    DailySeriesResponse,
    ExpenseBreakdownResponse,
    OverviewResponse,
    RevenueBreakdownResponse,
    ReviewAnalyticsResponse,
)
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/hotels/{hotel_public_id}/analytics", tags=["analytics"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the hotel.")]

#: Both bounds are inclusive and both are required. There is no implicit "last 30 days":
#: a default window would make two identical-looking requests mean different things on
#: different days, which is the opposite of what a reporting API should do.
DateFrom = Annotated[dt.date, Query(description="Inclusive start of the range.")]
DateTo = Annotated[dt.date, Query(description="Inclusive end of the range.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No hotel with this public identifier exists.",
    }
}
INVALID_RANGE_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": f"The range is reversed or spans more than {MAX_RANGE_DAYS} days.",
    }
}
STANDARD_RESPONSES = {**NOT_FOUND_RESPONSE, **INVALID_RANGE_RESPONSE}


@router.get(
    "/overview",
    response_model=OverviewResponse,
    summary="Hotel KPI snapshot for a date range",
    description="Bookings, occupancy, room and ledger revenue, expenses and reviews. Every "
    "monetary figure is bucketed by currency; nothing is converted or summed across "
    "currencies.",
    responses=STANDARD_RESPONSES,
)
def get_overview(
    hotel_public_id: HotelPath,
    service: AnalyticsServiceDep,
    date_from: DateFrom,
    date_to: DateTo,
) -> OverviewResponse:
    """404 for an unknown hotel; 422 for a reversed or over-long range."""
    return service.overview(hotel_public_id, date_from, date_to)


@router.get(
    "/daily",
    response_model=DailySeriesResponse,
    summary="Daily time series",
    description="One row per calendar day, ascending, including days with no activity. "
    "Suitable for charting directly; no presentation formatting is applied.",
    responses=STANDARD_RESPONSES,
)
def get_daily_series(
    hotel_public_id: HotelPath,
    service: AnalyticsServiceDep,
    date_from: DateFrom,
    date_to: DateTo,
) -> DailySeriesResponse:
    """Gap-free and deterministic: the same data always yields the same series."""
    return service.daily(hotel_public_id, date_from, date_to)


@router.get(
    "/revenue-by-category",
    response_model=RevenueBreakdownResponse,
    summary="Ledger revenue by category and currency",
    description="Room revenue from stay nights is NOT included: it lives in "
    "booking_room_nights, not the ledger. Each row carries is_room_revenue so a consumer can "
    "apply the schema's own exclusion rule.",
    responses=STANDARD_RESPONSES,
)
def get_revenue_breakdown(
    hotel_public_id: HotelPath,
    service: AnalyticsServiceDep,
    date_from: DateFrom,
    date_to: DateTo,
) -> RevenueBreakdownResponse:
    return service.revenue_breakdown(hotel_public_id, date_from, date_to)


@router.get(
    "/expenses-by-category",
    response_model=ExpenseBreakdownResponse,
    summary="Ledger expenses by category and currency",
    description="Each row carries is_fixed_cost, for the fixed/variable split.",
    responses=STANDARD_RESPONSES,
)
def get_expense_breakdown(
    hotel_public_id: HotelPath,
    service: AnalyticsServiceDep,
    date_from: DateFrom,
    date_to: DateTo,
) -> ExpenseBreakdownResponse:
    return service.expense_breakdown(hotel_public_id, date_from, date_to)


@router.get(
    "/reviews",
    response_model=ReviewAnalyticsResponse,
    summary="Review analytics",
    description="Averages use rating_normalized, the PostgreSQL-generated rating / "
    "rating_scale, which is the only figure comparable across five- and ten-point sources.",
    responses=STANDARD_RESPONSES,
)
def get_review_analytics(
    hotel_public_id: HotelPath,
    service: AnalyticsServiceDep,
    date_from: DateFrom,
    date_to: DateTo,
) -> ReviewAnalyticsResponse:
    """Includes reviews with no guest and no booking; the schema permits them."""
    return service.reviews(hotel_public_id, date_from, date_to)
