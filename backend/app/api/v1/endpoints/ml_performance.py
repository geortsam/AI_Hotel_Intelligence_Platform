"""Forecast-performance endpoints. HTTP concerns only.

Stage 7.3. Two routes, both read-only and hotel-scoped, on the same
``/hotels/{hotel_public_id}/ml`` prefix the serving routes use:

* ``GET .../ml/forecast-accuracy`` -- how far off this hotel's served predictions were;
* ``GET .../ml/prediction-distribution`` -- what those predictions and their inputs looked like.

**A separate module from ``ml_predictions.py``, sharing its prefix.** That module serves the
model: it turns a request into a score. These measure what it has already served. Same URL
namespace, because a caller looking for anything about the demand model should find it in one
place; different modules, because "produce a number" and "assess the numbers produced" are the
two responsibilities this stage exists to keep apart. The same argument that put the ML routes
under their own prefix rather than under ``/intelligence``.

What these routes do **not** do, which is most of what a measurement route could be tempted to
do:

* they compute no metric, no summary, no difference and no aggregate;
* they import no repository, no model, no SQLAlchemy, no scikit-learn and no protocol;
* they build no query and open no file;
* they make no authorization decision of their own beyond declaring the role they need;
* they accept no model version, protocol, threshold or hotel_id from the caller.

They read the URL and the query string and hand them to the service. Everything else is the
service's, and the arithmetic is the frozen offline modules' from Stages 6.9 and 6.10.

## The claims boundary

Neither route establishes production accuracy, evaluates a threshold, compares against a
baseline model, detects drift or ranks anything. Each response carries that in its own
``measurement`` block rather than only in this docstring, so a consumer reading the body is told
what the body does not establish.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import ForecastPerformanceServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse
from app.schemas.ml_performance import (
    ForecastAccuracyResponse,
    PredictionDistributionResponse,
)
from app.services.ml_performance import MAX_WINDOW_DAYS

router = APIRouter(prefix="/hotels/{hotel_public_id}/ml", tags=["ml"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the hotel.")]

AsOfDate = Annotated[
    dt.date,
    Query(
        description=(
            "The date the measurement is taken as of. Required and explicit: it decides which "
            "target dates have cleared the settlement lag, and a today-relative default would "
            "make the same request mean different things on different days."
        )
    ),
]

WindowFrom = Annotated[
    dt.date,
    Query(description="Earliest target date to include, inclusive."),
]

WindowTo = Annotated[
    dt.date,
    Query(description="Latest target date to include, inclusive."),
]

BaselineFrom = Annotated[
    dt.date | None,
    Query(
        description=(
            "Earliest target date of an optional reference window. Supply with `baseline_to` or "
            "not at all; half a pair is refused rather than ignored."
        )
    ),
]

BaselineTo = Annotated[
    dt.date | None,
    Query(description="Latest target date of the optional reference window, inclusive."),
]

#: Shared by both routes. Neither can 503: no artifact is loaded on this path, so there is no
#: model to be unavailable -- these read rows the model already produced.
RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "No hotel with this public identifier exists, or the caller is not a member of it. "
            "The two are deliberately indistinguishable."
        ),
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "A malformed window: a bound that is not a date, a `_from` later than its `_to`, or "
            f"a span longer than {MAX_WINDOW_DAYS} days. A correctly ordered window that holds "
            "no predictions is not an error -- it returns a result with zero candidates."
        ),
    },
}

ACCURACY_RESPONSES: dict[int | str, dict[str, Any]] = {
    **RESPONSES,
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": (
            "The caller is a member of this hotel but below the manager role. Distinct from "
            "404 on purpose: the hotel's existence is already known to a member."
        ),
    },
}


@router.get(
    "/forecast-accuracy",
    response_model=ForecastAccuracyResponse,
    summary="Measure this hotel's served predictions against what actually happened",
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
    description=(
        "Scores the predictions this property was served for target dates in the window against "
        "its recorded occupancy, under the frozen `accuracy_v1` protocol. Results are reported "
        "per model version and split into the protocol's two calibration segments, which never "
        "share a denominator; there is no combined headline figure, here or in the service "
        "behind it.\n\n"
        "Only target dates that have cleared the settlement lag by `as_of_date` are scored, so "
        "that late-recorded bookings are already in the ground truth; the rest are counted as "
        "`ineligible_by_settlement` and reported rather than silently dropped. `settled` is "
        "false when an allocation covering the scored window can still change status, which "
        "makes the measurement provisional.\n\n"
        "**This establishes no production accuracy.** It measures under a protocol fixed and "
        "checksummed before any number was computed, evaluates no threshold, compares against "
        "no baseline model and ranks nothing. Manager role, because how wrong a model has been "
        "is an operational judgement rather than a figure every member needs."
    ),
    responses=ACCURACY_RESPONSES,
)
def get_forecast_accuracy(
    hotel_public_id: HotelPath,
    service: ForecastPerformanceServiceDep,
    as_of_date: AsOfDate,
    window_from: WindowFrom,
    window_to: WindowTo,
) -> ForecastAccuracyResponse:
    """404 for an unknown hotel or a non-member, indistinguishably.

    Identical requests over unchanged rows return identical bodies: every bound is a parameter
    and nothing on this path reads a clock.
    """
    return service.forecast_accuracy(
        hotel_public_id,
        as_of_date=as_of_date,
        window_from=window_from,
        window_to=window_to,
    )


@router.get(
    "/prediction-distribution",
    response_model=PredictionDistributionResponse,
    summary="Summarise this hotel's stored predictions and their inputs over a window",
    description=(
        "Descriptive statistics -- count, minimum, maximum, mean, median and five quantiles -- "
        "over each of the model's input columns and its output, for the predictions this "
        "property was served in the window, under the frozen `distribution_v1` protocol. "
        "Reported per model version and per calibration segment, never pooled.\n\n"
        "Supply `baseline_from` and `baseline_to` to also receive a second window and the "
        "difference between the two, target minus baseline. Without them `comparison` is null, "
        "which is different from a comparison whose every difference happens to be zero.\n\n"
        "**This describes; it does not decide.** No threshold is evaluated, no verdict is "
        "reached, nothing is ranked and no alert is raised -- the protocol contains no such "
        "rule, so a difference reported here is a difference and not drift. Deciding what "
        "movement means is not something this endpoint, or the service behind it, does."
    ),
    responses=RESPONSES,
)
def get_prediction_distribution(
    hotel_public_id: HotelPath,
    service: ForecastPerformanceServiceDep,
    window_from: WindowFrom,
    window_to: WindowTo,
    baseline_from: BaselineFrom = None,
    baseline_to: BaselineTo = None,
) -> PredictionDistributionResponse:
    """Membership is enough, unlike the accuracy route.

    Every field summarised here is either calendar arithmetic or a figure this caller can
    already read in full from the analytics and stored-prediction endpoints at the same role.
    The argument is set out in ``app.schemas.ml_performance``.
    """
    return service.prediction_distribution(
        hotel_public_id,
        window_from=window_from,
        window_to=window_to,
        baseline_from=baseline_from,
        baseline_to=baseline_to,
    )
