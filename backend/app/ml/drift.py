"""Summarising the distribution of stored predictions. Pure: no SQL, no HTTP, no clock, no model.

Stage 6.10. Takes the predictions a hotel was served over a window and returns descriptive
summaries of the nine model inputs and the one model output, segmented and grouped exactly as
Stage 6.9 segments and groups them. Given a second window, it also returns the movement between
the two.

## What this module refuses to do

**It detects nothing.** There is no threshold here, no verdict, no alert, no anomaly flag, and no
composite statistic -- no PSI, no KS, no Jensen-Shannon. It reports counts, extremes, a mean, a
median and five declared quantiles, plus differences between two windows, and stops. Whether a
difference matters is not a question this stage answers, and it is not in this stage's vocabulary.

**It recomputes nothing.** The values summarised are the ones stored in ``demand_predictions`` by
Stage 6.8 -- the exact inputs a prediction was computed from and the exact number returned. No
feature is derived here, no artifact is loaded, no estimator is called and the training dataset
is never opened. Recomputing a feature would be summarising what the model *would* be given
today rather than what it *was* given then, which is the opposite of an observation.

**It does not select.** Selection happened in the database, under Stage 6.9's rule -- one
prediction per ``(target_date, horizon, model_version)``, the earliest ``generated_at`` with the
lowest ``id`` breaking a tie. This module inherits that unchanged and checks the invariant rather
than assuming it.

**It does not consult a clock.** Every window boundary arrives as an argument.

## Arithmetic that cannot drift

``math.fsum`` rather than ``sum`` wherever floating-point values are added, so a mean does not
depend on the order rows came back in. The quantile is the protocol's one declared convention,
written out here rather than delegated, so the rule a reader checks is the rule that ran.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.ml.accuracy_protocol import segment_of
from app.ml.drift_protocol import PROTOCOL, DistributionProtocol


class ObservationError(Exception):
    """An input that breaks the protocol's own assumptions rather than a caller's mistake."""


@dataclass(frozen=True, slots=True)
class ObservedPrediction:
    """One stored prediction, reduced to what an observation reads.

    Structurally what ``app.ml.accuracy.ScoredCandidate`` already carries, named separately here
    so this module states its own input rather than importing another stage's shape and quietly
    depending on fields it does not use.
    """

    target_date: Any
    model_version: str
    canonical_model_digest: str
    feature_digest: str
    feature_values: Mapping[str, Any]
    predicted_room_nights: float


def assert_one_per_group(predictions: Sequence[ObservedPrediction]) -> None:
    """Refuse a set holding two predictions for one target date and model version.

    The query applies the inherited selection rule. If it did not, summarising both would double
    a denominator and shift every statistic, and it would do so invisibly, because the result
    would still be a number.
    """
    seen: set[tuple[Any, str]] = set()
    for prediction in predictions:
        key = (prediction.target_date, prediction.model_version)
        if key in seen:
            raise ObservationError(
                "two predictions were supplied for one target date and model version; the "
                "Stage 6.9 selection rule was not applied"
            )
        seen.add(key)


def value_of(prediction: ObservedPrediction, field: str) -> float | None:
    """One observed field's stored value, or ``None`` when the row does not carry it.

    ``predicted_room_nights`` is a column; everything else is a key of the stored feature object.
    A row missing a feature -- which the serving path cannot produce, since all nine are written
    together -- contributes nothing to that field rather than contributing a zero.
    """
    if field == "predicted_room_nights":
        return float(prediction.predicted_room_nights)
    raw = prediction.feature_values.get(field)
    return None if raw is None else float(raw)


def quantile(sorted_values: Sequence[float], probability: float) -> float:
    """The protocol's one declared convention: linear interpolation between order statistics.

    ``h = (n - 1) * p``; the result is the value at ``floor(h)`` moved toward the value at
    ``ceil(h)`` by the fractional part. This is what the standard library calls ``inclusive`` and
    what R calls type 7, and a test cross-checks the two so they cannot quietly disagree.

    Written out rather than delegated because the convention is part of the published protocol: a
    reader recomputing a stored number by hand needs the rule in the code that produced it.
    """
    if not sorted_values:
        raise ObservationError("a quantile of an empty series is not defined")
    if len(sorted_values) == 1:
        return sorted_values[0]

    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    return sorted_values[lower] + (position - lower) * (sorted_values[upper] - sorted_values[lower])


def summarise(
    values: Sequence[float], *, protocol: DistributionProtocol = PROTOCOL
) -> dict[str, Any]:
    """Descriptive statistics for one series, under the declared conventions.

    An empty series yields ``count = 0`` and ``None`` everywhere else -- never ``0.0``. A series
    of one yields that value for every statistic, which is arithmetically right and worth being
    able to see: a summary resting on one observation should look like one.
    """
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "maximum": None,
            "mean": None,
            "median": None,
            "quantiles": tuple((label, None) for label, _ in protocol.quantiles),
        }

    ordered = sorted(values)
    # fsum, not sum: an exactly-rounded total cannot depend on the order the rows arrived in.
    mean = math.fsum(ordered) / len(ordered)
    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "maximum": ordered[-1],
        "mean": mean,
        # The median IS the p50 quantile under this convention, for odd and even n alike, and a
        # test pins that rather than leaving two roads to one number.
        "median": quantile(ordered, 0.5),
        "quantiles": tuple(
            (label, quantile(ordered, probability)) for label, probability in protocol.quantiles
        ),
    }


def difference(target: float | None, baseline: float | None) -> float | None:
    """``target - baseline``, or ``None`` if either side was never measured.

    ``None`` rather than zero, deliberately: a window with nothing in it has not "not moved", and
    reporting a zero there would be a fabricated comparison.
    """
    if target is None or baseline is None:
        return None
    return target - baseline


@dataclass(frozen=True, slots=True)
class Partition:
    """The predictions split by model identity, before anything is summarised."""

    in_scope: tuple[ObservedPrediction, ...]
    out_of_scope_model_digest: int


def partition(
    predictions: Sequence[ObservedPrediction],
    *,
    approved_digest: str,
) -> Partition:
    """Split by canonical model digest, counting what is set aside.

    A prediction from another identity is reported and summarised into nothing. Pooling it would
    attribute its distribution to the approved model, which is the one thing a per-version
    grouping exists to prevent.
    """
    assert_one_per_group(predictions)

    in_scope = [row for row in predictions if row.canonical_model_digest == approved_digest]
    return Partition(
        in_scope=tuple(in_scope),
        out_of_scope_model_digest=len(predictions) - len(in_scope),
    )


def by_segment(
    predictions: Sequence[ObservedPrediction], *, protocol: DistributionProtocol = PROTOCOL
) -> dict[str, tuple[ObservedPrediction, ...]]:
    """Group by Stage 6.9's segments, reading stored inputs and nothing else.

    Both segments are always present, empty or not. A segment that disappeared when it held no
    rows would make an absent denominator indistinguishable from a zero one.
    """
    buckets: dict[str, list[ObservedPrediction]] = {
        protocol.segment_below_calibration: [],
        protocol.segment_within_calibration: [],
    }
    for prediction in sorted(predictions, key=lambda row: (row.target_date, row.feature_digest)):
        buckets[segment_of(prediction.feature_values)].append(prediction)
    return {segment: tuple(rows) for segment, rows in buckets.items()}


__all__ = [
    "PROTOCOL",
    "ObservationError",
    "ObservedPrediction",
    "Partition",
    "assert_one_per_group",
    "by_segment",
    "difference",
    "partition",
    "quantile",
    "summarise",
    "value_of",
]
