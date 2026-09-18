"""Forecast error metrics, with their denominators and edge cases written down.

Three metrics, chosen for what they refuse to hide:

* **MAE** -- mean absolute error, in room nights. The unit a hotelier can argue with.
* **RMSE** -- root mean squared error, also in room nights, but quadratic, so a handful of
  badly missed days moves it and cannot be averaged away.
* **sMAPE** -- symmetric mean absolute percentage error, scale-free, so two hotels of different
  size can be put beside each other.

**MAPE is deliberately absent.** Its denominator is the actual value, so a single zero-demand
day makes it undefined and a near-zero day makes it enormous. This dataset happens to contain
no zero-demand day -- measured, not assumed -- but a metric that is only safe because of a
property of one dataset is not a metric worth keeping, and the production database will not
share that property.

Everything here is pure: floats in, floats out, no estimator, no state.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: An empty set of observations yields ``None`` for every metric, never ``0.0``. Zero error and
#: nothing measured are different claims, and only one of them is good news.
EMPTY_METRIC: float | None = None

METRIC_DEFINITIONS: dict[str, str] = {
    "mae": "mean(|actual - forecast|) over evaluated observations; unit: room nights",
    "rmse": ("sqrt(mean((actual - forecast)^2)) over evaluated observations; unit: room nights"),
    "smape": (
        "100 * mean(2 * |actual - forecast| / (|actual| + |forecast|)); a term whose "
        "denominator is zero (actual and forecast both zero, i.e. an exact match) contributes "
        "0.0; range 0..200"
    ),
    "denominator": "the number of observations for which a forecast was produced",
    "skipped": (
        "observations for which the method could not produce a forecast; excluded from every "
        "metric above and reported separately, never counted as zero error"
    ),
}


@dataclass(frozen=True, slots=True)
class MetricSet:
    """One method's error over one set of observations, and how many there were."""

    observations: int
    skipped: int
    mae: float | None
    rmse: float | None
    smape: float | None

    def as_dict(self, *, digits: int = 6) -> dict[str, object]:
        return {
            "observations": self.observations,
            "skipped": self.skipped,
            "mae": None if self.mae is None else round(self.mae, digits),
            "rmse": None if self.rmse is None else round(self.rmse, digits),
            "smape": None if self.smape is None else round(self.smape, digits),
        }


def absolute_errors(actuals: Sequence[float], forecasts: Sequence[float]) -> list[float]:
    if len(actuals) != len(forecasts):
        raise ValueError(
            f"{len(actuals)} actual(s) cannot be paired with {len(forecasts)} forecast(s)"
        )
    return [abs(a - f) for a, f in zip(actuals, forecasts, strict=True)]


def mean_absolute_error(actuals: Sequence[float], forecasts: Sequence[float]) -> float | None:
    errors = absolute_errors(actuals, forecasts)
    if not errors:
        return EMPTY_METRIC
    return math.fsum(errors) / len(errors)


def root_mean_squared_error(actuals: Sequence[float], forecasts: Sequence[float]) -> float | None:
    """``fsum`` rather than ``sum``: exactly rounded, so the result does not depend on the
    order the folds happened to be visited in."""
    if len(actuals) != len(forecasts):
        raise ValueError(
            f"{len(actuals)} actual(s) cannot be paired with {len(forecasts)} forecast(s)"
        )
    if not actuals:
        return EMPTY_METRIC
    squares = [(a - f) ** 2 for a, f in zip(actuals, forecasts, strict=True)]
    return math.sqrt(math.fsum(squares) / len(squares))


def symmetric_mean_absolute_percentage_error(
    actuals: Sequence[float], forecasts: Sequence[float]
) -> float | None:
    """sMAPE with the zero-denominator case decided rather than left to the floating-point gods.

    ``|actual| + |forecast| == 0`` can only happen when both are zero, which is an exact
    forecast of an empty day. Contributing ``0.0`` is the only reading that is not either a
    crash or a fabricated 100%.
    """
    if len(actuals) != len(forecasts):
        raise ValueError(
            f"{len(actuals)} actual(s) cannot be paired with {len(forecasts)} forecast(s)"
        )
    if not actuals:
        return EMPTY_METRIC
    terms: list[float] = []
    for actual, forecast in zip(actuals, forecasts, strict=True):
        denominator = abs(actual) + abs(forecast)
        terms.append(0.0 if denominator == 0 else 2.0 * abs(actual - forecast) / denominator)
    return 100.0 * math.fsum(terms) / len(terms)


def metric_set(
    actuals: Sequence[float], forecasts: Sequence[float], *, skipped: int = 0
) -> MetricSet:
    """All three metrics over one paired set, plus the count that was not forecastable."""
    return MetricSet(
        observations=len(actuals),
        skipped=skipped,
        mae=mean_absolute_error(actuals, forecasts),
        rmse=root_mean_squared_error(actuals, forecasts),
        smape=symmetric_mean_absolute_percentage_error(actuals, forecasts),
    )


def difference(left: float | None, right: float | None) -> float | None:
    """``left - right``, or ``None`` if either side was never measured.

    Used for ``learned_model - baseline``: negative means the learned model made a smaller
    error on those observations. It is a measurement and not a verdict, which is why this
    returns a number and nothing in this module returns a winner.
    """
    if left is None or right is None:
        return None
    return left - right


def pooled(sets: Iterable[MetricSet]) -> tuple[int, int]:
    """Observation and skip totals across folds.

    Deliberately not an average of per-fold metrics: the mean of per-fold MAEs weights a
    two-row fold as heavily as a fourteen-row one. Pooled metrics are recomputed from the
    pooled observations instead, and this helper only carries the counts.
    """
    observations = 0
    skipped = 0
    for item in sets:
        observations += item.observations
        skipped += item.skipped
    return observations, skipped
