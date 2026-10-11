"""R2.2b: same-hotel controls for the R2.2 cross-hotel experiment, pre-registered.

R2.2 refitted the served configuration on ONE hotel and scored it on the OTHER. R2.3 noted that
this changed two things at once relative to the pooled served model: the hotel and the amount of
training data. This experiment adds the missing control. Four single-hotel models, each fitted
on one hotel's eligible training rows with the Stage 6.3 configuration unchanged:

    city_hotel   -> city_hotel     (same-hotel control)
    resort_hotel -> resort_hotel   (same-hotel control)
    city_hotel   -> resort_hotel   (cross-hotel, as R2.2)
    resort_hotel -> city_hotel     (cross-hotel, as R2.2)

Two models fitted on the same hotel's rows are scored on both hotels, so "same hotel vs other
hotel" is compared at an equal amount of training data. Row selection, the 7-day cutoff, the lag
checks, the skip rule and ``seasonal_naive_7`` are R2.2's, reused from :mod:`ml.frozen_holdout`
rather than redefined. The served artifact is not loaded, scored, refitted or written; none of
these four models is written anywhere.

Beyond R2.2's metrics, every experiment records diagnostics that R2.2 did not: the mean signed
error (**prediction minus actual**: positive means over-prediction), the prediction range, the
training-target range, and the share of predictions outside it -- the quantities R2.3 could only
infer -- plus breakdowns by pre-registered ``demand_lag_7`` ranges.

## The protocol is fixed before anything is scored

Everything the experiment decides is in :func:`protocol_settings`; its SHA-256 is pinned as
:data:`PROTOCOL_SHA256`, committed before the first score was computed, and the pipeline refuses
to run if they differ. Pure: a dataset in, a JSON-ready record out; nothing is written here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ml.artifact import TRAINING_PARTITION
from ml.frozen_holdout import (
    DATASET_SHA256,
    DATASET_VERSION,
    FEATURE_COLUMNS,
    FEATURE_VERSION,
    HORIZON_DAYS,
    HOTELS,
    MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC,
    PERIODS,
    Scored,
    check_features,
    compare,
    coverage,
    cross_hotel_training_rows,
    estimator_forecaster,
    period_rows,
    require_dataset,
    score,
)
from ml.loading import ProcessedDataset, ProcessedRow
from ml.manifests import serialise
from ml.metrics import METRIC_DEFINITIONS
from ml.models import BASELINE_METHOD, EstimatorConfig, LearnedModel, design_matrix
from ml.pipelines.offline_demand import sha256_hex
from ml.registry import configuration_checksum

PROTOCOL_VERSION = "hotel_transfer_v1"


class HotelTransferError(Exception):
    """The data or the protocol is not the one this experiment was fixed for."""


@dataclass(frozen=True, slots=True)
class Experiment:
    train_hotel: str
    evaluate_hotel: str

    @property
    def name(self) -> str:
        return f"{self.train_hotel}->{self.evaluate_hotel}"

    @property
    def kind(self) -> str:
        return "same_hotel" if self.train_hotel == self.evaluate_hotel else "cross_hotel"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "train_hotel": self.train_hotel,
            "evaluate_hotel": self.evaluate_hotel,
        }


EXPERIMENTS: tuple[Experiment, ...] = (
    Experiment("city_hotel", "city_hotel"),
    Experiment("resort_hotel", "resort_hotel"),
    Experiment("city_hotel", "resort_hotel"),
    Experiment("resort_hotel", "city_hotel"),
)

#: Half-open ``[low, high)`` ranges of the row's ``demand_lag_7``. Taken from R2.3's description
#: of the TRAINING partition (no held-out row informed them), fixed here before scoring.
LAG_FEATURE = "demand_lag_7"
LAG_RANGES: tuple[tuple[float, float | None], ...] = (
    (0.0, 60.0),
    (60.0, 100.0),
    (100.0, 140.0),
    (140.0, 170.0),
    (170.0, 190.0),
    (190.0, None),
)

SIGN_CONVENTION = (
    "signed error = prediction - actual. Positive means over-prediction, negative means "
    "under-prediction. mean_signed_error is the plain mean over the paired rows."
)
RANGE_RULE = (
    "A prediction is outside the training-target range when it is strictly below the minimum or "
    "strictly above the maximum target_room_nights among the rows the model was fitted on. A "
    "prediction equal to either bound is inside. fraction_outside = (below + above) / paired."
)
COMPARISON_RULE = (
    "Every experiment is compared with the EVALUATED hotel's seasonal_naive_7 on the identical "
    "paired rows (both forecast). Wins, losses and ties on absolute error; win_proportion = "
    "wins / paired; differences learned - baseline. No threshold, no winner, no pooled headline: "
    "validation and test are reported separately and never combined."
)
OBSERVED_MAXIMA_NOTE = (
    "Training-target minima and maxima are observed values in the dataset, not hotel capacity; "
    "capacity is not available in this dataset."
)
LIMITATIONS: tuple[str, ...] = (
    "Same source: two Portuguese hotels, 2015-2017, one published dataset. Nothing here "
    "establishes generalisation to other hotels or production accuracy.",
    "Single-hotel models are fitted on about half the rows of the served pooled model (414 and "
    "402 vs 816); they are diagnostics, not candidates, and none is served.",
    "The two hotels' eligible training rows differ by 12 (the resort hotel's data starts 12 days "
    "later); the difference is recorded, not equalised.",
    "Not pristine: these months were seen by the Stage 6.3 backtest and by R2.2. Nothing was tuned "
    "on them.",
    "Two hotels give one same-hotel/cross-hotel contrast per evaluated hotel; there is no third.",
    "The lag ranges were chosen from a description of the training partition; groups with fewer "
    f"than {MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC} paired rows report counts only.",
    "Point forecasts only; no interval and no significance test.",
)
CLAIMS: Mapping[str, bool] = {
    "production_ready": False,
    "production_accuracy_established": False,
    "cross_hotel_generalisation_established": False,
    "generalisation_to_unseen_hotels_established": False,
    "causal_explanation_established": False,
    "model_superior_to_baseline": False,
}


def _lag_range_label(low: float, high: float | None) -> str:
    return f"[{low:g},{high:g})" if high is not None else f"[{low:g},inf)"


def protocol_settings() -> dict[str, object]:
    """Everything ``hotel_transfer_v1`` fixes, as data. Its digest is :data:`PROTOCOL_SHA256`."""
    configuration = EstimatorConfig().as_dict()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "question": (
            "at an equal amount of single-hotel training data, how much of R2.2's cross-hotel "
            "error comes from changing the hotel, and is it a systematic offset"
        ),
        "dataset": {
            "dataset_version": DATASET_VERSION,
            "feature_version": FEATURE_VERSION,
            "sha256": DATASET_SHA256,
            "path": "ml/data/processed/demand_daily_v1.csv",
        },
        "served_model": "not loaded, not scored, not refitted",
        "horizon_days": HORIZON_DAYS,
        "feature_columns": list(FEATURE_COLUMNS),
        "periods": [period.as_dict() for period in PERIODS],
        "experiments": [experiment.as_dict() for experiment in EXPERIMENTS],
        "training_rows": (
            f"the train_hotel's {TRAINING_PARTITION!r}-partition rows ordered (target_date, "
            "hotel_key); rows lacking any feature are skipped and counted (R2.2's rule, "
            "ml.frozen_holdout.cross_hotel_training_rows)"
        ),
        "evaluation_rows": (
            "the evaluate_hotel's validation and test rows (ml.frozen_holdout.period_rows), "
            "with every lag reconstructed and every calendar feature recomputed "
            "(ml.frozen_holdout.check_features)"
        ),
        "estimator": configuration,
        "estimator_sha256": configuration_checksum(configuration),
        "tuning": "none; the Stage 6.3 configuration, unchanged, for all four experiments",
        "persistence": "none; the four fitted models are discarded",
        "baseline": {
            "method": BASELINE_METHOD,
            "definition": "the evaluated row's own demand_lag_7",
        },
        "metrics": dict(METRIC_DEFINITIONS),
        "sign_convention": SIGN_CONVENTION,
        "range_rule": RANGE_RULE,
        "comparison_rule": COMPARISON_RULE,
        "breakdowns": {
            "by_hotel": "the evaluated hotel (one per experiment)",
            "by_lag_range": {
                "feature": LAG_FEATURE,
                "ranges": [_lag_range_label(low, high) for low, high in LAG_RANGES],
                "rule": "half-open [low, high); the last range has no upper bound",
            },
        },
        "minimum_observations_for_group_metric": MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC,
        "determinism": "the record is built twice in one process; the checksums must match",
        "observed_maxima_note": OBSERVED_MAXIMA_NOTE,
        "limitations": list(LIMITATIONS),
        "claims": dict(CLAIMS),
    }


def protocol_digest() -> str:
    return sha256_hex(serialise(protocol_settings()))


#: ``hotel_transfer_v1``'s checksum, committed before the first score was computed.
PROTOCOL_SHA256 = "86b3db03af2e155661c92999bcfca19357621ccd9245720b5426863ba8b28ceb"


def assert_protocol_frozen() -> None:
    computed = protocol_digest()
    if computed != PROTOCOL_SHA256:
        raise HotelTransferError(
            f"{PROTOCOL_VERSION} is not the frozen protocol: its settings hash to {computed}, "
            f"the recorded checksum is {PROTOCOL_SHA256}"
        )


# --- diagnostics --------------------------------------------------------------------------------


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def signed_errors(scored: Sequence[Scored], method: str) -> list[float]:
    """``prediction - actual`` on the paired rows, for ``"learned"`` or ``"baseline"``."""
    out: list[float] = []
    for s in scored:
        if s.learned is None or s.baseline is None:
            continue
        prediction = s.learned if method == "learned" else s.baseline
        out.append(prediction - float(s.row.target_room_nights))
    return out


def prediction_summary(
    scored: Sequence[Scored], method: str, *, train_min: float, train_max: float
) -> dict[str, object]:
    errors = signed_errors(scored, method)
    paired = [s for s in scored if s.learned is not None and s.baseline is not None]
    predictions = [(s.learned if method == "learned" else s.baseline) for s in paired]
    values = [float(p) for p in predictions if p is not None]
    below = sum(1 for p in values if p < train_min)
    above = sum(1 for p in values if p > train_max)
    return {
        "paired_observations": len(values),
        "mean_signed_error": _round(math.fsum(errors) / len(errors)) if errors else None,
        "over_predictions": sum(1 for e in errors if e > 0),
        "under_predictions": sum(1 for e in errors if e < 0),
        "exact": sum(1 for e in errors if e == 0),
        "prediction_min": _round(min(values)) if values else None,
        "prediction_max": _round(max(values)) if values else None,
        "below_training_range": below,
        "above_training_range": above,
        "fraction_outside_training_range": _round((below + above) / len(values))
        if values
        else None,
    }


def evaluate_group(
    scored: Sequence[Scored], *, train_min: float, train_max: float
) -> dict[str, object]:
    paired = sum(1 for s in scored if s.learned is not None and s.baseline is not None)
    if paired < MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC:
        return {
            "sufficient_coverage": False,
            "rows": len(scored),
            "paired_observations": paired,
            "minimum_required": MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC,
            "reason": "insufficient coverage; no metric is reported",
        }
    return {
        "sufficient_coverage": True,
        **compare(scored),
        "diagnostics": {
            "learned": prediction_summary(
                scored, "learned", train_min=train_min, train_max=train_max
            ),
            "baseline": prediction_summary(
                scored, "baseline", train_min=train_min, train_max=train_max
            ),
        },
    }


def lag_range_of(row: ProcessedRow) -> str | None:
    value = row.features.get(LAG_FEATURE)
    if value is None:
        return None
    for low, high in LAG_RANGES:
        if value >= low and (high is None or value < high):
            return _lag_range_label(low, high)
    return None


def by_lag_range(
    scored: Sequence[Scored], *, train_min: float, train_max: float
) -> dict[str, object]:
    groups: dict[str, list[Scored]] = {_lag_range_label(low, high): [] for low, high in LAG_RANGES}
    for s in scored:
        label = lag_range_of(s.row)
        if label is not None:
            groups[label].append(s)
    return {
        label: evaluate_group(group, train_min=train_min, train_max=train_max)
        for label, group in groups.items()
    }


# --- one experiment -----------------------------------------------------------------------------


def training_summary(rows: Sequence[ProcessedRow]) -> dict[str, object]:
    matrix = design_matrix(rows, FEATURE_COLUMNS)
    fitted = [rows[index] for index in matrix.used]
    targets = [row.target_room_nights for row in fitted]
    return {
        "partition_rows": len(rows),
        "fitted_rows": len(fitted),
        "skipped_rows": len(matrix.skipped),
        "first_date": min(row.target_date for row in fitted).isoformat(),
        "last_date": max(row.target_date for row in fitted).isoformat(),
        "target_min": min(targets),
        "target_max": max(targets),
        "target_range_note": OBSERVED_MAXIMA_NOTE,
    }


def run_experiment(dataset: ProcessedDataset, experiment: Experiment) -> dict[str, object]:
    training = cross_hotel_training_rows(dataset, experiment.train_hotel)
    summary = training_summary(training)
    train_min = float(summary["target_min"])  # type: ignore[arg-type]
    train_max = float(summary["target_max"])  # type: ignore[arg-type]
    model = LearnedModel.fit(training, FEATURE_COLUMNS, EstimatorConfig())
    forecaster = estimator_forecaster(model.estimator)
    periods: dict[str, object] = {}
    for period in PERIODS:
        rows = tuple(
            row
            for row in period_rows(dataset, period)
            if row.hotel_key == experiment.evaluate_hotel
        )
        if not rows:
            raise HotelTransferError(f"{experiment.evaluate_hotel} has no {period.name} rows")
        checks = check_features(dataset, rows)
        scored = score(rows, forecaster)
        overall = evaluate_group(scored, train_min=train_min, train_max=train_max)
        periods[period.name] = {
            "period": period.as_dict(),
            "coverage": coverage(rows),
            "feature_checks": checks,
            "excluded_rows": sum(1 for s in scored if s.learned is None or s.baseline is None),
            "overall": overall,
            "by_hotel": {experiment.evaluate_hotel: overall},
            "by_lag_range": by_lag_range(scored, train_min=train_min, train_max=train_max),
        }
    return {**experiment.as_dict(), "training": summary, "periods": periods}


def build_record(
    dataset: ProcessedDataset, *, expected_dataset_sha256: str = DATASET_SHA256
) -> dict[str, object]:
    """The deterministic record, without its ``generation`` block (stamped by the caller)."""
    assert_protocol_frozen()
    require_dataset(dataset, expected_sha256=expected_dataset_sha256)
    if {e.train_hotel for e in EXPERIMENTS} != set(HOTELS):
        raise HotelTransferError("the experiments do not cover both hotels")
    return {
        "protocol": {"version": PROTOCOL_VERSION, "sha256": PROTOCOL_SHA256},
        "dataset": {
            "dataset_version": DATASET_VERSION,
            "feature_version": FEATURE_VERSION,
            "sha256": dataset.sha256,
            "rows": len(dataset.rows),
        },
        "label": (
            "diagnostic single-hotel models, fitted in memory and discarded; none is the served "
            "artifact and none is a candidate"
        ),
        "sign_convention": SIGN_CONVENTION,
        "range_rule": RANGE_RULE,
        "comparison_rule": COMPARISON_RULE,
        "experiments": [run_experiment(dataset, experiment) for experiment in EXPERIMENTS],
        "limitations": list(LIMITATIONS),
        "claims": dict(CLAIMS),
    }


__all__ = [
    "EXPERIMENTS",
    "LAG_RANGES",
    "PROTOCOL_SHA256",
    "PROTOCOL_VERSION",
    "Experiment",
    "HotelTransferError",
    "assert_protocol_frozen",
    "build_record",
    "by_lag_range",
    "evaluate_group",
    "lag_range_of",
    "prediction_summary",
    "protocol_digest",
    "protocol_settings",
    "run_experiment",
    "signed_errors",
    "training_summary",
]
