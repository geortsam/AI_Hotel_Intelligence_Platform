"""The demand-prediction API contract: what a caller may ask, and what comes back.

**The request carries no model internals.** A caller supplies a hotel and a target date. It
cannot name an artifact, a file, an estimator, a feature column, a model version, a dataset or
a path, because no field here accepts one. The horizon is present and is validated against the
served model rather than obeyed -- see :data:`SERVED_HORIZON_DAYS` -- so a client can *state*
the horizon it believes it is asking for and be told when that belief is wrong, which is a
different thing from choosing one.

**The response is provenance-complete.** Model name, version, feature version, dataset version,
status, horizon, cutoff and the features used all travel with the number, so a prediction can
be attributed, reproduced and invalidated later without a second request. None of that is a
model internal: every one of those values is published in ``docs/ml-model-card.md``.

**There is no ``generated_at``.** Every other forecast response in this API carries one; this
one deliberately does not, because the endpoint's contract is that two identical requests
produce two byte-identical bodies, and a wall clock in the payload would quietly break that.

**No internal identifier appears anywhere.** The hotel is named by its public UUID, which is
the same identifier the URL uses, and the schema has no field a ``BIGINT`` key could travel in.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.ml.serving import APPROVED_MODEL

#: The horizon the served artifact forecasts at. A request may state this value and no other.
SERVED_HORIZON_DAYS = APPROVED_MODEL.forecast_horizon_days

#: Bound on the horizon a request may name at all. Anything inside the bound but different from
#: :data:`SERVED_HORIZON_DAYS` is refused by the service with a message that says what is served;
#: anything outside it never reaches the service. Two layers, because "14 is not the horizon this
#: model forecasts at" and "0 is not a horizon" are different mistakes.
MAX_REQUESTED_HORIZON_DAYS = 90


class DemandModelMetadata(BaseModel):
    """Which model produced the number, and what that model is.

    ``status`` and ``production_ready`` are in the payload rather than only in the
    documentation. This artifact is an offline research candidate: Stage 6.4 accepted it against
    a predeclared offline policy and established neither production accuracy nor cross-hotel
    generalisation. A consumer that reads this response is told so by the response.
    """

    model_config = ConfigDict(protected_namespaces=())

    model_name: str
    model_version: str
    feature_version: str
    dataset_version: str
    #: ``offline_research_candidate``. Not a production model, and it says so.
    status: str
    production_ready: bool
    #: One line describing the arithmetic, so the response explains itself.
    methodology: str


class DemandPredictionResponse(BaseModel):
    """One hotel, one date, one number, and everything needed to account for it."""

    model_config = ConfigDict(protected_namespaces=())

    hotel_public_id: uuid.UUID
    target_date: dt.date

    #: How far ahead this prediction is made, in whole days. Fixed by the served model.
    forecast_horizon_days: int
    #: The last day whose realised demand the prediction was allowed to see.
    cutoff_date: dt.date
    #: The instant a fact had to precede to be usable as a feature: midnight UTC ending
    #: ``cutoff_date``. Timezone-aware and always UTC, never a naive datetime.
    prediction_cutoff: dt.datetime

    #: The model's estimate of occupied room nights on ``target_date``. A real number, not a
    #: count: it is a regression output and rounding it here would discard the only information
    #: a consumer has about how close the estimate sits to a boundary.
    predicted_room_nights: float

    model: DemandModelMetadata
    #: The feature columns the prediction was computed from, in the order the model consumes
    #: them. Published in the model card already; repeated here so the response is self-
    #: describing.
    features_used: list[str] = Field(
        description="Feature columns used, in the model's own column order."
    )


__all__ = [
    "MAX_REQUESTED_HORIZON_DAYS",
    "SERVED_HORIZON_DAYS",
    "DemandModelMetadata",
    "DemandPredictionResponse",
]
