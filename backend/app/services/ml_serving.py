"""Serving the approved demand model: resolve, acquire, score, translate.

Stage 6.6. **This service trains nothing, fits nothing and changes no artifact.** It is the
orchestration between four things that already exist, and it adds no ML of its own:

    HotelScopeResolver      who may reach this hotel, and which row it is   (app.services.scope)
        |
    MlDemandRepository      one grouped query for realised demand     (app.repositories)
        |
    app.ml.serving          the Stage 6.1 features at one target date       (pure, no SQL)
        |
    app.ml.artifact_store   the verified artifact and the Stage 6.5 contract
        |
    DemandPredictionResponse

**Read-only.** Nothing here commits or rolls back, because it writes nothing -- the same
asymmetry the architecture audit checks for on the analytics and intelligence services, and the
honest signal that asking for a forecast cannot alter operational data.

## Tenant isolation

The hotel is resolved FIRST, through the shared scope resolver, before anything else happens.
That ordering is the isolation guarantee and it is not incidental: membership is established
before an artifact is consulted, before a query is issued and before a feature is computed, so
a caller who is not a member of a hotel cannot learn whether the model is loaded, whether that
hotel has history, or whether it exists at all. They get the hotel's own 404, byte-identical to
the one an unknown identifier produces.

Extraction is then bounded by the internal ``hotel_id`` of the row the resolver returned, and
by nothing a caller supplied. There is no method here that reads more than one hotel and no
parameter through which a second one could be named.

## Leakage

The prediction for ``target_date`` may only read demand up to ``target_date - 7``. That is
enforced by the **bounds of the one query**, not by a filter applied afterwards:
:func:`app.ml.serving.feature_window` derives the range from the model's own lags and horizon,
and its upper bound IS the cutoff date. The extraction is never given a date the forecaster is
not allowed to have seen, so there is no later step that could forget to exclude one.

No booking, revenue, payment or review information from on or after the target date is read at
all -- the single query counts realised room nights inside the window and nothing else.

## Errors

Every refusal below is one of the project's existing :class:`~app.core.errors.AppError` types.
No second error system is introduced, no exception from scikit-learn, pickle or SQLAlchemy is
allowed to escape as itself, and no message names a path, an artifact, a version, a table or a
library.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.core.errors import InsufficientHistoryError, ModelUnavailableError, ValidationError
from app.ml.artifact_store import ServedModel, approved_model, predict_room_nights
from app.ml.serving import (
    APPROVED_MODEL,
    METHODOLOGY,
    MODEL_STATUS,
    ArtifactRejectedError,
    ArtifactUnavailableError,
    InsufficientFeatureHistoryError,
    build_feature_values,
    feature_window,
)
from app.repositories.ml_demand import MlDemandRepository
from app.schemas.ml_serving import DemandModelMetadata, DemandPredictionResponse
from app.services.scope import HotelScopeResolver


class DemandPredictionService:
    """One prediction, for one hotel, on one date. Deliberately nothing else.

    No batch method and no date range, for the same reason :class:`MlDemandRepository` takes one
    hotel at a time: a method that answers for many is one edit away from a method that answers
    for all, and "every hotel's forecast in one result" is exactly the query this domain must
    not own. A caller that needs a week asks seven times, and every one of those asks is
    separately authorised.
    """

    def __init__(self, repository: MlDemandRepository, scope: HotelScopeResolver) -> None:
        self._repository = repository
        self._scope = scope

    def forecast_demand(
        self,
        hotel_public_id: uuid.UUID,
        target_date: dt.date,
        horizon_days: int,
    ) -> DemandPredictionResponse:
        """Forecast occupied room nights for *target_date*, seven days out.

        Deterministic: the same hotel, the same date and the same recorded history produce the
        same number on every call. There is no clock in the computation, no random state, no
        cache of previous answers, and no field in the response that varies between two
        identical requests.
        """
        # Authorization and tenancy first, always. Everything below this line is about a hotel
        # the caller has already been shown to be a member of.
        hotel = self._scope.require_hotel(hotel_public_id)

        model = APPROVED_MODEL
        if horizon_days != model.forecast_horizon_days:
            raise ValidationError(
                f"This model forecasts {model.forecast_horizon_days} days ahead. "
                f"horizon_days must be {model.forecast_horizon_days}."
            )

        served = self._require_model()
        window = feature_window(target_date, model=model)

        # One grouped query, for one hotel, bounded at the cutoff. Not one per lag: three
        # single-date lookups would be three round trips for a range 22 days wide, and the
        # pattern that starts as three is the pattern that becomes N.
        demand = self._repository.demand_by_date(hotel.id, window.date_from, window.date_to)

        try:
            features = build_feature_values(demand, target_date, model=model)
        except InsufficientFeatureHistoryError as error:
            raise InsufficientHistoryError from error

        value = predict_room_nights(
            served,
            hotel_public_id=hotel.public_id,
            target_date=target_date,
            features=features,
        )

        return DemandPredictionResponse(
            hotel_public_id=hotel.public_id,
            target_date=target_date,
            forecast_horizon_days=model.forecast_horizon_days,
            cutoff_date=window.date_to,
            prediction_cutoff=window.cutoff,
            predicted_room_nights=value,
            model=DemandModelMetadata(
                model_name=model.model_name,
                model_version=model.model_version,
                feature_version=model.feature_version,
                dataset_version=model.dataset_version,
                status=MODEL_STATUS,
                production_ready=False,
                methodology=METHODOLOGY,
            ),
            features_used=list(model.feature_columns),
        )

    def _require_model(self) -> ServedModel:
        """The verified artifact, or one fixed sentence.

        Both refusals collapse into :class:`~app.core.errors.ModelUnavailableError` on purpose.
        "The runtime is missing", "the file is absent" and "the digest does not match" are three
        different operational facts and one identical client experience, because the difference
        between them describes this server rather than this request. The detail is already in
        the log, written where the check failed.

        ``raise ... from None`` rather than ``from error``: the chained cause would carry the
        refusal's own message -- which names the mismatching field -- into the traceback the
        unexpected-error handler would render if this ever escaped.
        """
        try:
            return approved_model()
        except (ArtifactUnavailableError, ArtifactRejectedError):
            raise ModelUnavailableError from None


__all__ = ["DemandPredictionService"]
