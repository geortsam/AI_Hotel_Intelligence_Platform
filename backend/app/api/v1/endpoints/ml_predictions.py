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

from app.api.deps import DemandPredictionReadServiceDep, DemandPredictionServiceDep
from app.schemas.common import ErrorResponse, Page
from app.schemas.ml_prediction_read import StoredDemandPredictionResponse
from app.schemas.ml_serving import (
    MAX_REQUESTED_HORIZON_DAYS,
    SERVED_HORIZON_DAYS,
    DemandPredictionResponse,
)
from app.services.ml_prediction_read import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

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


#: The read path's failures, which are a strict subset of the serving path's: there is no
#: model to be unavailable when nothing is scored.
READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: RESPONSES[status.HTTP_404_NOT_FOUND],
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "A malformed window or page: date_from later than date_to, a date that is not a "
            "date, or a page size outside 1..100. An empty window is not an error -- it "
            "returns an empty page."
        ),
    },
}

DateFrom = Annotated[
    dt.date,
    Query(
        description=(
            "Earliest target_date to include, inclusive. Required and explicit: a "
            "today-relative default would make the same request mean different things on "
            "different days."
        )
    ),
]

DateTo = Annotated[
    dt.date,
    Query(description="Latest target_date to include, inclusive."),
]


@router.get(
    "/demand-predictions",
    response_model=Page[StoredDemandPredictionResponse],
    summary="List this hotel's stored demand predictions",
    description=(
        "Every prediction this property was served whose target date falls in the window, "
        "oldest first. Read-only: there is no endpoint that writes, edits or deletes a stored "
        "prediction. Rows are returned exactly as they were stored -- if the same target date "
        "was forecast more than once, every one of those predictions appears, because each was "
        "computed from different recorded history. Requires membership, the same level the "
        "forecast route requires. The model is an offline research candidate with no "
        "established production accuracy; listing a number is not an endorsement of it."
    ),
    responses=READ_RESPONSES,
)
def list_stored_demand_predictions(
    hotel_public_id: HotelPath,
    service: DemandPredictionReadServiceDep,
    date_from: DateFrom,
    date_to: DateTo,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[StoredDemandPredictionResponse]:
    """404 for an unknown hotel or a non-member, indistinguishably.

    Identical requests over unchanged rows return identical pages: every bound is a parameter
    and the order is total, so nothing here depends on when it is asked.
    """
    return service.list_predictions(
        hotel_public_id,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


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
