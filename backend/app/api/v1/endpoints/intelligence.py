"""Intelligence endpoints -- read-only forecasts, trend, anomalies and insights.

``/hotels/{hotel_public_id}/intelligence/...``. GET only, hotel-scoped, no flat route: a
portfolio-wide forecast would sit outside the segment where tenant isolation is established.

Every date is explicit and required. ``training_days`` and ``horizon_days`` are fixed counts
relative to the supplied dates, never "the last 90 days from today" -- an identical request
must return an identical prediction whenever it is made, and a today-relative default would
quietly break that.

What these endpoints do **not** do: change a rate, move a booking, allocate a room, post
revenue, schedule anything, or call an external service. They observe and explain.

HTTP concerns only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from app.api.deps import IntelligenceServiceDep
from app.schemas.common import ErrorResponse
from app.schemas.intelligence import (
    DEFAULT_HORIZON_DAYS,
    DEFAULT_TRAINING_DAYS,
    MAX_HORIZON_DAYS,
    MAX_OBSERVATION_DAYS,
    MAX_TRAINING_DAYS,
    MIN_TRAINING_DAYS,
    AnomalyResponse,
    DemandTrendResponse,
    InsightsResponse,
    OccupancyForecastResponse,
    RevenueForecastResponse,
)

router = APIRouter(prefix="/hotels/{hotel_public_id}/intelligence", tags=["intelligence"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the hotel.")]

HorizonFrom = Annotated[dt.date, Query(description="First forecast day, inclusive.")]
HorizonTo = Annotated[dt.date, Query(description="Last forecast day, inclusive.")]
WindowFrom = Annotated[dt.date, Query(description="First observed day, inclusive.")]
WindowTo = Annotated[dt.date, Query(description="Last observed day, inclusive.")]

TrainingDays = Annotated[
    int,
    Query(
        ge=MIN_TRAINING_DAYS,
        le=MAX_TRAINING_DAYS,
        description=(
            "Days of history immediately BEFORE date_from that the model may learn from. "
            "The training window always ends the day before the horizon starts, which is "
            "what prevents a forecast from seeing the period it is predicting."
        ),
    ),
]
HorizonDays = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_HORIZON_DAYS,
        description="Days to look ahead, starting the day after the observation window.",
    ),
]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No hotel with this public identifier exists.",
    }
}
INVALID_RANGE_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            f"The range is reversed, the horizon exceeds {MAX_HORIZON_DAYS} days, or the "
            f"observation window exceeds {MAX_OBSERVATION_DAYS} days."
        ),
    }
}
STANDARD_RESPONSES = {**NOT_FOUND_RESPONSE, **INVALID_RANGE_RESPONSE}


@router.get(
    "/forecast/occupancy",
    response_model=OccupancyForecastResponse,
    summary="Forecast occupied room nights",
    description="Each day carries the room nights already ON THE BOOKS as an actual figure "
    "beside the model's prediction. The two are never blended: one is a confirmed "
    "reservation, the other is an estimate.",
    responses=STANDARD_RESPONSES,
)
def get_occupancy_forecast(
    hotel_public_id: HotelPath,
    service: IntelligenceServiceDep,
    date_from: HorizonFrom,
    date_to: HorizonTo,
    training_days: TrainingDays = DEFAULT_TRAINING_DAYS,
) -> OccupancyForecastResponse:
    """404 for an unknown hotel; 422 for a reversed or over-long horizon.

    A day with too little history reports ``insufficient_data`` and no number.
    """
    return service.occupancy_forecast(hotel_public_id, date_from, date_to, training_days)


@router.get(
    "/forecast/revenue",
    response_model=RevenueForecastResponse,
    summary="Forecast room revenue, independently per currency",
    description="Room revenue comes from booking_room_nights.rate. Payments are not used as "
    "a proxy and room_types.base_price is not used as a price. Currencies are forecast "
    "separately and never combined.",
    responses=STANDARD_RESPONSES,
)
def get_revenue_forecast(
    hotel_public_id: HotelPath,
    service: IntelligenceServiceDep,
    date_from: HorizonFrom,
    date_to: HorizonTo,
    training_days: TrainingDays = DEFAULT_TRAINING_DAYS,
) -> RevenueForecastResponse:
    """Only currencies with real history are forecast; a currency the hotel has never traded
    in is absent rather than predicted at zero."""
    return service.revenue_forecast(hotel_public_id, date_from, date_to, training_days)


@router.get(
    "/demand-trend",
    response_model=DemandTrendResponse,
    summary="Detect increasing, decreasing or stable booking demand",
    description="Split-window median comparison over bookings.booked_at. Both halves' "
    "medians and the threshold are returned, so the classification can be recomputed by hand.",
    responses=STANDARD_RESPONSES,
)
def get_demand_trend(
    hotel_public_id: HotelPath,
    service: IntelligenceServiceDep,
    date_from: WindowFrom,
    date_to: WindowTo,
) -> DemandTrendResponse:
    return service.demand_trend(hotel_public_id, date_from, date_to)


@router.get(
    "/anomalies",
    response_model=AnomalyResponse,
    summary="Flag unusual occupancy, booking or revenue days",
    description="Modified z-score on the median absolute deviation. Every flag carries the "
    "metric, date, value, window median, MAD, score and threshold.",
    responses=STANDARD_RESPONSES,
)
def get_anomalies(
    hotel_public_id: HotelPath,
    service: IntelligenceServiceDep,
    date_from: WindowFrom,
    date_to: WindowTo,
) -> AnomalyResponse:
    """An empty list means nothing was unusual; ``metrics_scanned`` says what was examined."""
    return service.anomalies(hotel_public_id, date_from, date_to)


@router.get(
    "/insights",
    response_model=InsightsResponse,
    summary="Structured, deterministic findings",
    description="Assembled from the trend, anomaly and forecast outputs. Explanations are "
    "templates filled from the numbers each insight carries -- no generated prose.",
    responses=STANDARD_RESPONSES,
)
def get_insights(
    hotel_public_id: HotelPath,
    service: IntelligenceServiceDep,
    date_from: WindowFrom,
    date_to: WindowTo,
    horizon_days: HorizonDays = DEFAULT_HORIZON_DAYS,
) -> InsightsResponse:
    """The forecast horizon starts the day after the observation window, so the window is
    exactly the training data -- no gap, no overlap, no leakage."""
    return service.insights(hotel_public_id, date_from, date_to, horizon_days)
