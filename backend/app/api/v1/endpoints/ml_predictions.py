"""The demand model's serving endpoint. HTTP concerns only.

``GET /hotels/{hotel_public_id}/ml/demand-forecast``. One route, read-only, hotel-scoped, and
nested under the hotel segment for the same reason every analytics route is: that segment is
where tenant isolation is established, and a portfolio-wide forecast would sit outside it.

**Separate from ``/intelligence``, deliberately.** That router serves the V1 statistical
forecaster -- seasonal day-of-week medians computed per request from the analytics series. This
one serves a V2 artifact: gradient-boosted trees fitted offline, versioned, checksummed and
loaded from disk. They answer a similar question by entirely different means, they carry
different version vocabularies, and putting them behind one prefix would invite a reader to
assume a lineage that does not exist.

What this router does **not** do, which is most of what a serving route could be tempted to do:

* it loads no artifact and opens no file;
* it imports no scikit-learn, no pickle and no SQLAlchemy;
* it computes no feature and builds no matrix;
* it raises no domain error and makes no authorization decision of its own;
* it accepts no artifact path, model version, feature column or estimator from the caller.

It reads two values off the URL and hands them to the service. Everything else is the service's.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from app.api.deps import DemandPredictionServiceDep
from app.schemas.common import ErrorResponse
from app.schemas.ml_serving import (
    MAX_REQUESTED_HORIZON_DAYS,
    SERVED_HORIZON_DAYS,
    DemandPredictionResponse,
)

router = APIRouter(prefix="/hotels/{hotel_public_id}/ml", tags=["ml"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the hotel.")]

TargetDate = Annotated[
    dt.date,
    Query(
        description=(
            "The day whose occupied room nights are forecast. Required and explicit: a "
            "today-relative default would make two identical requests return different "
            "predictions on different days."
        )
    ),
]

HorizonDays = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_REQUESTED_HORIZON_DAYS,
        description=(
            f"How far ahead the forecast is made, in whole days. The served model forecasts at "
            f"{SERVED_HORIZON_DAYS} days and no other value is accepted; it is a parameter so "
            "that a caller expecting a different horizon is told so rather than silently "
            "given this one."
        ),
    ),
]

RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "No hotel with this public identifier exists, or the caller is not a member of "
            "it. The two are deliberately indistinguishable."
        ),
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            f"The horizon is not {SERVED_HORIZON_DAYS} days, the target date is not a date, "
            "or the hotel has too little recorded demand history to compute the model's "
            "features for this date. No value is invented to fill a gap."
        ),
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": (
            "The approved model is not available on this server. The reason is recorded in "
            "the server log and is deliberately not described here."
        ),
    },
}


@router.get(
    "/demand-forecast",
    response_model=DemandPredictionResponse,
    summary="Forecast occupied room nights with the versioned demand model",
    description=(
        "Scores the approved offline artifact `demand_baseline_v1` for one hotel on one date. "
        "The response carries the model, feature and dataset versions, the prediction cutoff "
        "and the features used, so a number can be attributed and reproduced. The model is an "
        "offline research candidate: it has no established production accuracy and the "
        "response says so."
    ),
    responses=RESPONSES,
)
def get_demand_forecast(
    hotel_public_id: HotelPath,
    service: DemandPredictionServiceDep,
    target_date: TargetDate,
    horizon_days: HorizonDays = SERVED_HORIZON_DAYS,
) -> DemandPredictionResponse:
    """Identical requests return identical bodies. Nothing here reads a clock."""
    return service.forecast_demand(hotel_public_id, target_date, horizon_days)
