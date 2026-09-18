"""Rolling-origin backtesting: many chronological origins, an expanding training window, and
an evaluation block that never touches a day the model was trained on.

## Why not the Stage 6.2 split

Stage 6.2 cut the dataset once, 60/20/20 by date. That split is real and it is in the manifest,
but it cannot be the figure of record here: validation lands in November-April and test in
April-August, so the two periods describe different seasons of a seasonal business, and a
single number from a single split would be as much a statement about which months it happened
to land on as about the model. Stage 6.2 said so in its own limitations, and this module is the
answer to it.

## The protocol, stated so it can be checked rather than trusted

    dates            every distinct target date in the dataset, ascending
    first origin     dates[minimum_train_days - 1]
    origin(k)        first origin + k * step_days, while origin < dates[-1]
    train(k)         every row with target_date <= origin(k)
    evaluate(k)      every row with origin(k) < target_date <= origin(k) + horizon_days

Origins are **calendar dates advanced by a fixed step**, not indices, and they are generated
before a single prediction is made. Nothing in this module can look at a score and then choose
an origin -- which is the failure mode that makes a backtest flattering, and it is prevented by
construction rather than by intention.

The window **expands**: fold k trains on everything up to its origin. A sliding fixed-width
window is the alternative and is not used here; with two years of history, discarding the
earlier of the two summers to keep the window short would cost more than it buys.

``step_days == horizon_days`` makes the evaluation blocks tile: every date after the first
origin is evaluated exactly once, so the pooled metrics are an average over the whole period
rather than over whichever days the steps happened to land on.

## The assertion that matters

Every fold checks ``max(train date) <= origin < min(evaluation date)`` before it fits anything.
It is cheap, it runs on every fold of every run, and it is the one invariant whose violation
would leave every number in this stage looking entirely reasonable.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ml.loading import ProcessedRow
from ml.metrics import MetricSet, difference, metric_set
from ml.models import (
    BASELINE_METHOD,
    LEARNED_METHOD,
    SEASONAL_NAIVE_LAG_DAYS,
    EstimatorConfig,
    FeatureSelection,
    LearnedModel,
    seasonal_naive_predictions,
    select_model_features,
)

#: A full seasonal year before the first forecast. Chosen so that every fold has seen each
#: calendar month at least once, which is the minimum for a model that is handed `month` and
#: `week_of_year` as features. It is a judgement about seasonality, not a tuned value: no
#: metric was consulted in setting it.
DEFAULT_MINIMUM_TRAIN_DAYS = 365

#: Below this many evaluated observations a group's metrics are reported as insufficient
#: coverage rather than as a number. A handful of days is not an error rate.
MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC = 30


class EvaluationError(Exception):
    """A protocol that would not measure what it claims to measure."""


@dataclass(frozen=True, slots=True)
class RollingOriginPolicy:
    """The origin-generation rule, as data, so the manifest can record exactly what ran."""

    horizon_days: int = SEASONAL_NAIVE_LAG_DAYS
    minimum_train_days: int = DEFAULT_MINIMUM_TRAIN_DAYS
    step_days: int = SEASONAL_NAIVE_LAG_DAYS

    def __post_init__(self) -> None:
        if self.horizon_days < 1:
            raise EvaluationError(f"horizon_days must be at least 1, got {self.horizon_days}")
        if self.minimum_train_days < 1:
            raise EvaluationError(
                f"minimum_train_days must be at least 1, got {self.minimum_train_days}"
            )
        if self.step_days < self.horizon_days:
            raise EvaluationError(
                f"step_days ({self.step_days}) below horizon_days ({self.horizon_days}) would "
                "make evaluation windows overlap, and a pooled metric would then count some "
                "days twice"
            )

    @property
    def windows_tile(self) -> bool:
        return self.step_days == self.horizon_days

    def as_dict(self) -> dict[str, object]:
        return {
            "scheme": "expanding-window rolling origin",
            "horizon_days": self.horizon_days,
            "minimum_train_days": self.minimum_train_days,
            "step_days": self.step_days,
            "windows_tile": self.windows_tile,
            "origin_rule": (
                "first origin = the date with minimum_train_days distinct dates at or before "
                "it; subsequent origins advance by step_days until the last dataset date; "
                "evaluation window = (origin, origin + horizon_days]"
            ),
            "shuffle": False,
            "random_cross_validation": False,
        }


def rolling_origins(dates: Sequence[dt.date], policy: RollingOriginPolicy) -> tuple[dt.date, ...]:
    """Every forecast origin, generated before any model is fitted.

    Raises rather than returning an empty tuple: a backtest with no origins is not a result,
    and a caller handed ``()`` tends to report zero folds as though that were an outcome.
    """
    ordered = sorted(set(dates))
    if len(ordered) < policy.minimum_train_days + 1:
        raise EvaluationError(
            f"{len(ordered)} distinct date(s) cannot support a backtest that reserves "
            f"{policy.minimum_train_days} for training and needs at least one more to forecast"
        )
    first = ordered[policy.minimum_train_days - 1]
    last = ordered[-1]
    origins: list[dt.date] = []
    origin = first
    while origin < last:
        origins.append(origin)
        origin = origin + dt.timedelta(days=policy.step_days)
    if not origins:
        raise EvaluationError("the policy produced no forecast origin")
    return tuple(origins)


@dataclass(frozen=True, slots=True)
class Fold:
    """One origin, and the two disjoint row sets it induces."""

    index: int
    origin: dt.date
    train_start: dt.date
    train_end: dt.date
    evaluation_start: dt.date
    evaluation_end: dt.date
    train_rows: int
    evaluation_rows: int
    train_hotels: tuple[str, ...]
    evaluation_hotels: tuple[str, ...]
    #: False when the evaluation window ran off the end of the dataset, which happens to the
    #: last fold whenever the remaining dates are not an exact multiple of the step. Flagged
    #: rather than dropped: its predictions are real, and its metrics are simply computed over
    #: fewer observations -- which the observation count next to them already says.
    complete_window: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "fold": self.index,
            "origin": self.origin.isoformat(),
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "evaluation_start": self.evaluation_start.isoformat(),
            "evaluation_end": self.evaluation_end.isoformat(),
            "train_rows": self.train_rows,
            "evaluation_rows": self.evaluation_rows,
            "train_hotels": list(self.train_hotels),
            "evaluation_hotels": list(self.evaluation_hotels),
            "complete_window": self.complete_window,
        }


@dataclass(frozen=True, slots=True)
class SkippedFold:
    """An origin that produced no evaluable observation, kept rather than dropped."""

    index: int
    origin: dt.date
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"fold": self.index, "origin": self.origin.isoformat(), "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Prediction:
    """One forecast day: what happened, and what each method said -- or could not say."""

    fold_index: int
    hotel_key: str
    target_date: dt.date
    actual: int
    baseline: float | None
    learned: float | None


def _split(
    rows: Sequence[ProcessedRow], origin: dt.date, horizon_days: int
) -> tuple[list[ProcessedRow], list[ProcessedRow]]:
    train = [row for row in rows if row.target_date <= origin]
    end = origin + dt.timedelta(days=horizon_days)
    evaluation = [row for row in rows if origin < row.target_date <= end]
    return train, evaluation


def assert_fold_is_chronological(
    train: Sequence[ProcessedRow], evaluation: Sequence[ProcessedRow], origin: dt.date
) -> None:
    """Prove the separation instead of documenting it."""
    if not train or not evaluation:
        raise EvaluationError("a fold must have both training and evaluation rows")
    latest_train = max(row.target_date for row in train)
    earliest_eval = min(row.target_date for row in evaluation)
    if latest_train > origin:
        raise EvaluationError(f"training reaches {latest_train}, past the origin {origin}")
    if earliest_eval <= origin:
        raise EvaluationError(
            f"evaluation starts {earliest_eval}, at or before the origin {origin}"
        )
    if latest_train >= earliest_eval:
        raise EvaluationError(
            f"training reaches {latest_train} and evaluation starts {earliest_eval}"
        )


@dataclass(frozen=True, slots=True)
class FoldResult:
    """One fold's predictions and its two independent error measurements."""

    fold: Fold
    predictions: tuple[Prediction, ...]
    baseline: MetricSet
    learned: MetricSet

    def as_dict(self) -> dict[str, object]:
        record = dict(self.fold.as_dict())
        record["baseline"] = self.baseline.as_dict()
        record["learned"] = self.learned.as_dict()
        record["comparison"] = comparison(self.predictions)
        return record


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Everything one backtest produced, with nothing averaged away."""

    policy: RollingOriginPolicy
    selection: FeatureSelection
    config: EstimatorConfig
    folds: tuple[FoldResult, ...]
    skipped_folds: tuple[SkippedFold, ...]
    predictions: tuple[Prediction, ...]
    dataset_rows: int
    dataset_hotels: tuple[str, ...]

    @property
    def baseline_pooled(self) -> MetricSet:
        return pooled_metrics(self.predictions, "baseline")

    @property
    def learned_pooled(self) -> MetricSet:
        return pooled_metrics(self.predictions, "learned")


def _method_values(
    predictions: Sequence[Prediction], method: str
) -> tuple[list[float], list[float], int]:
    """Actual/forecast pairs for one method, plus how many it could not forecast.

    An unrecognised method name raises rather than falling through to the other one: a typo
    that silently scored the learned model twice would produce a comparison of nothing against
    itself, and it would look entirely plausible.
    """
    if method not in ("baseline", "learned"):
        raise EvaluationError(f"unknown method {method!r}; expected 'baseline' or 'learned'")
    actuals: list[float] = []
    forecasts: list[float] = []
    skipped = 0
    for prediction in predictions:
        value = prediction.baseline if method == "baseline" else prediction.learned
        if value is None:
            skipped += 1
            continue
        actuals.append(float(prediction.actual))
        forecasts.append(value)
    return actuals, forecasts, skipped


def pooled_metrics(predictions: Sequence[Prediction], method: str) -> MetricSet:
    """Metrics recomputed over the pooled observations.

    Not the mean of the per-fold metrics: that would weight a fold with two evaluable days as
    heavily as one with fourteen.
    """
    actuals, forecasts, skipped = _method_values(predictions, method)
    return metric_set(actuals, forecasts, skipped=skipped)


def comparison(predictions: Sequence[Prediction]) -> dict[str, object]:
    """Learned minus baseline, over the observations where **both** produced a forecast.

    Paired on purpose. Comparing each method's pooled metric over its own available
    observations would compare two different sets of days, and the difference would then
    partly measure which days each method declined to forecast.

    Negative means the learned model's error was smaller on those days. It is a measured
    difference and nothing here turns it into a ranking.
    """
    actuals: list[float] = []
    baseline: list[float] = []
    learned: list[float] = []
    for prediction in predictions:
        if prediction.baseline is None or prediction.learned is None:
            continue
        actuals.append(float(prediction.actual))
        baseline.append(prediction.baseline)
        learned.append(prediction.learned)

    baseline_metrics = metric_set(actuals, baseline)
    learned_metrics = metric_set(actuals, learned)
    return {
        "paired_observations": len(actuals),
        "baseline": baseline_metrics.as_dict(),
        "learned": learned_metrics.as_dict(),
        "learned_minus_baseline": {
            "mae": _round(difference(learned_metrics.mae, baseline_metrics.mae)),
            "rmse": _round(difference(learned_metrics.rmse, baseline_metrics.rmse)),
            "smape": _round(difference(learned_metrics.smape, baseline_metrics.smape)),
        },
    }


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def per_hotel_metrics(
    predictions: Sequence[Prediction],
    *,
    minimum: int = MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC,
) -> dict[str, object]:
    """Metrics per hotel, or an explicit statement that there were not enough days.

    Two hotels is not a sample from which generalisation can be claimed, and nothing in this
    function implies otherwise -- it reports each hotel separately so that a pooled number
    cannot hide one of them being much worse than the other.
    """
    groups: dict[str, list[Prediction]] = {}
    for prediction in predictions:
        groups.setdefault(prediction.hotel_key, []).append(prediction)

    out: dict[str, object] = {}
    for hotel_key in sorted(groups):
        group = groups[hotel_key]
        evaluated = sum(1 for p in group if p.baseline is not None or p.learned is not None)
        if evaluated < minimum:
            out[hotel_key] = {
                "sufficient_coverage": False,
                "evaluated_observations": evaluated,
                "minimum_required": minimum,
                "reason": "insufficient coverage; no metric is reported",
            }
            continue
        out[hotel_key] = {
            "sufficient_coverage": True,
            "predictions": len(group),
            "baseline": pooled_metrics(group, "baseline").as_dict(),
            "learned": pooled_metrics(group, "learned").as_dict(),
            "comparison": comparison(group),
        }
    return out


def evaluate(
    rows: Sequence[ProcessedRow],
    *,
    policy: RollingOriginPolicy | None = None,
    config: EstimatorConfig | None = None,
    feature_names: Sequence[str] | None = None,
) -> EvaluationResult:
    """Run the whole backtest. Deterministic from the rows and the configuration alone."""
    policy = policy or RollingOriginPolicy()
    config = config or EstimatorConfig()
    available = feature_names or _dataset_feature_names(rows)
    selection = select_model_features(available, horizon_days=policy.horizon_days)

    ordered = sorted(rows, key=lambda row: (row.target_date, row.hotel_key))
    origins = rolling_origins([row.target_date for row in ordered], policy)

    folds: list[FoldResult] = []
    skipped: list[SkippedFold] = []
    predictions: list[Prediction] = []

    for index, origin in enumerate(origins):
        train, evaluation = _split(ordered, origin, policy.horizon_days)
        if not evaluation:
            skipped.append(
                SkippedFold(index, origin, "no observation falls inside the evaluation window")
            )
            continue
        if not train:
            skipped.append(SkippedFold(index, origin, "no observation precedes the origin"))
            continue
        assert_fold_is_chronological(train, evaluation, origin)

        model = LearnedModel.fit(train, selection.selected, config)
        learned_values = model.predict(evaluation)
        baseline_values = seasonal_naive_predictions(evaluation, horizon_days=policy.horizon_days)

        fold_predictions = tuple(
            Prediction(
                fold_index=index,
                hotel_key=row.hotel_key,
                target_date=row.target_date,
                actual=row.target_room_nights,
                baseline=baseline,
                learned=learned,
            )
            for row, baseline, learned in zip(
                evaluation, baseline_values, learned_values, strict=True
            )
        )
        predictions.extend(fold_predictions)

        fold = Fold(
            index=index,
            origin=origin,
            train_start=min(row.target_date for row in train),
            train_end=max(row.target_date for row in train),
            evaluation_start=min(row.target_date for row in evaluation),
            evaluation_end=max(row.target_date for row in evaluation),
            train_rows=len(train),
            evaluation_rows=len(evaluation),
            train_hotels=tuple(sorted({row.hotel_key for row in train})),
            evaluation_hotels=tuple(sorted({row.hotel_key for row in evaluation})),
            complete_window=(
                len(evaluation) == policy.horizon_days * len({row.hotel_key for row in evaluation})
            ),
        )
        folds.append(
            FoldResult(
                fold=fold,
                predictions=fold_predictions,
                baseline=pooled_metrics(fold_predictions, "baseline"),
                learned=pooled_metrics(fold_predictions, "learned"),
            )
        )

    if not folds:
        raise EvaluationError("every origin was skipped; there is nothing to report")
    return EvaluationResult(
        policy=policy,
        selection=selection,
        config=config,
        folds=tuple(folds),
        skipped_folds=tuple(skipped),
        predictions=tuple(predictions),
        dataset_rows=len(ordered),
        dataset_hotels=tuple(sorted({row.hotel_key for row in ordered})),
    )


def _dataset_feature_names(rows: Sequence[ProcessedRow]) -> tuple[str, ...]:
    if not rows:
        raise EvaluationError("no rows were supplied")
    return tuple(rows[0].features)


def method_names() -> Mapping[str, str]:
    return {"baseline": BASELINE_METHOD, "learned": LEARNED_METHOD}
