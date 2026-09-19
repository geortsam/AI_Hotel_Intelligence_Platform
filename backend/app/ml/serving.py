"""The serving contract for ``demand_baseline_v1``: what may be served, and from what.

**Offline training and online inference are different activities and this module belongs to
the second one.** Training happens in ``ml/``, once, on a committed dataset, and produces a
versioned artifact. Inference happens here, per request, against that artifact and nothing
else. Nothing in this module fits, tunes, selects or updates a model; there is no estimator in
it at all.

This module is **pure** on the same terms as :mod:`app.ml.dataset` and
:mod:`app.ml.timeseries`: no SQLAlchemy, no session, no request, no FastAPI, no scikit-learn,
and no import of the offline ``ml`` package. It holds two things:

* :data:`APPROVED_MODEL` -- the one model this server is allowed to serve, pinned by identity
  rather than by whatever happens to be on disk;
* the feature assembly, which is the Stage 6.1 contract applied at a single target date.

## Which features exist at this horizon, and why only these

The artifact forecasts at **seven days**. Stage 6.3 computed admissibility from that horizon
rather than choosing it: a feature may be used only if its value is knowable at the prediction
cutoff, which is midnight UTC ending ``target_date - 7``. Of the fifteen columns in the Stage
6.1 contract, nine survive:

    day_of_week, day_of_month, month, week_of_year, day_of_year, is_weekend
        Derived from the target date alone. Knowable at any horizon, including infinite.

    demand_lag_7, demand_lag_14, demand_lag_28
        Realised demand 7, 14 and 28 days before the target date. The shortest of them lands
        exactly on the cutoff date, which is the last day whose demand is known.

The six that are excluded, and the reason each is excluded, are worth stating because their
absence is a decision rather than an oversight:

    demand_lag_1                        knowable one day ahead; inside a seven-day horizon
    demand_rolling_mean_{7,14,28}       their windows end the day before the target date
    on_books_room_nights_at_cutoff      reconstructed at the same one-day cutoff
    rooms_existing_at_cutoff            absent from every row of the training dataset, so it
                                        was never a column of this model

**No feature is fabricated to fill a gap.** A lag whose day is not in the extracted history is
missing, and a missing feature is :class:`InsufficientFeatureHistoryError` rather than a
zero. Zero is a real demand value -- a hotel that sold nothing is not a hotel with no
record -- and the training dataset dropped such rows rather than imputing them, so imputing
here would score a row of a kind the model never saw.

## What the server owns

Everything. The artifact, the model version, the feature version, the dataset version, the
feature columns, their order, the horizon and the cutoff are all fixed here. A request supplies
a hotel and a target date; it cannot name an artifact, a version, a column, an estimator or a
path, and there is no parameter through which it could.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.ml.dataset import calendar_features, cutoff_date_for, lag_features, prediction_cutoff

# --- the one approved model ---------------------------------------------------------------


class ServingError(Exception):
    """Base class for every refusal the serving boundary issues.

    Deliberately NOT an :class:`~app.core.errors.AppError`. These are facts about the model and
    its inputs, and the decision about what a client is told is made one layer up, by the
    service, in the project's existing error vocabulary. A domain error raised from here would
    be this module deciding an HTTP status, which is not its job.
    """


class ArtifactUnavailableError(ServingError):
    """The approved artifact could not be obtained: no ML runtime, or no artifact present."""


class ArtifactRejectedError(ServingError):
    """An artifact was found and refused. It is not the approved model, or it has been altered."""


class InsufficientFeatureHistoryError(ServingError):
    """This hotel has too little realised demand for the features this model requires."""


class InferenceFailedError(ServingError):
    """The verified model was asked for a prediction and did not produce a usable one."""


@dataclass(frozen=True, slots=True)
class ApprovedModel:
    """The identity of the only artifact this server will load.

    Pinned as data rather than read from whatever file is on disk. That inversion is the point:
    the artifact declares what it is, and this says what it must be, so a swapped, rebuilt or
    edited artifact is a mismatch rather than a new model quietly being served.

    ``canonical_model_digest`` is the cross-build fingerprint Stage 6.5 established -- a hash
    over the feature columns, the estimator configuration, the training extent and the model's
    predictions on a fixed synthetic probe grid. The payload's own SHA-256 is deliberately NOT
    pinned here: pickle bytes are a property of the interpreter, the scikit-learn build and the
    NumPy build, and Stage 6.5 makes no claim that they reproduce across toolchains. The
    payload digest is still checked -- against the artifact's own metadata, before a byte is
    deserialised -- which is the integrity question it can actually answer.
    """

    model_name: str
    model_version: str
    feature_version: str
    dataset_version: str
    dataset_sha256: str
    canonical_model_digest: str
    schema_version: str
    artifact_format: str
    forecast_horizon_days: int
    feature_columns: tuple[str, ...]
    lag_days: tuple[int, ...]

    @property
    def lookback_days(self) -> int:
        """How far back feature acquisition must reach. The longest lag, and nothing more."""
        return max(self.lag_days)


#: The claims block the approved artifact must carry, exactly.
#:
#: All four are ``false`` and all four must stay ``false``. This model is an offline research
#: candidate: Stage 6.4 accepted it against a predeclared offline policy and established
#: neither production accuracy nor cross-hotel generalisation, and Stage 6.5 recorded that in
#: the artifact. Serving it does not change any of that, and an artifact that claimed otherwise
#: would be an artifact somebody had edited -- which is why this is checked rather than ignored.
#:
#: ``serving_enabled`` is the subtle one. It stays ``false`` because the ARTIFACT grants no
#: serving approval; the approval lives here, in the server, as :data:`APPROVED_MODEL`. Keeping
#: the flag false means the offline loader's refusal of a self-authorising artifact remains in
#: force unchanged, and the one thing that can authorise serving is code review of this file.
EXPECTED_CLAIMS: Mapping[str, bool] = MappingProxyType(
    {
        "production_ready": False,
        "production_accuracy_established": False,
        "cross_hotel_generalisation_established": False,
        "serving_enabled": False,
    }
)

#: The single model this server serves. Every field is checked against the loaded artifact.
APPROVED_MODEL = ApprovedModel(
    model_name="demand_baseline",
    model_version="demand_baseline_v1",
    feature_version="v1",
    dataset_version="v1",
    dataset_sha256="904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d",
    canonical_model_digest="436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70",
    schema_version="artifact_v1",
    artifact_format="pickle",
    forecast_horizon_days=7,
    feature_columns=(
        "day_of_week",
        "day_of_month",
        "month",
        "week_of_year",
        "day_of_year",
        "is_weekend",
        "demand_lag_7",
        "demand_lag_14",
        "demand_lag_28",
    ),
    lag_days=(7, 14, 28),
)

#: A one-line description of what produced the number, carried in every response so a
#: prediction explains itself without a second lookup.
METHODOLOGY = (
    "Gradient-boosted regression trees (HistGradientBoostingRegressor) fitted offline on the "
    "Stage 6.2 demand dataset, forecasting occupied room nights seven days ahead from six "
    "calendar features and three realised-demand lags."
)

#: What the model is, stated in the response so nobody has to infer it from a version string.
MODEL_STATUS = "offline_research_candidate"


# --- the acquisition window ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureWindow:
    """The span of realised demand a single prediction is allowed to read.

    Both bounds inclusive, and ``date_to`` IS the cutoff date -- the last day whose demand is
    known at prediction time. Expressing the window this way is what makes leakage a property
    of the query bounds rather than of a filter somebody has to remember to apply: the
    extraction cannot return the target day, because the target day is outside the range it is
    given.
    """

    date_from: dt.date
    date_to: dt.date
    cutoff: dt.datetime

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1


def feature_window(target_date: dt.date, *, model: ApprovedModel = APPROVED_MODEL) -> FeatureWindow:
    """The one bounded range feature acquisition needs, derived from the model's own lags.

    ``date_to`` is ``cutoff_date_for(target_date, horizon)`` -- the shared Stage 6.1 definition,
    imported rather than restated, so the serving cutoff and the training cutoff cannot drift
    into two answers.
    """
    cutoff_day = cutoff_date_for(target_date, model.forecast_horizon_days)
    return FeatureWindow(
        date_from=target_date - dt.timedelta(days=model.lookback_days),
        date_to=cutoff_day,
        cutoff=prediction_cutoff(target_date, model.forecast_horizon_days),
    )


# --- feature assembly ----------------------------------------------------------------------


def build_feature_values(
    demand_by_date: Mapping[dt.date, int],
    target_date: dt.date,
    *,
    model: ApprovedModel = APPROVED_MODEL,
) -> dict[str, float]:
    """The exact feature vector this artifact consumes, in the exact order it consumes it.

    Assembled from the Stage 6.1 primitives -- :func:`~app.ml.dataset.calendar_features` and
    :func:`~app.ml.dataset.lag_features` -- rather than from a second implementation written
    for serving. A serving-only copy of "demand seven days ago" is how the features a model was
    trained on and the features it is asked to score quietly stop being the same thing.

    Order is part of the contract: the estimator consumes the row positionally, so a correct
    set of values in the wrong order is a wrong prediction rather than an error. The returned
    mapping is built by walking ``model.feature_columns``, and the result is checked against
    that tuple before it is returned.

    Raises :class:`InsufficientFeatureHistoryError` when any lag day is absent from
    *demand_by_date*. That is not a defensive nicety: the three lags reach 7, 14 and 28 days
    back, so a hotel with
    less than four weeks of recorded occupancy before the cutoff cannot be scored by this
    model, and saying so is more useful than a number computed from a filled-in zero.
    """
    calendar = calendar_features(target_date)
    lags = lag_features(
        demand_by_date, target_date, model.forecast_horizon_days, list(model.lag_days)
    )

    missing = sorted(name for name, value in lags.items() if value is None)
    if missing:
        raise InsufficientFeatureHistoryError(
            f"{len(missing)} of {len(lags)} demand lags have no recorded value at or before "
            f"the cutoff for {target_date.isoformat()}"
        )

    available: dict[str, int | None] = {**calendar, **lags}
    values: dict[str, float] = {}
    for name in model.feature_columns:
        value = available.get(name)
        if value is None:
            # Unreachable while the columns and the two producers agree; asserted rather than
            # assumed, because the failure it guards against is a silently short row.
            raise ArtifactRejectedError(
                f"the serving contract names feature {name!r}, which nothing here produces"
            )
        values[name] = float(value)

    if tuple(values) != model.feature_columns:
        raise ArtifactRejectedError(
            "the assembled feature vector is not in the model's column order"
        )
    return values


__all__ = [
    "APPROVED_MODEL",
    "EXPECTED_CLAIMS",
    "METHODOLOGY",
    "MODEL_STATUS",
    "ApprovedModel",
    "ArtifactRejectedError",
    "ArtifactUnavailableError",
    "FeatureWindow",
    "InferenceFailedError",
    "InsufficientFeatureHistoryError",
    "ServingError",
    "build_feature_values",
    "feature_window",
]
