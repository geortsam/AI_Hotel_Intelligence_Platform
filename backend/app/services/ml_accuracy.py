"""Measuring served predictions against what actually happened. Reads only, writes nothing.

Stage 6.9. Stage 6.8 made every served prediction durable; this asks the second of the three
questions that stage named -- *how far off were we* -- and answers it under a protocol declared
before any number was computed.

    HotelScopeResolver       who may reach this hotel, and which row it is   (app.services.scope)
        |
    MlPredictionRepository   one prediction per group, already selected      (one query)
        |
    MlDemandRepository       realised room nights, the EXISTING definition   (demand_by_date)
        |
    MlPredictionRepository   allocations that can still change               (one query)
        |
    app.ml.accuracy          pure pairing, segmentation, metrics             (no SQL, no clock)
        |
    AccuracyEvaluation

## Read-only, and structurally so

This service holds **no session**. It cannot commit, roll back or flush, because it has nothing
to do those things to -- the two repositories hold the session for their queries, and neither
writes. That is a deliberate contrast with ``DemandPredictionService``, which took a session in
Stage 6.8 precisely because it owns a unit of work. This one owns none.

It builds no SQLAlchemy query either. Every ``select`` on this path lives in a repository.

## Tenant isolation

The hotel is resolved FIRST, through the shared resolver, before a prediction or a booking row
is read. A caller who is not a member gets the hotel's own 404 -- byte-identical to the one an
unknown identifier produces -- and no evaluation happens, so they cannot learn whether a hotel
has predictions, or history, or exists.

Both reads are then bounded by the internal ``hotel_id`` the resolver returned. One hotel per
call: there is no parameter here through which a second could be named, no platform-wide
method, and no result in which two hotels could share a metric.

## No clock

``as_of_date`` is a required keyword argument. Nothing on this path calls ``now``, ``today`` or
``utcnow``, and nothing reads ``func.now()``. Two evaluations with the same as-of date over the
same data return the same answer whenever they are run, which is what makes a measurement
repeatable rather than a snapshot of when someone happened to ask.

## What this service does not do

No threshold is evaluated, no baseline is computed, no model version is ranked against another,
and nothing here decides that a model should be retrained, promoted or replaced. The result
carries ``establishes_production_accuracy = False`` because that is the literal truth: these
are measurements under ``PROTOCOL``, and the model card's "Production accuracy established:
No" is unchanged by them.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

from app.ml.accuracy import (
    ScoredCandidate,
    SegmentScoring,
    metrics_for,
    partition,
    score_segments,
    scored_window,
)
from app.ml.accuracy_protocol import PROTOCOL, AccuracyProtocol
from app.ml.serving import APPROVED_MODEL
from app.repositories.ml_demand import MlDemandRepository
from app.repositories.ml_prediction import MlPredictionRepository
from app.schemas.ml_accuracy import (
    AccuracyEvaluation,
    ModelVersionAccuracy,
    SegmentAccuracy,
)
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

#: The outcomes an evaluation run can report. Two, because an evaluation either produced a
#: measurement or found nothing in the window that had settled yet -- and the second is a normal
#: answer for a young deployment, not a failure.
EVALUATED = "evaluated"
NOTHING_TO_SCORE = "nothing_to_score"


class DemandAccuracyService:
    """One hotel, one window, one as-of date. Deliberately nothing else.

    No batch method and no multi-hotel signature, for the reason every ML repository in this
    codebase takes one hotel at a time: "every hotel's accuracy in one result" is exactly the
    query this domain must not own, and a cross-tenant aggregate is one edit away from it.
    """

    def __init__(
        self,
        predictions: MlPredictionRepository,
        demand: MlDemandRepository,
        scope: HotelScopeResolver,
        *,
        protocol: AccuracyProtocol = PROTOCOL,
    ) -> None:
        self._predictions = predictions
        self._demand = demand
        self._scope = scope
        self._protocol = protocol

    def evaluate(
        self,
        hotel_public_id: uuid.UUID,
        *,
        as_of_date: dt.date,
        window_from: dt.date,
        window_to: dt.date,
    ) -> AccuracyEvaluation:
        """Measure this hotel's served predictions for target dates in the window.

        *as_of_date* is required and is the only notion of "when" on this path. Predictions
        whose target date has not cleared the settlement lag by then are counted as ineligible
        rather than scored, and the count is returned so the caller can see what was left out.
        """
        # Authorization and tenancy first, always. Nothing below this line runs for a caller
        # who is not a member, so a non-member leaves no trace and learns nothing.
        hotel = self._scope.require_hotel(hotel_public_id)

        started = time.perf_counter()
        scored_range = scored_window(window_from, window_to, as_of_date, protocol=self._protocol)

        candidates = self._predictions.scorable_predictions(hotel.id, window_from, window_to)
        split = partition(
            candidates,
            as_of_date=as_of_date,
            approved_digest=APPROVED_MODEL.canonical_model_digest,
            protocol=self._protocol,
        )

        realised: dict[dt.date, int] = {}
        unsettled = 0
        if scored_range is not None:
            scored_from, scored_to = scored_range
            realised = self._demand.demand_by_date(hotel.id, scored_from, scored_to)
            unsettled = self._predictions.unsettled_allocation_count(
                hotel.id, scored_from, scored_to
            )

        by_model_version = self._by_model_version(split.eligible, realised)
        evaluation = AccuracyEvaluation(
            hotel_public_id=hotel.public_id,
            as_of_date=as_of_date,
            window_from=window_from,
            window_to=window_to,
            scored_from=None if scored_range is None else scored_range[0],
            scored_to=None if scored_range is None else scored_range[1],
            protocol_version=self._protocol.version,
            protocol_checksum=self._protocol.checksum,
            settlement_lag_days=self._protocol.settlement_lag_days,
            candidates=len(candidates),
            ineligible_by_settlement=split.ineligible_by_settlement,
            out_of_scope_model_digest=split.out_of_scope_model_digest,
            unsettled_allocations=unsettled,
            settled=unsettled == 0,
            by_model_version=by_model_version,
        )
        self._observe(evaluation, time.perf_counter() - started)
        return evaluation

    def _by_model_version(
        self, eligible: tuple[ScoredCandidate, ...], realised: dict[dt.date, int]
    ) -> tuple[ModelVersionAccuracy, ...]:
        """One entry per model version, each split into the two declared segments.

        Grouped rather than pooled: two model versions never share a denominator, and neither
        do the two segments. There is no combined figure computed anywhere here, which is why
        there is no field one could be read from.
        """
        versions = sorted({candidate.model_version for candidate in eligible})
        entries: list[ModelVersionAccuracy] = []
        for version in versions:
            rows = tuple(row for row in eligible if row.model_version == version)
            scoring = score_segments(rows, realised, protocol=self._protocol)
            entries.append(
                ModelVersionAccuracy(
                    model_version=version,
                    # Every eligible row shares the approved identity by construction: a row
                    # with any other digest was partitioned out as out-of-scope above.
                    canonical_model_digest=rows[0].canonical_model_digest,
                    below_calibration=self._segment(
                        scoring[self._protocol.segment_below_calibration]
                    ),
                    within_calibration=self._segment(
                        scoring[self._protocol.segment_within_calibration]
                    ),
                )
            )
        return tuple(entries)

    def _segment(self, scoring: SegmentScoring) -> SegmentAccuracy:
        return SegmentAccuracy(
            segment=scoring.segment,
            metrics=metrics_for(scoring),
            scored_feature_digests=scoring.feature_digests,
        )

    def _observe(self, evaluation: AccuracyEvaluation, seconds: float) -> None:
        """One event per evaluation run. Five fields, and nothing a hotel owns.

        No metric value, no prediction value, no feature value, no realised demand, no hotel
        identifier, no digest of any kind, no path. Those are the hotel's business data and the
        server's internals; the returned object holds the first and nobody needs the second.

        ``model_version`` is the approved model's label -- a value already published in the
        model card -- and never a digest. The request id is not passed: ``RequestIdFilter``
        attaches it already, and a second writer of one field is a field that disagrees.
        """
        scored = sum(
            entry.below_calibration.observations + entry.within_calibration.observations
            for entry in evaluation.by_model_version
        )
        skipped = sum(
            entry.below_calibration.skipped + entry.within_calibration.skipped
            for entry in evaluation.by_model_version
        )
        outcome = EVALUATED if scored else NOTHING_TO_SCORE
        logger.info(
            "demand accuracy %s (model=%s) over %s prediction(s) in %.1f ms",
            outcome,
            APPROVED_MODEL.model_version,
            scored,
            seconds * 1000,
            extra={
                "outcome": outcome,
                "model_version": APPROVED_MODEL.model_version,
                "predictions_scored": scored,
                "predictions_skipped": skipped,
                "duration_ms": round(seconds * 1000, 3),
            },
        )


__all__ = ["EVALUATED", "NOTHING_TO_SCORE", "DemandAccuracyService"]
