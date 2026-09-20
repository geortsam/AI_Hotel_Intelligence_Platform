"""Pairing served predictions with what actually happened. Pure: no SQL, no HTTP, no clock.

Stage 6.9. Takes the predictions a hotel was served, the realised demand for the same dates,
and the day the evaluation is being made as of; returns metrics under
:data:`~app.ml.accuracy_protocol.PROTOCOL`. It decides nothing about whether those metrics are
good.

## The one bridge to ``ml``

``ml/metrics.py`` already defines MAE, RMSE and sMAPE, with their denominators, their empty-set
behaviour and the reason MAPE is absent. **This module imports those definitions rather than
restating them**, through a function-local import, and it is the only module in ``backend/``
that does.

That is the pattern ``app.ml.artifact_store`` already established for ``ml.artifact`` and
``ml.inference``, and it is sound for the same reasons: the import is function-local so nothing
is pulled in at module scope; ``ml/metrics.py`` depends on nothing outside the standard library,
so no dependency is added and ``backend/requirements.txt`` does not move; it is already one of
the thirteen ``ml/`` modules the production image ships, so the image does not change either;
and the arrow stays one-way, because ``ml/`` still imports nothing from ``backend/``.

The honest cost, stated rather than buried: the approved backend-to-``ml`` surface is now two
named modules instead of one. The alternative was a second copy of three formulas whose exact
denominators and edge cases are the whole point of having written them down once -- the same
trade this codebase already refuses for occupancy, where ``MlDemandRepository`` imports
``OCCUPANCY_STATUSES`` rather than restating it.

``ml.metrics.difference`` and ``ml.metrics.pooled`` are deliberately **not** imported.
``difference`` is a baseline comparison and ``pooled`` combines metric sets across groups; this
stage performs neither, and a test asserts neither name appears anywhere in ``backend/``.

## What this module refuses to do

It does not select. Selection happens in the database, as one grouped ``DISTINCT ON`` whose
``ORDER BY`` is the protocol's rule, because that is where a "one row per group" claim can
actually be enforced rather than hoped for. What this module does is **check** that the rows it
was handed satisfy the invariant, and refuse to score them if they do not -- a silently pooled
duplicate would inflate a denominator in a way no reader could see.

It does not consult a clock. ``as_of_date`` arrives as an argument, and there is no call to
``now``, ``today`` or ``utcnow`` on this path.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.ml.accuracy_protocol import (
    PROTOCOL,
    AccuracyProtocol,
    is_eligible,
    last_settled_target_date,
    segment_of,
)


class AccuracyError(Exception):
    """An input that breaks the protocol's own assumptions rather than a caller's mistake."""


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """One prediction, as the repository hands it over after selection.

    Deliberately not the ORM row: this module maps no table and imports no SQLAlchemy. It also
    carries no internal key -- the row's ``id`` decides selection inside the query and has no
    business travelling any further.
    """

    target_date: dt.date
    forecast_horizon_days: int
    model_version: str
    canonical_model_digest: str
    feature_digest: str
    feature_values: Mapping[str, Any]
    predicted_room_nights: float


@dataclass(frozen=True, slots=True)
class Partition:
    """The candidates split by the protocol's rules, before any metric is computed."""

    eligible: tuple[ScoredCandidate, ...]
    ineligible_by_settlement: int
    out_of_scope_model_digest: int


@dataclass(frozen=True, slots=True)
class SegmentScoring:
    """One segment's paired series and its skip count, ready for ``metric_set``."""

    segment: str
    actuals: tuple[float, ...]
    forecasts: tuple[float, ...]
    feature_digests: tuple[str, ...]
    skipped: int


def assert_one_per_group(candidates: Sequence[ScoredCandidate]) -> None:
    """Refuse a set that holds two predictions for one group.

    The query is supposed to have applied the selection rule already. If it did not, scoring
    both would pool two predictions into one denominator -- exactly what the protocol forbids --
    and it would do so invisibly, since the metric would still be a number.
    """
    seen: set[tuple[dt.date, int, str]] = set()
    for candidate in candidates:
        key = (candidate.target_date, candidate.forecast_horizon_days, candidate.model_version)
        if key in seen:
            raise AccuracyError(
                "two predictions were supplied for one target date, horizon and model version; "
                "the selection rule was not applied"
            )
        seen.add(key)


def partition(
    candidates: Sequence[ScoredCandidate],
    *,
    as_of_date: dt.date,
    approved_digest: str,
    protocol: AccuracyProtocol = PROTOCOL,
) -> Partition:
    """Split candidates into what may be scored and what may not, counting each reason.

    Order matters and is declared: settlement first, then model identity. A prediction that has
    not settled is not *also* counted as out of scope, so the three counts partition the
    candidate set exactly and a reader can add them up.
    """
    assert_one_per_group(candidates)

    eligible: list[ScoredCandidate] = []
    ineligible = 0
    out_of_scope = 0
    for candidate in candidates:
        if not is_eligible(candidate.target_date, as_of_date, protocol=protocol):
            ineligible += 1
        elif candidate.canonical_model_digest != approved_digest:
            out_of_scope += 1
        else:
            eligible.append(candidate)
    return Partition(
        eligible=tuple(eligible),
        ineligible_by_settlement=ineligible,
        out_of_scope_model_digest=out_of_scope,
    )


def score_segments(
    candidates: Sequence[ScoredCandidate],
    realised: Mapping[dt.date, int],
    *,
    protocol: AccuracyProtocol = PROTOCOL,
) -> dict[str, SegmentScoring]:
    """Pair each candidate with its realised demand, split by segment.

    A target date absent from *realised* is **skipped**, never scored as zero. The distinction
    is the same one ``MlDemandRepository.demand_by_date`` already makes by omitting empty days
    rather than reporting them: "nothing was recorded" and "nothing happened" are different
    claims, and only one of them belongs in a denominator.
    """
    buckets: dict[str, dict[str, list[Any]]] = {
        protocol.segment_below_calibration: {"a": [], "f": [], "d": [], "skipped": [0]},
        protocol.segment_within_calibration: {"a": [], "f": [], "d": [], "skipped": [0]},
    }

    for candidate in sorted(candidates, key=lambda row: (row.target_date, row.model_version)):
        bucket = buckets[segment_of(candidate.feature_values, protocol=protocol)]
        actual = realised.get(candidate.target_date)
        if actual is None:
            bucket["skipped"][0] += 1
            continue
        bucket["a"].append(float(actual))
        bucket["f"].append(float(candidate.predicted_room_nights))
        bucket["d"].append(candidate.feature_digest)

    return {
        segment: SegmentScoring(
            segment=segment,
            actuals=tuple(bucket["a"]),
            forecasts=tuple(bucket["f"]),
            feature_digests=tuple(bucket["d"]),
            skipped=int(bucket["skipped"][0]),
        )
        for segment, bucket in buckets.items()
    }


def metrics_for(scoring: SegmentScoring) -> Any:
    """``ml.metrics.metric_set`` over one segment. The only bridge, and it is function-local.

    Returns ``ml.metrics.MetricSet``. Annotated as ``Any`` because naming the type at module
    scope would require importing ``ml`` there, which is precisely what this arrangement exists
    to avoid; ``app.schemas.ml_accuracy`` names it under ``TYPE_CHECKING`` for readers.
    """
    from ml.metrics import metric_set

    return metric_set(scoring.actuals, scoring.forecasts, skipped=scoring.skipped)


def scored_window(
    window_from: dt.date,
    window_to: dt.date,
    as_of_date: dt.date,
    *,
    protocol: AccuracyProtocol = PROTOCOL,
) -> tuple[dt.date, dt.date] | None:
    """The part of the requested window that has cleared the settlement lag, or ``None``.

    ``None`` means no date in the window is scorable yet, which is a normal answer for a young
    deployment rather than an error -- and it is the answer that keeps the empty-set metrics at
    ``None`` instead of at zero.
    """
    last = last_settled_target_date(as_of_date, protocol=protocol)
    end = min(window_to, last)
    if window_from > end:
        return None
    return window_from, end


__all__ = [
    "AccuracyError",
    "Partition",
    "ScoredCandidate",
    "SegmentScoring",
    "assert_one_per_group",
    "metrics_for",
    "partition",
    "score_segments",
    "scored_window",
]
