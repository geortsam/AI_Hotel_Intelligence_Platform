"""What an accuracy evaluation returns. Not an HTTP contract, and deliberately not one.

Stage 6.9 adds **no endpoint**. These are frozen dataclasses rather than Pydantic models, which
is the difference that matters: every other module in this package defines a request or response
body that FastAPI registers, and a Pydantic model here would suggest this one does too. Nothing
in this file is registered on a route, appears in the OpenAPI document or crosses the network.
The API stays at 51 paths / 83 operations.

## What travels, and what does not

**Aggregates and attribution, never the per-observation dataset.** Each segment carries its
metric set -- with its own denominator -- and the ``feature_digest`` of every prediction that
was scored into it, so any number here can be traced back to the exact rows that produced it.
The predicted and realised values themselves are not returned: the question this stage answers
is "how far off were we, under these rules", and shipping the paired series would make it an
export instead.

**No internal identifier.** The hotel is named by its public UUID, the same one the caller
supplied. There is no field a ``BIGINT`` key could travel in.

## The claims boundary is in the payload, not only in the prose

``protocol_version``, ``protocol_checksum`` and ``as_of_date`` are on every result, because a
metric without the rules that produced it is a number looking for a caption.
:attr:`AccuracyEvaluation.establishes_production_accuracy` is a constant ``False`` and exists to
be read: this stage measures under a declared protocol and certifies nothing. There is no
threshold field, no baseline field and no ranking, here or in the protocol.

``settled`` is the honest qualifier. False means at least one allocation covering the scored
window could still change status, so the measurement is provisional.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Protocol


class MetricSetLike(Protocol):
    """The shape of ``ml.metrics.MetricSet``, declared structurally rather than imported.

    ``app.ml.accuracy`` is the **only** module in ``backend/`` permitted to name ``ml.metrics``,
    and that includes a ``TYPE_CHECKING`` import: a type-only import is still this file naming
    that module, and the whole value of "one bridge" is that grepping for it finds one place.

    So the shape is declared here instead. This is not a second definition of anything that
    matters -- there is no formula here, no denominator rule and no edge case, only the five
    names MAE, RMSE and sMAPE arrive under. Those still have exactly one definition, in
    ``ml/metrics.py``, and this stage computes them by calling it.
    """

    observations: int
    skipped: int
    mae: float | None
    rmse: float | None
    smape: float | None

    def as_dict(self, *, digits: int = ...) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class SegmentAccuracy:
    """One segment's error, its denominator, and which predictions went into it."""

    #: ``below_calibration`` or ``within_calibration``, as the protocol names them.
    segment: str
    #: Straight from ``ml.metrics.metric_set``: observations, skipped, mae, rmse, smape.
    metrics: MetricSetLike
    #: The ``feature_digest`` of every scored prediction, in target-date order. Attribution:
    #: each of these identifies one row of ``demand_predictions`` exactly.
    scored_feature_digests: tuple[str, ...]

    @property
    def observations(self) -> int:
        """The denominator, surfaced so a reader never has to reach through ``metrics``."""
        return self.metrics.observations

    @property
    def skipped(self) -> int:
        return self.metrics.skipped

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "metrics": self.metrics.as_dict(),
            "scored_feature_digests": list(self.scored_feature_digests),
        }


@dataclass(frozen=True, slots=True)
class ModelVersionAccuracy:
    """One model version's error over one hotel, split into the two declared segments.

    The two segments are separate fields rather than entries in a mapping, so there is no
    shape in which they could be summed by accident, and no combined headline metric exists
    anywhere on this object.
    """

    model_version: str
    #: The identity that produced these numbers, carried so a result can be attributed to an
    #: artifact rather than to "the model".
    canonical_model_digest: str
    below_calibration: SegmentAccuracy
    within_calibration: SegmentAccuracy

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "canonical_model_digest": self.canonical_model_digest,
            "below_calibration": self.below_calibration.as_dict(),
            "within_calibration": self.within_calibration.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class AccuracyEvaluation:
    """One hotel, one window, one as-of date, under one declared protocol."""

    hotel_public_id: uuid.UUID
    as_of_date: dt.date
    window_from: dt.date
    window_to: dt.date
    #: The range actually scored: ``window_to`` clamped to the last date that cleared the lag.
    scored_from: dt.date | None
    scored_to: dt.date | None

    protocol_version: str
    protocol_checksum: str
    settlement_lag_days: int

    #: Candidate predictions the window held, after one-per-group selection.
    candidates: int
    #: Candidates whose target date had not cleared the settlement lag.
    ineligible_by_settlement: int
    #: Eligible candidates produced by something other than the approved model identity.
    #: Reported here and pooled into nothing.
    out_of_scope_model_digest: int

    #: Allocations covering the scored window that can still change occupancy. See
    #: ``app.ml.accuracy_protocol`` for why this exists.
    unsettled_allocations: int
    settled: bool

    by_model_version: tuple[ModelVersionAccuracy, ...]

    #: Constant. Stage 6.9 measures under a declared protocol; it does not establish production
    #: accuracy, evaluate a threshold, compare against a baseline or rank anything.
    establishes_production_accuracy: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "hotel_public_id": str(self.hotel_public_id),
            "as_of_date": self.as_of_date.isoformat(),
            "window_from": self.window_from.isoformat(),
            "window_to": self.window_to.isoformat(),
            "scored_from": None if self.scored_from is None else self.scored_from.isoformat(),
            "scored_to": None if self.scored_to is None else self.scored_to.isoformat(),
            "protocol_version": self.protocol_version,
            "protocol_checksum": self.protocol_checksum,
            "settlement_lag_days": self.settlement_lag_days,
            "candidates": self.candidates,
            "ineligible_by_settlement": self.ineligible_by_settlement,
            "out_of_scope_model_digest": self.out_of_scope_model_digest,
            "unsettled_allocations": self.unsettled_allocations,
            "settled": self.settled,
            "by_model_version": [entry.as_dict() for entry in self.by_model_version],
            "establishes_production_accuracy": self.establishes_production_accuracy,
        }


__all__ = ["AccuracyEvaluation", "MetricSetLike", "ModelVersionAccuracy", "SegmentAccuracy"]
