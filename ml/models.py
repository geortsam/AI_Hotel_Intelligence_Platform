"""The two forecasters Stage 6.3 measures, and the rule that decides what either may look at.

Two methods, evaluated on exactly the same observations:

* **`seasonal_naive_7`** -- demand on the target date is forecast as the demand seven days
  earlier. Deterministic, explainable in one sentence, and it is the thing a learned model has
  to beat to have earned its dependency.
* **`hist_gradient_boosting`** -- ``HistGradientBoostingRegressor``, the smallest conventional
  learned regressor that handles a small tabular panel without a preprocessing pipeline.

## The horizon decides the features, not taste

The Stage 6.2 dataset was built at ``horizon_days = 1``: every feature in it is guaranteed
knowable one day before its target date, and some of them are knowable *only* one day before.
Stage 6.3 forecasts at **seven** days. That is not a free change of a number -- at a seven-day
horizon, ``demand_lag_1`` reads a day the forecaster has not lived through yet.

So admissibility is computed from the horizon (:func:`feature_lead_days`,
:func:`select_model_features`) rather than listed by hand, and a feature whose name this module
does not recognise is **refused** instead of assumed safe. The exclusions at seven days are
``demand_lag_1``, all three rolling means (their windows end the day before the target) and
``on_books_room_nights_at_cutoff`` (reconstructed at the same one-day cutoff).

## Capacity, which is absent rather than zero

``rooms_existing_at_cutoff`` is ``None`` on every row of the offline dataset: the published
source carries no room inventory. Stage 6.3 takes the explicit decision to **exclude it from
the model feature matrix while leaving the Stage 6.1 contract untouched** -- option (A). The
alternative, feeding it to the estimator as a missing value, would mean training on a column
that is *entirely* missing offline and *entirely* present in production, which is not a missing
value at all but a different feature wearing the same name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sklearn.ensemble import HistGradientBoostingRegressor

from ml.loading import ProcessedRow

MODEL_NAME = "demand_baseline"
MODEL_VERSION = "demand_baseline_v1"

BASELINE_METHOD = "seasonal_naive_7"
LEARNED_METHOD = "hist_gradient_boosting"

#: The seasonal reference: same weekday, one week earlier. Seven because hotel demand is
#: weekly-periodic -- Stage 6.1's V1 baseline reaches for the same period, from the same
#: reasoning, and using a different one here would make the two incomparable.
SEASONAL_NAIVE_LAG_DAYS = 7

#: Features derived from the target date alone. Knowable at any horizon, including infinite.
CALENDAR_FEATURES: tuple[str, ...] = (
    "day_of_week",
    "day_of_month",
    "month",
    "week_of_year",
    "day_of_year",
    "is_weekend",
)

#: Present in the Stage 6.1 contract, absent from every offline row. Excluded by name and for a
#: stated reason, not by happening to be missing.
OFFLINE_UNAVAILABLE_FEATURES: Mapping[str, str] = {
    "rooms_existing_at_cutoff": (
        "the published source carries no room inventory, so the column is None on every "
        "offline row; excluded from the model matrix rather than fabricated or imputed"
    ),
}

_LAG = re.compile(r"^demand_lag_(\d+)$")
_ROLLING = re.compile(r"^demand_rolling_mean_(\d+)$")

#: Features reconstructed at the dataset's own cutoff, one day before the target date.
_CUTOFF_FEATURES: frozenset[str] = frozenset(
    {"on_books_room_nights_at_cutoff", "rooms_existing_at_cutoff"}
)


class ModelContractError(Exception):
    """A configuration that would let a forecaster see something it cannot have seen."""


def feature_lead_days(name: str) -> int | None:
    """How many days before its target date a feature's value is knowable.

    ``None`` means unbounded -- a calendar feature is derivable from the date itself, so no
    horizon can make it unavailable.

    An unrecognised name raises. That is the whole value of this function: a feature added
    later without a lead time recorded here must stop the evaluation rather than be quietly
    admitted at every horizon.
    """
    if name in CALENDAR_FEATURES:
        return None
    lag = _LAG.match(name)
    if lag:
        return int(lag.group(1))
    if _ROLLING.match(name):
        # The window ends at cutoff_date, which is the day before the target date.
        return 1
    if name in _CUTOFF_FEATURES:
        return 1
    raise ModelContractError(
        f"no lead time is recorded for feature {name!r}; add one rather than assuming it is "
        "safe at every horizon"
    )


@dataclass(frozen=True, slots=True)
class FeatureSelection:
    """What the model may use at this horizon, and what it may not, with reasons."""

    horizon_days: int
    selected: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "horizon_days": self.horizon_days,
            "selected": list(self.selected),
            "excluded": [{"feature": name, "reason": reason} for name, reason in self.excluded],
        }


def select_model_features(available: Sequence[str], *, horizon_days: int) -> FeatureSelection:
    """The admissible feature subset for a forecast made *horizon_days* ahead.

    Order follows the dataset's own column order, which Stage 6.1 fixed, so the design matrix
    columns mean the same thing on every run.
    """
    if horizon_days < 1:
        raise ModelContractError(f"horizon_days must be at least 1, got {horizon_days}")

    selected: list[str] = []
    excluded: list[tuple[str, str]] = []
    for name in available:
        unavailable = OFFLINE_UNAVAILABLE_FEATURES.get(name)
        if unavailable is not None:
            excluded.append((name, unavailable))
            continue
        lead = feature_lead_days(name)
        if lead is None or lead >= horizon_days:
            selected.append(name)
        else:
            excluded.append(
                (
                    name,
                    f"knowable only {lead} day(s) before the target date, which is inside a "
                    f"{horizon_days}-day forecast horizon",
                )
            )
    if not selected:
        raise ModelContractError(
            f"no feature in {list(available)} is knowable {horizon_days} day(s) ahead"
        )
    return FeatureSelection(horizon_days, tuple(selected), tuple(excluded))


# --- seasonal-naive baseline -------------------------------------------------------------------


def seasonal_naive_prediction(
    row: ProcessedRow, *, lag_days: int = SEASONAL_NAIVE_LAG_DAYS, horizon_days: int
) -> float | None:
    """Realised demand *lag_days* before the target date, or ``None`` when it does not exist.

    The value is read from the dataset's own ``demand_lag_{k}`` column rather than looked up in
    a second index built here. That is deliberate: a separate lookup would be a second
    definition of "demand seven days ago", and two definitions of one quantity drift. A test
    checks this column against an independently reconstructed history.

    ``None`` is returned, not zero and not the series mean: a week with no recorded predecessor
    is a prediction the baseline cannot make, and saying so is the whole point of the rule.
    """
    if lag_days < horizon_days:
        raise ModelContractError(
            f"a {lag_days}-day seasonal reference is not knowable {horizon_days} day(s) ahead"
        )
    return row.features.get(f"demand_lag_{lag_days}")


def seasonal_naive_predictions(
    rows: Sequence[ProcessedRow],
    *,
    lag_days: int = SEASONAL_NAIVE_LAG_DAYS,
    horizon_days: int,
) -> tuple[float | None, ...]:
    return tuple(
        seasonal_naive_prediction(row, lag_days=lag_days, horizon_days=horizon_days) for row in rows
    )


# --- learned model -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EstimatorConfig:
    """Every hyper-parameter that is not a scikit-learn default, written down once.

    ``loss`` and ``early_stopping`` are fixed rather than configurable, and the second one
    matters: ``early_stopping="auto"`` -- which is what this estimator does by default once the
    training set passes 10,000 rows -- carves out an internal validation split *at random*.
    A random split of a time series is exactly the thing this stage exists not to do, and it
    would also make the fit non-reproducible from the seed alone. It is switched off, always,
    and the number of boosting iterations is fixed instead.
    """

    max_iter: int = 200
    learning_rate: float = 0.05
    max_leaf_nodes: int = 31
    min_samples_leaf: int = 20
    l2_regularization: float = 0.0
    max_bins: int = 255
    random_state: int = 0

    #: Fixed, not tunable. See the class docstring.
    loss: str = field(default="squared_error", init=False)
    early_stopping: bool = field(default=False, init=False)

    def build(self) -> HistGradientBoostingRegressor:
        return HistGradientBoostingRegressor(
            loss=self.loss,
            max_iter=self.max_iter,
            learning_rate=self.learning_rate,
            max_leaf_nodes=self.max_leaf_nodes,
            min_samples_leaf=self.min_samples_leaf,
            l2_regularization=self.l2_regularization,
            max_bins=self.max_bins,
            early_stopping=self.early_stopping,
            random_state=self.random_state,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "estimator": "sklearn.ensemble.HistGradientBoostingRegressor",
            "loss": self.loss,
            "max_iter": self.max_iter,
            "learning_rate": self.learning_rate,
            "max_leaf_nodes": self.max_leaf_nodes,
            "min_samples_leaf": self.min_samples_leaf,
            "l2_regularization": self.l2_regularization,
            "max_bins": self.max_bins,
            "early_stopping": self.early_stopping,
            "random_state": self.random_state,
        }


@dataclass(frozen=True, slots=True)
class DesignMatrix:
    """Rows the estimator can actually use, and the ones it cannot, kept apart.

    A row missing any selected feature is **not** imputed and **not** zero-filled. It is held
    out and counted, so "the model scored 1,344 of 1,400 days" stays a fact rather than a
    footnote.
    """

    features: tuple[tuple[float, ...], ...]
    targets: tuple[float, ...]
    used: tuple[int, ...]
    skipped: tuple[int, ...]


def design_matrix(rows: Sequence[ProcessedRow], feature_names: Sequence[str]) -> DesignMatrix:
    used: list[int] = []
    skipped: list[int] = []
    features: list[tuple[float, ...]] = []
    targets: list[float] = []
    for index, row in enumerate(rows):
        values = [row.features.get(name) for name in feature_names]
        if any(value is None for value in values):
            skipped.append(index)
            continue
        used.append(index)
        features.append(tuple(float(value) for value in values if value is not None))
        targets.append(float(row.target_room_nights))
    return DesignMatrix(tuple(features), tuple(targets), tuple(used), tuple(skipped))


@dataclass(frozen=True, slots=True)
class LearnedModel:
    """A fitted estimator, together with the exact columns it was fitted on."""

    feature_names: tuple[str, ...]
    config: EstimatorConfig
    training_rows: int
    estimator: Any

    @classmethod
    def fit(
        cls,
        rows: Sequence[ProcessedRow],
        feature_names: Sequence[str],
        config: EstimatorConfig,
    ) -> LearnedModel:
        matrix = design_matrix(rows, feature_names)
        if not matrix.features:
            raise ModelContractError(
                f"no training row has all {len(feature_names)} selected feature(s) present"
            )
        estimator = config.build()
        estimator.fit([list(values) for values in matrix.features], list(matrix.targets))
        return cls(tuple(feature_names), config, len(matrix.features), estimator)

    def predict(self, rows: Sequence[ProcessedRow]) -> tuple[float | None, ...]:
        """One forecast per row, ``None`` where a selected feature is absent.

        The estimator is called once for the whole batch rather than row by row, because a
        per-row call would be both slower and -- with a tree ensemble -- a different floating
        point summation order.
        """
        matrix = design_matrix(rows, self.feature_names)
        out: list[float | None] = [None] * len(rows)
        if not matrix.features:
            return tuple(out)
        raw = self.estimator.predict([list(values) for values in matrix.features])
        for position, index in enumerate(matrix.used):
            out[index] = float(raw[position])
        return tuple(out)
