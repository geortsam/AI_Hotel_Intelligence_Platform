"""Observing the distribution of a hotel's stored predictions. Reads only, writes nothing.

Stage 6.10. Stage 6.8 made every served prediction durable and Stage 6.9 measured how far off it
was. This answers a different question -- *what did the inputs and the outputs look like over
this window, and how far did they move from that one* -- and answers nothing else.

    HotelScopeResolver       who may reach this hotel, and which row it is   (app.services.scope)
        |
    MlPredictionRepository   one prediction per group, already selected      (Stage 6.9's read)
        |
    app.ml.drift             pure summaries and differences                  (no SQL, no clock)
        |
    DistributionObservation

## Observation, not detection

**This service detects nothing.** No threshold is evaluated, no verdict is produced, no alert is
emitted, nothing is ranked, and no field in what it returns is named for drift or an anomaly. It
reports what the distributions were and how far they moved. Deciding whether that movement means
something is a later stage's job, and Stage 6.8 §9 already wrote down why it is not this one's:
choosing a statistic before there is data to look at would be guessing.

It also establishes no production accuracy. Stage 6.9 measures error under its own protocol and
makes no accuracy claim either; this stage does not even measure error.

## Read-only, and structurally so

It holds **no session**. It cannot commit, roll back or flush because it has nothing to do those
things to -- the repository holds the session for its query, and that query is a SELECT. It
builds no SQLAlchemy query either. The same arrangement as ``DemandAccuracyService``, and a
deliberate contrast with ``DemandPredictionService``, which owns a unit of work.

## The reference window

The baseline is an explicitly supplied second window of stored predictions, never the training
distribution -- the reason is in ``app.ml.drift_protocol``. Without a baseline the result carries
``comparison = None``, which is different from a comparison whose every difference is zero.

## Tenant isolation

The hotel is resolved FIRST, through the shared resolver, before any prediction is read. A
non-member gets the hotel's own 404 and no observation happens, so they cannot learn whether a
hotel has predictions, or history, or exists. Both window reads are then bounded by the internal
``hotel_id`` the resolver returned. One hotel per call: no platform-wide method, no multi-hotel
parameter, and no result in which two hotels could share a summary.

## No clock

Every window boundary is a parameter. Nothing on this path calls ``now``, ``today`` or
``utcnow``, and nothing reads ``func.now()`` -- so the same rows and the same windows give the
same summaries whenever the observation is run.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections.abc import Sequence

from app.ml.drift import (
    ObservedPrediction,
    by_segment,
    difference,
    partition,
    summarise,
    value_of,
)
from app.ml.drift_protocol import PROTOCOL, DistributionProtocol
from app.ml.serving import APPROVED_MODEL
from app.repositories.ml_prediction import MlPredictionRepository
from app.schemas.ml_drift import (
    DistributionComparison,
    DistributionObservation,
    FieldDifference,
    FieldSummary,
    ModelVersionDifference,
    ModelVersionSummary,
    SegmentDifference,
    SegmentSummary,
    WindowSummary,
)
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

#: The outcomes an observation run can report. Two, because a run either summarised something or
#: found nothing in the window -- and the second is a normal answer for a young deployment.
OBSERVED = "observed"
NOTHING_TO_OBSERVE = "nothing_to_observe"


class DemandDistributionService:
    """One hotel, one window, optionally one baseline window. Deliberately nothing else.

    No batch method and no multi-hotel signature, for the reason every ML repository in this
    codebase takes one hotel at a time: "every hotel's distributions in one result" is exactly
    the query this domain must not own.
    """

    def __init__(
        self,
        predictions: MlPredictionRepository,
        scope: HotelScopeResolver,
        *,
        protocol: DistributionProtocol = PROTOCOL,
    ) -> None:
        self._predictions = predictions
        self._scope = scope
        self._protocol = protocol

    def observe(
        self,
        hotel_public_id: uuid.UUID,
        *,
        window_from: dt.date,
        window_to: dt.date,
        baseline: tuple[dt.date, dt.date] | None = None,
    ) -> DistributionObservation:
        """Summarise this hotel's stored predictions over the window, and optionally against one.

        *baseline* is a pair or nothing, so there is no half-specified state to validate: a
        caller cannot supply one end of a second window and leave the other to a default.
        """
        # Authorization and tenancy first, always. Nothing below runs for a caller who is not a
        # member, so a non-member leaves no trace and learns nothing.
        hotel = self._scope.require_hotel(hotel_public_id)

        started = time.perf_counter()
        observed = self._window(hotel.id, window_from, window_to)

        comparison: DistributionComparison | None = None
        if baseline is not None:
            baseline_summary = self._window(hotel.id, baseline[0], baseline[1])
            comparison = DistributionComparison(
                baseline=baseline_summary,
                by_model_version=self._differences(observed, baseline_summary),
            )

        result = DistributionObservation(
            hotel_public_id=hotel.public_id,
            protocol_version=self._protocol.version,
            protocol_checksum=self._protocol.checksum,
            observed=observed,
            comparison=comparison,
        )
        self._observe(result, time.perf_counter() - started)
        return result

    # --- one window ----------------------------------------------------------------------------

    def _window(self, hotel_id: int, window_from: dt.date, window_to: dt.date) -> WindowSummary:
        """Read one window and summarise it, grouped per model version.

        The read is Stage 6.9's, unchanged: one prediction per ``(target_date, horizon,
        model_version)``, earliest ``generated_at``, lowest ``id`` breaking a tie. Inheriting it
        rather than adding a second read keeps one selection rule in this codebase.
        """
        candidates = [
            ObservedPrediction(
                target_date=row.target_date,
                model_version=row.model_version,
                canonical_model_digest=row.canonical_model_digest,
                feature_digest=row.feature_digest,
                feature_values=row.feature_values,
                predicted_room_nights=row.predicted_room_nights,
            )
            for row in self._predictions.scorable_predictions(hotel_id, window_from, window_to)
        ]
        split = partition(candidates, approved_digest=APPROVED_MODEL.canonical_model_digest)

        versions = sorted({row.model_version for row in split.in_scope})
        entries: list[ModelVersionSummary] = []
        for version in versions:
            rows = tuple(row for row in split.in_scope if row.model_version == version)
            segments = by_segment(rows, protocol=self._protocol)
            entries.append(
                ModelVersionSummary(
                    model_version=version,
                    # Every in-scope row shares the approved identity by construction: any other
                    # digest was partitioned out above.
                    canonical_model_digest=rows[0].canonical_model_digest,
                    below_calibration=self._segment(
                        self._protocol.segment_below_calibration,
                        segments[self._protocol.segment_below_calibration],
                    ),
                    within_calibration=self._segment(
                        self._protocol.segment_within_calibration,
                        segments[self._protocol.segment_within_calibration],
                    ),
                )
            )

        return WindowSummary(
            window_from=window_from,
            window_to=window_to,
            candidates=len(candidates),
            out_of_scope_model_digest=split.out_of_scope_model_digest,
            by_model_version=tuple(entries),
        )

    def _segment(self, segment: str, rows: Sequence[ObservedPrediction]) -> SegmentSummary:
        """Every observed field summarised over one segment, in the protocol's column order."""
        fields = []
        for field in self._protocol.observed_fields:
            values = [
                value for value in (value_of(row, field) for row in rows) if value is not None
            ]
            summary = summarise(values, protocol=self._protocol)
            fields.append(
                FieldSummary(
                    field=field,
                    count=summary["count"],
                    minimum=summary["minimum"],
                    maximum=summary["maximum"],
                    mean=summary["mean"],
                    median=summary["median"],
                    quantiles=summary["quantiles"],
                )
            )
        return SegmentSummary(
            segment=segment,
            observations=len(rows),
            feature_digests=tuple(row.feature_digest for row in rows),
            fields=tuple(fields),
        )

    # --- movement between two windows -----------------------------------------------------------

    def _differences(
        self, observed: WindowSummary, baseline: WindowSummary
    ) -> tuple[ModelVersionDifference, ...]:
        """Target minus baseline, for every model version present in either window.

        A version present in only one window still appears, with its missing side unmeasured and
        therefore every difference ``None``. Dropping it would hide the fact that a model version
        started or stopped serving between the two windows -- which is movement, and exactly the
        kind this stage exists to make visible.
        """
        indexed_target = {entry.model_version: entry for entry in observed.by_model_version}
        indexed_baseline = {entry.model_version: entry for entry in baseline.by_model_version}

        entries: list[ModelVersionDifference] = []
        for version in sorted(set(indexed_target) | set(indexed_baseline)):
            target = indexed_target.get(version)
            reference = indexed_baseline.get(version)
            entries.append(
                ModelVersionDifference(
                    model_version=version,
                    below_calibration=self._segment_difference(
                        self._protocol.segment_below_calibration,
                        None if target is None else target.below_calibration,
                        None if reference is None else reference.below_calibration,
                    ),
                    within_calibration=self._segment_difference(
                        self._protocol.segment_within_calibration,
                        None if target is None else target.within_calibration,
                        None if reference is None else reference.within_calibration,
                    ),
                )
            )
        return tuple(entries)

    def _segment_difference(
        self, segment: str, target: SegmentSummary | None, baseline: SegmentSummary | None
    ) -> SegmentDifference:
        target_fields = {} if target is None else {f.field: f for f in target.fields}
        baseline_fields = {} if baseline is None else {f.field: f for f in baseline.fields}

        fields = []
        for name in self._protocol.observed_fields:
            left = target_fields.get(name)
            right = baseline_fields.get(name)
            fields.append(
                FieldDifference(
                    field=name,
                    count=(0 if left is None else left.count)
                    - (0 if right is None else right.count),
                    minimum=difference(
                        None if left is None else left.minimum,
                        None if right is None else right.minimum,
                    ),
                    maximum=difference(
                        None if left is None else left.maximum,
                        None if right is None else right.maximum,
                    ),
                    mean=difference(
                        None if left is None else left.mean, None if right is None else right.mean
                    ),
                    median=difference(
                        None if left is None else left.median,
                        None if right is None else right.median,
                    ),
                    quantiles=self._quantile_differences(left, right),
                )
            )
        return SegmentDifference(
            segment=segment,
            observations=(0 if target is None else target.observations)
            - (0 if baseline is None else baseline.observations),
            fields=tuple(fields),
        )

    def _quantile_differences(
        self, target: FieldSummary | None, baseline: FieldSummary | None
    ) -> tuple[tuple[str, float | None], ...]:
        left = {} if target is None else dict(target.quantiles)
        right = {} if baseline is None else dict(baseline.quantiles)
        return tuple(
            (label, difference(left.get(label), right.get(label)))
            for label in self._protocol.quantile_labels
        )

    # --- the event ------------------------------------------------------------------------------

    def _observe(self, result: DistributionObservation, seconds: float) -> None:
        """One event per observation run. Five fields, and nothing a hotel owns.

        No summary value, no feature value, no prediction, no hotel identifier, no digest of any
        kind, no path. Those are the hotel's business data and the server's internals; the
        returned object holds the first and nobody needs the second.

        ``model_version`` is the approved model's label -- a value already published in the model
        card -- and never a digest. The request id is not passed: ``RequestIdFilter`` attaches it
        already.
        """
        summarised = self._summarised(result.observed)
        windows = 1
        if result.comparison is not None:
            summarised += self._summarised(result.comparison.baseline)
            windows = 2

        outcome = OBSERVED if summarised else NOTHING_TO_OBSERVE
        logger.info(
            "demand distribution %s (model=%s) over %s prediction(s) in %.1f ms",
            outcome,
            APPROVED_MODEL.model_version,
            summarised,
            seconds * 1000,
            extra={
                "outcome": outcome,
                "model_version": APPROVED_MODEL.model_version,
                "predictions_summarised": summarised,
                "windows_compared": windows,
                "duration_ms": round(seconds * 1000, 3),
            },
        )

    @staticmethod
    def _summarised(window: WindowSummary) -> int:
        """In-scope predictions the window contributed, across both segments."""
        return sum(
            entry.below_calibration.observations + entry.within_calibration.observations
            for entry in window.by_model_version
        )


__all__ = ["NOTHING_TO_OBSERVE", "OBSERVED", "DemandDistributionService"]
