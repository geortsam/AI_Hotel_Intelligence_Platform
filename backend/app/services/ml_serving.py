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

**No longer read-only, as of Stage 6.8.** This service now owns a transaction boundary: the
prediction it returns and the row recording it commit together, or neither happens. That is a
deliberate change from Stages 6.6-6.7, argued in ``docs/ml-prediction-persistence-design.md``
§16 -- the precedent is the audit trail, which writes a record of what happened inside the
transaction of the thing that happened.

It still alters no **operational** data. It writes one row to ``demand_predictions`` and nothing
else; no booking, rate, room or ledger entry is touched by asking for a forecast, and the
endpoint stays idempotent because a repeated request writes no second row.

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

**A refusal writes nothing.** 401, 404, 422 and 503 all leave the table exactly as they found
it, because the row is written only after a prediction exists.

**A persistence failure fails the request.** If the row cannot be written, no prediction is
returned. A served-but-unrecorded prediction would make the table's completeness unverifiable,
and completeness is the only thing the table is for: an absent row has to mean "not served"
rather than "served, but we lost it".

## Observability

Exactly one structured event per serving attempt that got past authorization, carrying the
outcome, the model version, the horizon and the duration -- and nothing else. No prediction
value, no feature value, no hotel identifier, no digest, no path. Those are the hotel's business
data and the server's internals respectively; the row holds the first and nobody needs the
second. Counts come from the table, which is why they are not counters here.

401 and 404 emit no event. The serving attempt begins once membership is established, and a
caller who is not a member should leave no trace on the ML path at all.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    InsufficientHistoryError,
    ModelUnavailableError,
    ValidationError,
    internal_fault,
)
from app.core.request_id import current_request_id
from app.ml.artifact_store import ServedModel, approved_model, predict_room_nights
from app.ml.serving import (
    APPROVED_MODEL,
    METHODOLOGY,
    MODEL_STATUS,
    ApprovedModel,
    ArtifactRejectedError,
    ArtifactUnavailableError,
    FeatureWindow,
    InsufficientFeatureHistoryError,
    build_feature_values,
    feature_digest,
    feature_window,
)
from app.models.hotel import Hotel
from app.repositories.ml_demand import MlDemandRepository
from app.repositories.ml_prediction import MlPredictionRepository
from app.schemas.ml_serving import DemandModelMetadata, DemandPredictionResponse
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

#: The outcomes a serving attempt can report. Five, fixed, and the vocabulary an operator reads.
#:
#: ``INFERENCE_FAILED`` is the terminal one: the request got past authorization and the model was
#: available, but no prediction was returned. That covers an estimator failure and a failure to
#: record the prediction, because from outside they are the same event -- the platform did not
#: answer.
SERVED = "served"
MODEL_UNAVAILABLE = "model_unavailable"
INSUFFICIENT_HISTORY = "insufficient_history"
INVALID_REQUEST = "invalid_request"
INFERENCE_FAILED = "inference_failed"


class DemandPredictionService:
    """One prediction, for one hotel, on one date. Deliberately nothing else.

    No batch method and no date range, for the same reason :class:`MlDemandRepository` takes one
    hotel at a time: a method that answers for many is one edit away from a method that answers
    for all, and "every hotel's forecast in one result" is exactly the query this domain must
    not own. A caller that needs a week asks seven times, and every one of those asks is
    separately authorised.
    """

    def __init__(
        self,
        session: Session,
        repository: MlDemandRepository,
        predictions: MlPredictionRepository,
        scope: HotelScopeResolver,
    ) -> None:
        # The session is held because this service owns a unit of work, exactly as every other
        # writing service does. The repositories hold it too, for their queries; neither of
        # them commits.
        self._session = session
        self._repository = repository
        self._predictions = predictions
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
        identical requests. ``generated_at`` is recorded on the row and deliberately kept out of
        the response, so persistence cannot contaminate the number or the contract.
        """
        # Authorization and tenancy first, always. Everything below this line is about a hotel
        # the caller has already been shown to be a member of -- and nothing above it is
        # observed, so a caller who is not a member leaves no trace on the ML path.
        hotel = self._scope.require_hotel(hotel_public_id)

        started = time.perf_counter()
        outcome = INFERENCE_FAILED
        try:
            response = self._serve(hotel, target_date, horizon_days)
        except ValidationError:
            outcome = INVALID_REQUEST
            raise
        except InsufficientHistoryError:
            outcome = INSUFFICIENT_HISTORY
            raise
        except ModelUnavailableError:
            outcome = MODEL_UNAVAILABLE
            raise
        else:
            outcome = SERVED
            return response
        finally:
            self._observe(outcome, time.perf_counter() - started)

    def _serve(
        self, hotel: Hotel, target_date: dt.date, horizon_days: int
    ) -> DemandPredictionResponse:
        """Validate, acquire, score, record, commit. The order is the guarantee."""
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

        # The prediction and its record commit together. Nothing has been written before this
        # point, which is what makes every refusal above leave the table untouched.
        self._record(hotel, target_date, window, features, value, model)

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

    def _record(
        self,
        hotel: Hotel,
        target_date: dt.date,
        window: FeatureWindow,
        features: dict[str, float],
        value: float,
        model: ApprovedModel,
    ) -> None:
        """Write the row and commit, or roll back and refuse to return a prediction.

        ``ON CONFLICT DO NOTHING`` inside the repository means a repeat writes nothing and
        raises nothing, so there is no uniqueness race to translate here -- two concurrent
        identical requests produce one row and two identical responses. The
        :class:`IntegrityError` branch therefore covers what is left: a hotel deleted underneath
        the request, or a value the table's CHECK constraints refuse. Both are server faults the
        caller cannot act on, and both are reported through the shared factory so the SQLSTATE
        and the relation reach the log and nothing reaches the client.
        """
        try:
            self._predictions.record(
                hotel_id=hotel.id,
                target_date=target_date,
                forecast_horizon_days=model.forecast_horizon_days,
                prediction_cutoff=window.cutoff,
                predicted_room_nights=value,
                model_name=model.model_name,
                model_version=model.model_version,
                feature_version=model.feature_version,
                dataset_version=model.dataset_version,
                canonical_model_digest=model.canonical_model_digest,
                feature_values=dict(features),
                feature_digest=feature_digest(features),
                request_id=current_request_id(),
            )
            self._session.commit()
        except IntegrityError as error:
            self._session.rollback()
            raise internal_fault(error) from error

    def _observe(self, outcome: str, seconds: float) -> None:
        """One event per serving attempt. Four fields, and nothing a hotel owns.

        The request id is deliberately NOT passed here: ``RequestIdFilter`` already attaches it
        to every record, and setting it through ``extra`` would be a second writer of one field.
        """
        logger.info(
            "demand forecast %s (model=%s, horizon=%sd) in %.1f ms",
            outcome,
            APPROVED_MODEL.model_version,
            APPROVED_MODEL.forecast_horizon_days,
            seconds * 1000,
            extra={
                "outcome": outcome,
                "model_version": APPROVED_MODEL.model_version,
                "forecast_horizon_days": APPROVED_MODEL.forecast_horizon_days,
                "duration_ms": round(seconds * 1000, 3),
            },
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
