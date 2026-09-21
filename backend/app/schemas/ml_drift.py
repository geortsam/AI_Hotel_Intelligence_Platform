"""What a distribution observation returns. Not an HTTP contract, and deliberately not one.

Stage 6.10 adds **no endpoint**. These are frozen dataclasses rather than Pydantic models, for
the reason Stage 6.9's result types are: every other module in this package defines a body that
FastAPI registers, and a Pydantic model here would suggest this one does too. Nothing in this
file is registered on a route, appears in the OpenAPI document or crosses the network. The API
stays at 51 paths / 83 operations.

## No field here reaches a conclusion

There is no ``drift``, ``drifted``, ``drift_detected``, ``anomaly``, ``threshold``, ``alert``,
``passed`` or ``severity`` field anywhere below, and a test walks the whole result tree asserting
it. This stage reports what the numbers were and how far they moved; deciding whether that
movement means anything is not its job and is not in its vocabulary.

## What travels

Summaries and attribution, never the raw series. Each segment carries its own denominator and
the ``feature_digest`` of every prediction summarised into it, so any number here traces back to
exact rows; each model-version group carries its ``canonical_model_digest``. The per-observation
values are not returned -- shipping them would make this an export rather than an observation.

**No internal identifier.** The hotel is named by the public UUID the caller supplied. There is
no field a ``BIGINT`` key could travel in.

``protocol_version`` and ``protocol_checksum`` ride on every result, because a summary without
the conventions that produced it is a number looking for a caption.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FieldSummary:
    """One observed variable's distribution over one set of predictions.

    Every statistic is ``None`` when ``count`` is zero -- never ``0.0``. Zero and unmeasured are
    different claims.
    """

    field: str
    count: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    median: float | None
    #: ``(label, value)`` in the protocol's declared order, values ``None`` when unmeasured.
    quantiles: tuple[tuple[str, float | None], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "count": self.count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
            "median": self.median,
            "quantiles": [[label, value] for label, value in self.quantiles],
        }


@dataclass(frozen=True, slots=True)
class SegmentSummary:
    """One segment's distributions, its denominator, and which predictions went into it."""

    segment: str
    observations: int
    #: The ``feature_digest`` of every prediction summarised here, in target-date order.
    feature_digests: tuple[str, ...]
    #: One entry per observed field, in the protocol's declared order.
    fields: tuple[FieldSummary, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "observations": self.observations,
            "feature_digests": list(self.feature_digests),
            "fields": [field.as_dict() for field in self.fields],
        }


@dataclass(frozen=True, slots=True)
class ModelVersionSummary:
    """One model version's distributions over one hotel, split into the two declared segments.

    The segments are separate fields rather than entries in a mapping, so there is no shape in
    which they could be summed by accident, and no combined figure exists on this object.
    """

    model_version: str
    canonical_model_digest: str
    below_calibration: SegmentSummary
    within_calibration: SegmentSummary

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "canonical_model_digest": self.canonical_model_digest,
            "below_calibration": self.below_calibration.as_dict(),
            "within_calibration": self.within_calibration.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class WindowSummary:
    """Everything observed in one window, for one hotel."""

    window_from: dt.date
    window_to: dt.date
    #: Predictions the window held, after the inherited Stage 6.9 one-per-group selection.
    candidates: int
    #: Candidates produced by something other than the approved model identity. Summarised
    #: into nothing.
    out_of_scope_model_digest: int
    by_model_version: tuple[ModelVersionSummary, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_from": self.window_from.isoformat(),
            "window_to": self.window_to.isoformat(),
            "candidates": self.candidates,
            "out_of_scope_model_digest": self.out_of_scope_model_digest,
            "by_model_version": [entry.as_dict() for entry in self.by_model_version],
        }


@dataclass(frozen=True, slots=True)
class FieldDifference:
    """Target minus baseline, per statistic. ``None`` wherever either side was unmeasured."""

    field: str
    count: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    median: float | None
    quantiles: tuple[tuple[str, float | None], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "count": self.count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
            "median": self.median,
            "quantiles": [[label, value] for label, value in self.quantiles],
        }


@dataclass(frozen=True, slots=True)
class SegmentDifference:
    """One segment's movement between the two windows."""

    segment: str
    observations: int
    fields: tuple[FieldDifference, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "observations": self.observations,
            "fields": [field.as_dict() for field in self.fields],
        }


@dataclass(frozen=True, slots=True)
class ModelVersionDifference:
    """One model version's movement, in the same two segments and never pooled across them."""

    model_version: str
    below_calibration: SegmentDifference
    within_calibration: SegmentDifference

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "below_calibration": self.below_calibration.as_dict(),
            "within_calibration": self.within_calibration.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class DistributionComparison:
    """The baseline window and the movement from it, present only when a baseline was supplied."""

    baseline: WindowSummary
    by_model_version: tuple[ModelVersionDifference, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.as_dict(),
            "by_model_version": [entry.as_dict() for entry in self.by_model_version],
        }


@dataclass(frozen=True, slots=True)
class DistributionObservation:
    """One hotel, one window, optionally against one baseline window, under one protocol."""

    hotel_public_id: uuid.UUID
    protocol_version: str
    protocol_checksum: str
    observed: WindowSummary
    #: ``None`` when no baseline window was supplied. Never a fabricated zero comparison.
    comparison: DistributionComparison | None

    #: Constant. Stage 6.10 observes distributions; it detects nothing, evaluates no threshold
    #: and establishes no production accuracy.
    establishes_production_accuracy: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "hotel_public_id": str(self.hotel_public_id),
            "protocol_version": self.protocol_version,
            "protocol_checksum": self.protocol_checksum,
            "observed": self.observed.as_dict(),
            "comparison": None if self.comparison is None else self.comparison.as_dict(),
            "establishes_production_accuracy": self.establishes_production_accuracy,
        }


__all__ = [
    "DistributionComparison",
    "DistributionObservation",
    "FieldDifference",
    "FieldSummary",
    "ModelVersionDifference",
    "ModelVersionSummary",
    "SegmentDifference",
    "SegmentSummary",
    "WindowSummary",
]
