"""The HTTP boundary over the two frozen measurement services. Reads only, writes nothing.

Stage 7.3. ``DemandAccuracyService`` (6.9) and ``DemandDistributionService`` (6.10) are complete,
tested, protocol-checksummed and unchanged by this stage. What they lacked was a way in from the
network. This is that way, and it is deliberately the thinnest thing that could be one.

    ForecastPerformanceService
        |
        |-- validate the window                     (a malformed window is not an empty one)
        |-- DemandAccuracyService.evaluate          (resolves the hotel, measures)
        |-- DemandDistributionService.observe       (resolves the hotel, summarises)
        |
    project the frozen result onto the published contract   (copy, drop, never compute)

## Why this exists at all, rather than the router calling the services directly

Three jobs have to happen between an HTTP request and a published body, and none of them belongs
in a router:

1. **Window validation.** Neither frozen service checks that ``window_from <= window_to``. Given
   a reversed window both return an empty result rather than an error, which is precisely the
   silence Stage 6.11 refused to ship -- "returning an empty page would let a client mistake a
   typo for a quiet period". The check is a rule about a request, so it lives in a service and
   raises the same ``ValidationError`` every other service raises.
2. **Bounding.** An unbounded window is an unbounded scan. Every other read in this codebase is
   bounded by construction, and this one is bounded by :data:`MAX_WINDOW_DAYS`.
3. **Projection.** The frozen results carry two fields that must not cross the network. Dropping
   them is a decision about disclosure, argued in ``app.schemas.ml_performance``; executing it in
   a router would put that decision where nobody looks for it.

**Nothing here computes a metric.** Every number in every response is copied from the value the
frozen service returned -- no arithmetic, no re-aggregation, no rounding, no default substituted
for a ``None``. The projection methods below are field copies, and a test asserts that this
module contains no arithmetic operator on a measured value.

## Read-only, and structurally so

It holds **no session**, like the two services it wraps: it cannot commit, roll back or flush
because it has nothing to do those things to. It builds no SQLAlchemy query and imports no
repository, no model and no scikit-learn.

## Tenant isolation

This service resolves nothing itself. Each frozen service resolves the hotel through the shared
``HotelScopeResolver`` **first**, before any prediction is read, and returns the hotel's own 404
for an unknown identifier and for a non-member alike. Validation here happens before delegation
and reads nothing, so it cannot become an oracle: a malformed window is refused identically for a
hotel that exists and one that does not, and neither answer required a database read.

The accuracy route additionally declares ``require_role(MANAGER)`` at the router, which is where
this codebase keeps the question of which role a given operation needs.

## No clock

``as_of_date`` is a required parameter, as it is on the service beneath. Nothing here calls
``now``, ``today`` or ``utcnow``. The same request over unchanged rows returns the same body
whenever it is made.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.core.errors import ValidationError
from app.schemas.ml_accuracy import (
    AccuracyEvaluation,
    ModelVersionAccuracy,
    SegmentAccuracy,
)
from app.schemas.ml_drift import (
    DistributionObservation,
    FieldDifference,
    FieldSummary,
    ModelVersionDifference,
    ModelVersionSummary,
    SegmentDifference,
    SegmentSummary,
    WindowSummary,
)
from app.schemas.ml_performance import (
    MEASUREMENT_STATEMENT,
    AccuracyMetrics,
    DistributionComparisonResponse,
    FieldDifferenceResponse,
    FieldSummaryResponse,
    ForecastAccuracyResponse,
    MeasurementMetadata,
    ModelVersionAccuracyResponse,
    ModelVersionDifferenceResponse,
    ModelVersionSummaryResponse,
    PredictionDistributionResponse,
    QuantileValue,
    SegmentAccuracyResponse,
    SegmentDifferenceResponse,
    SegmentSummaryResponse,
    WindowSummaryResponse,
)
from app.services.ml_accuracy import DemandAccuracyService
from app.services.ml_drift import DemandDistributionService

#: The longest window either endpoint will measure, inclusive of both ends.
#:
#: 366 days, so that a full calendar year including a leap one is a single request and a
#: year-over-year baseline is expressible, and nothing longer is. This is a bound on the HTTP
#: surface, not a protocol value: neither frozen protocol gained a field, and a programmatic
#: caller of ``DemandAccuracyService`` is as unbounded as it was before this stage.
MAX_WINDOW_DAYS = 366


class ForecastPerformanceService:
    """Validate, delegate, project. One hotel per call, and no third responsibility."""

    def __init__(
        self,
        accuracy: DemandAccuracyService,
        distribution: DemandDistributionService,
    ) -> None:
        self._accuracy = accuracy
        self._distribution = distribution

    # --- accuracy ------------------------------------------------------------------------------

    def forecast_accuracy(
        self,
        hotel_public_id: uuid.UUID,
        *,
        as_of_date: dt.date,
        window_from: dt.date,
        window_to: dt.date,
    ) -> ForecastAccuracyResponse:
        """Measure this hotel's served predictions, then publish the publishable part."""
        self._require_window(window_from, window_to, label="window")
        evaluation = self._accuracy.evaluate(
            hotel_public_id,
            as_of_date=as_of_date,
            window_from=window_from,
            window_to=window_to,
        )
        return self._accuracy_response(evaluation)

    def _accuracy_response(self, evaluation: AccuracyEvaluation) -> ForecastAccuracyResponse:
        """Field for field. ``scored_feature_digests`` is dropped; nothing is recomputed."""
        return ForecastAccuracyResponse(
            hotel_public_id=evaluation.hotel_public_id,
            as_of_date=evaluation.as_of_date,
            window_from=evaluation.window_from,
            window_to=evaluation.window_to,
            scored_from=evaluation.scored_from,
            scored_to=evaluation.scored_to,
            settlement_lag_days=evaluation.settlement_lag_days,
            candidates=evaluation.candidates,
            ineligible_by_settlement=evaluation.ineligible_by_settlement,
            out_of_scope_model_digest=evaluation.out_of_scope_model_digest,
            unsettled_allocations=evaluation.unsettled_allocations,
            settled=evaluation.settled,
            by_model_version=[
                self._accuracy_version(entry) for entry in evaluation.by_model_version
            ],
            measurement=MeasurementMetadata(
                protocol_version=evaluation.protocol_version,
                protocol_checksum=evaluation.protocol_checksum,
                establishes_production_accuracy=evaluation.establishes_production_accuracy,
                statement=MEASUREMENT_STATEMENT,
            ),
        )

    def _accuracy_version(self, entry: ModelVersionAccuracy) -> ModelVersionAccuracyResponse:
        """``canonical_model_digest`` stops here. See the schema module for why."""
        return ModelVersionAccuracyResponse(
            model_version=entry.model_version,
            below_calibration=self._accuracy_segment(entry.below_calibration),
            within_calibration=self._accuracy_segment(entry.within_calibration),
        )

    @staticmethod
    def _accuracy_segment(segment: SegmentAccuracy) -> SegmentAccuracyResponse:
        """The metric set as computed offline, read through the structural protocol it declares.

        The five values are read off ``ml.metrics.MetricSet`` and passed through untouched. This
        module never names ``ml.metrics`` -- ``app.ml.accuracy`` is the one bridge to it, and the
        shape is declared as ``MetricSetLike`` so a second import is not needed to read it.
        """
        metrics = segment.metrics
        return SegmentAccuracyResponse(
            segment=segment.segment,
            metrics=AccuracyMetrics(
                observations=metrics.observations,
                skipped=metrics.skipped,
                mae=metrics.mae,
                rmse=metrics.rmse,
                smape=metrics.smape,
            ),
        )

    # --- distribution --------------------------------------------------------------------------

    def prediction_distribution(
        self,
        hotel_public_id: uuid.UUID,
        *,
        window_from: dt.date,
        window_to: dt.date,
        baseline_from: dt.date | None = None,
        baseline_to: dt.date | None = None,
    ) -> PredictionDistributionResponse:
        """Summarise this hotel's stored predictions, optionally against a baseline window.

        The baseline is a pair or nothing. The service beneath takes a tuple precisely so there
        is no half-specified state for it to validate; the two optional query parameters HTTP
        forces on us are folded back into that tuple here, and a half-supplied pair is refused
        rather than silently treated as absent.
        """
        self._require_window(window_from, window_to, label="window")
        baseline = self._require_baseline(baseline_from, baseline_to)

        observation = self._distribution.observe(
            hotel_public_id,
            window_from=window_from,
            window_to=window_to,
            baseline=baseline,
        )
        return self._distribution_response(observation)

    def _distribution_response(
        self, observation: DistributionObservation
    ) -> PredictionDistributionResponse:
        comparison = observation.comparison
        return PredictionDistributionResponse(
            hotel_public_id=observation.hotel_public_id,
            observed=self._window(observation.observed),
            comparison=(
                None
                if comparison is None
                else DistributionComparisonResponse(
                    baseline=self._window(comparison.baseline),
                    by_model_version=[
                        self._version_difference(entry) for entry in comparison.by_model_version
                    ],
                )
            ),
            measurement=MeasurementMetadata(
                protocol_version=observation.protocol_version,
                protocol_checksum=observation.protocol_checksum,
                establishes_production_accuracy=observation.establishes_production_accuracy,
                statement=MEASUREMENT_STATEMENT,
            ),
        )

    def _window(self, window: WindowSummary) -> WindowSummaryResponse:
        return WindowSummaryResponse(
            window_from=window.window_from,
            window_to=window.window_to,
            candidates=window.candidates,
            out_of_scope_model_digest=window.out_of_scope_model_digest,
            by_model_version=[self._version_summary(entry) for entry in window.by_model_version],
        )

    def _version_summary(self, entry: ModelVersionSummary) -> ModelVersionSummaryResponse:
        """``canonical_model_digest`` stops here, as it does on the accuracy side."""
        return ModelVersionSummaryResponse(
            model_version=entry.model_version,
            below_calibration=self._segment_summary(entry.below_calibration),
            within_calibration=self._segment_summary(entry.within_calibration),
        )

    def _segment_summary(self, segment: SegmentSummary) -> SegmentSummaryResponse:
        """``feature_digests`` stops here."""
        return SegmentSummaryResponse(
            segment=segment.segment,
            observations=segment.observations,
            fields=[self._field_summary(field) for field in segment.fields],
        )

    def _field_summary(self, field: FieldSummary) -> FieldSummaryResponse:
        return FieldSummaryResponse(
            field=field.field,
            count=field.count,
            minimum=field.minimum,
            maximum=field.maximum,
            mean=field.mean,
            median=field.median,
            quantiles=self._quantiles(field.quantiles),
        )

    def _version_difference(self, entry: ModelVersionDifference) -> ModelVersionDifferenceResponse:
        return ModelVersionDifferenceResponse(
            model_version=entry.model_version,
            below_calibration=self._segment_difference(entry.below_calibration),
            within_calibration=self._segment_difference(entry.within_calibration),
        )

    def _segment_difference(self, segment: SegmentDifference) -> SegmentDifferenceResponse:
        return SegmentDifferenceResponse(
            segment=segment.segment,
            observations=segment.observations,
            fields=[self._field_difference(field) for field in segment.fields],
        )

    def _field_difference(self, field: FieldDifference) -> FieldDifferenceResponse:
        return FieldDifferenceResponse(
            field=field.field,
            count=field.count,
            minimum=field.minimum,
            maximum=field.maximum,
            mean=field.mean,
            median=field.median,
            quantiles=self._quantiles(field.quantiles),
        )

    @staticmethod
    def _quantiles(quantiles: tuple[tuple[str, float | None], ...]) -> list[QuantileValue]:
        """Pairs to named objects, in the protocol's own order. Nothing is sorted or filled."""
        return [QuantileValue(label=label, value=value) for label, value in quantiles]

    # --- request rules -------------------------------------------------------------------------

    @staticmethod
    def _require_window(window_from: dt.date, window_to: dt.date, *, label: str) -> None:
        """A window that ends before it starts is malformed, not empty.

        Both frozen services would return an empty result for a reversed window, which reads as
        "nothing happened" rather than "you asked wrongly". An empty window that is correctly
        ordered is not an error and is not refused here.
        """
        if window_from > window_to:
            raise ValidationError(f"{label}_from must not be later than {label}_to.")
        span = (window_to - window_from).days + 1
        if span > MAX_WINDOW_DAYS:
            raise ValidationError(
                f"The {label} must not exceed {MAX_WINDOW_DAYS} days; this one is {span}."
            )

    def _require_baseline(
        self, baseline_from: dt.date | None, baseline_to: dt.date | None
    ) -> tuple[dt.date, dt.date] | None:
        """Both ends or neither, and the pair is validated exactly as the target window is."""
        if baseline_from is None and baseline_to is None:
            return None
        if baseline_from is None or baseline_to is None:
            raise ValidationError(
                "baseline_from and baseline_to must be supplied together or not at all."
            )
        self._require_window(baseline_from, baseline_to, label="baseline")
        return baseline_from, baseline_to


__all__ = ["MAX_WINDOW_DAYS", "ForecastPerformanceService"]
