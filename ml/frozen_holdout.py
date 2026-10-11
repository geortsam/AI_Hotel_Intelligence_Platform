"""R2.2: the frozen served model, scored on the partitions it was never fitted on.

The question this module answers is narrower than "how good is the model", and it is worth
stating exactly. ``demand_baseline_v1`` -- the artifact the API serves, identified by its
canonical model digest -- was fitted on the ``train`` partition of the Stage 6.2 dataset
(816 rows, 2015-09-23 .. 2016-11-09). The ``validation`` and ``test`` partitions
(2016-11-10 .. 2017-08-31) never reached ``fit``. So the frozen artifact can be scored on them
**without refitting anything**, beside the ``seasonal_naive_7`` baseline, on identical dates.

**Not a pristine hold-out.** The Stage 6.3 rolling-origin backtest refitted the *method* at
weekly origins across these same months and its results are published. No configuration was
tuned on them -- the estimator settings are Stage 6.3's defaults and acceptance was metric-blind
-- but they have been seen. The results here are a measurement of one frozen model on months it
was not fitted on; they are not an unbiased estimate of generalisation, and every record says so.

A second, separately labelled experiment refits the **same configuration** on one hotel's
training partition and scores it on the *other* hotel's held-out rows, in both directions. It
asks whether what the method learns transfers between the two hotels. Its models are never
written anywhere and are not the served artifact.

## The protocol is fixed before anything is scored

Everything the evaluation decides is in :func:`protocol_settings`, and its SHA-256 is pinned as
:data:`PROTOCOL_SHA256`. The pipeline refuses to run if they differ. The checksum was committed
before the first score was computed (see ``docs/ml-frozen-holdout-evaluation.md``).

Pure: datasets and fitted estimators in, a JSON-ready record out. No file is written here and
nothing reads the wall clock.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ml.artifact import TRAINING_PARTITION, LoadedArtifact, probe_predictions
from ml.inference import FeatureVector, predict_demand
from ml.loading import ProcessedDataset, ProcessedRow
from ml.manifests import serialise
from ml.metrics import METRIC_DEFINITIONS, MetricSet, difference, metric_set
from ml.models import (
    BASELINE_METHOD,
    MODEL_VERSION,
    SEASONAL_NAIVE_LAG_DAYS,
    EstimatorConfig,
    LearnedModel,
    design_matrix,
    feature_lead_days,
    seasonal_naive_predictions,
    select_model_features,
)
from ml.pipelines.offline_demand import sha256_hex
from ml.registry import configuration_checksum

PROTOCOL_VERSION = "frozen_holdout_v1"

#: The served artifact, by the identity that survives a rebuild: what the model computes.
FROZEN_MODEL_VERSION = MODEL_VERSION
FROZEN_CANONICAL_MODEL_DIGEST = "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"
#: The payload this repository's Stage 6.5 build wrote. Machine-specific (it varies with the
#: OpenMP thread count); checked when the local payload is evaluated, never required of a rebuild.
FROZEN_PAYLOAD_SHA256 = "bdfeb3b81b05a1884dba6b3cc687174204fe7dd13ebb26a0c65161e710fb140b"

DATASET_VERSION = "v1"
FEATURE_VERSION = "v1"
DATASET_SHA256 = "904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d"

HORIZON_DAYS = 7
FEATURE_COLUMNS: tuple[str, ...] = (
    "day_of_week",
    "day_of_month",
    "month",
    "week_of_year",
    "day_of_year",
    "is_weekend",
    "demand_lag_7",
    "demand_lag_14",
    "demand_lag_28",
)
CALENDAR_COLUMNS: tuple[str, ...] = FEATURE_COLUMNS[:6]
LAG_COLUMNS: tuple[str, ...] = FEATURE_COLUMNS[6:]

HOTELS: tuple[str, ...] = ("city_hotel", "resort_hotel")
#: Targets with fewer actual room nights than this form the "weak range" breakdown: the served
#: model returns about 165.83 for any history at or below about forty room nights a night.
WEAK_RANGE_BELOW = 40
#: A group with fewer paired observations reports its counts and no metric (Stage 6.3's rule).
MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC = 30
CROSS_HOTEL_DIRECTIONS: tuple[tuple[str, str], ...] = (
    ("city_hotel", "resort_hotel"),
    ("resort_hotel", "city_hotel"),
)


class FrozenHoldoutError(Exception):
    """The data, the model or the protocol is not the one this evaluation was fixed for."""


@dataclass(frozen=True, slots=True)
class Period:
    """A held-out partition, by the partition column AND by its declared date range."""

    name: str
    start: dt.date
    end: dt.date

    def as_dict(self) -> dict[str, object]:
        return {
            "partition": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


PERIODS: tuple[Period, ...] = (
    Period("validation", dt.date(2016, 11, 10), dt.date(2017, 4, 5)),
    Period("test", dt.date(2017, 4, 6), dt.date(2017, 8, 31)),
)

COMPARISON_RULE = (
    "Learned model and baseline are compared on the paired set: the held-out rows where BOTH "
    "produced a forecast. On each paired row the learned model 'wins' when |actual - learned| < "
    "|actual - baseline|, 'loses' when it is greater, and the row is a 'tie' when they are equal. "
    "win_proportion = wins / paired. Differences are learned - baseline (negative = smaller "
    "learned error). No threshold is applied, no winner is declared, and no number is a verdict."
)
SKIP_RULE = (
    "A row on which a method cannot forecast -- a selected feature, or the baseline's "
    "demand_lag_7, is absent -- is skipped for that method, counted, and excluded from its "
    "metrics; it is never scored as zero error and never imputed. Paired metrics use only rows "
    "both methods forecast."
)
GROUP_RULE = (
    "Breakdowns by hotel, by calendar month of the target date, and by the weak range (actual < "
    f"{WEAK_RANGE_BELOW} room nights) use the same paired rule. A group with fewer than "
    f"{MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC} paired rows reports its counts and no metric."
)
FAILURE_CONDITIONS: tuple[str, ...] = (
    "the dataset's SHA-256 differs from the pinned value, or from its manifest",
    "the model's probe predictions or canonical digest differ from the frozen artifact's",
    "the local payload's SHA-256 differs from the pinned value, before or after the run",
    "the selected feature columns differ from the pinned list",
    "any evaluated row lies outside its period's partition or date range, or a (hotel, date) "
    "appears twice",
    "any lag on an evaluated row differs from the realised target it names, or cannot be checked",
    "any calendar feature on an evaluated row differs from its value recomputed from the date",
    "any forecast is not a finite number",
    "a cross-hotel training set contains a held-out row or a row of the evaluated hotel",
    "two runs in one process produce different records",
)
LIMITATIONS: tuple[str, ...] = (
    "Same source: the held-out months come from the same two Portuguese hotels (2015-2017) whose "
    "earlier months the model was fitted on. Nothing here establishes generalisation to other "
    "hotels or production accuracy.",
    "Not pristine: the Stage 6.3 backtest refitted the same method across these months and its "
    "results are published. No setting was tuned on them, but they are not unseen.",
    "Seasonal mismatch: validation is November-April and test is April-August; the two are "
    "reported separately and are not interchangeable.",
    "Two hotels: the cross-hotel experiment has two directions and no third hotel; it measures "
    "transfer between these two, not to hotels in general.",
    "Weak range untested: no validation or test target is below forty room nights (0 of 590 "
    "rows; counted from the targets before any scoring), so the served model's known weak range "
    "(about 165.83 for small histories) is not exercised here and its breakdown reports no metric.",
    "Duplicates: 31,994 duplicate source rows retained by Stage 6.2 inflate demand consistently; "
    "every figure inherits that uncertainty.",
    "Point forecasts only: no interval, no uncertainty quantification, no capacity cap.",
)
CLAIMS: Mapping[str, bool] = {
    "production_ready": False,
    "production_accuracy_established": False,
    "cross_hotel_generalisation_established": False,
    "business_value_established": False,
    "unbiased_generalisation_estimate": False,
    "model_superior_to_baseline": False,
}


def protocol_settings() -> dict[str, object]:
    """Everything ``frozen_holdout_v1`` fixes, as data. Its digest is :data:`PROTOCOL_SHA256`."""
    configuration = EstimatorConfig().as_dict()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "question": (
            "how the frozen served artifact, unrefitted, forecasts the two partitions it was "
            "never fitted on, beside seasonal_naive_7 on identical rows; and, separately, how "
            "the same configuration transfers between the two hotels"
        ),
        "frozen_model": {
            "model_version": FROZEN_MODEL_VERSION,
            "canonical_model_digest": FROZEN_CANONICAL_MODEL_DIGEST,
            "local_payload_sha256": FROZEN_PAYLOAD_SHA256,
            "identity_rule": (
                "the model's probe predictions must equal the recorded ones and its canonical "
                "digest must equal the pinned value; a local payload must also match its pinned "
                "SHA-256 before and after the run. Never refitted, never written."
            ),
            "scoring_path": (
                "ml.artifact.load_artifact (payload digest verified before unpickling), then "
                "ml.inference.predict_demand -- the serving path -- once per period, on the rows "
                "with every feature present"
            ),
        },
        "dataset": {
            "dataset_version": DATASET_VERSION,
            "feature_version": FEATURE_VERSION,
            "sha256": DATASET_SHA256,
            "path": "ml/data/processed/demand_daily_v1.csv",
        },
        "target": (
            "target_room_nights: occupied room nights for one hotel on one calendar date "
            "(checked-out bookings expanded over [arrival, arrival + nights)); Stage 6.2 section 5"
        ),
        "horizon_days": HORIZON_DAYS,
        "cutoff": (
            "a forecast for target date D uses only facts dated on or before D - 7. Calendar "
            "features derive from D itself; demand_lag_k is the realised target on D - k, and only "
            "k >= 7 is admissible (ml.models.feature_lead_days)"
        ),
        "feature_columns": list(FEATURE_COLUMNS),
        "periods": [period.as_dict() for period in PERIODS],
        "row_order": "(target_date, hotel_key); no shuffle and no random split anywhere",
        "baseline": {
            "method": BASELINE_METHOD,
            "definition": f"the row's own demand_lag_{SEASONAL_NAIVE_LAG_DAYS}",
        },
        "metrics": dict(METRIC_DEFINITIONS),
        "comparison_rule": COMPARISON_RULE,
        "skip_rule": SKIP_RULE,
        "group_rule": GROUP_RULE,
        "breakdowns": ["hotel", "calendar_month", f"actual_below_{WEAK_RANGE_BELOW}"],
        "weak_range_below": WEAK_RANGE_BELOW,
        "minimum_observations_for_group_metric": MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC,
        "cross_hotel": {
            "directions": [
                {"train_hotel": source, "evaluate_hotel": target}
                for source, target in CROSS_HOTEL_DIRECTIONS
            ],
            "training_rows": (
                f"the train_hotel's rows in the {TRAINING_PARTITION!r} partition with every "
                "selected feature present, ordered (target_date, hotel_key)"
            ),
            "evaluation_rows": "the evaluate_hotel's validation and test rows, reported per period",
            "estimator": configuration,
            "estimator_sha256": configuration_checksum(configuration),
            "tuning": "none; the Stage 6.3 configuration, unchanged",
            "persistence": "none; the fitted models are discarded and never served",
        },
        "leakage_checks": [
            "every lag on an evaluated row equals the realised target on its source date",
            "every lag's source date is on or before the row's cutoff (D - 7)",
            "every calendar feature equals its value recomputed from the target date",
            "no evaluated row is in the train partition or outside its period's date range",
            "cross-hotel training rows are train-partition rows of the other hotel only",
        ],
        "determinism": (
            "the record is built twice in one process and the two content checksums must match"
        ),
        "failure_conditions": list(FAILURE_CONDITIONS),
        "limitations": list(LIMITATIONS),
        "claims": dict(CLAIMS),
    }


def protocol_digest() -> str:
    return sha256_hex(serialise(protocol_settings()))


#: ``frozen_holdout_v1``'s checksum, committed before the first score was computed.
PROTOCOL_SHA256 = "27e6c8a4873c0aa5d99516e15d2df6ed663fc3ccd538d8b2dad487f9516f0bd5"


def assert_protocol_frozen() -> None:
    computed = protocol_digest()
    if computed != PROTOCOL_SHA256:
        raise FrozenHoldoutError(
            f"{PROTOCOL_VERSION} is not the frozen protocol: its settings hash to {computed}, "
            f"the recorded checksum is {PROTOCOL_SHA256}"
        )


# --- inputs ----------------------------------------------------------------------------------


def require_dataset(dataset: ProcessedDataset, *, expected_sha256: str = DATASET_SHA256) -> None:
    if dataset.sha256 != expected_sha256:
        raise FrozenHoldoutError(
            f"the dataset hashes to {dataset.sha256}, the protocol fixes {expected_sha256}"
        )
    selected = select_model_features(dataset.feature_names, horizon_days=HORIZON_DAYS).selected
    if selected != FEATURE_COLUMNS:
        raise FrozenHoldoutError(
            f"the admissible features are {list(selected)}, the protocol fixes "
            f"{list(FEATURE_COLUMNS)}"
        )
    keys = [(row.hotel_key, row.target_date) for row in dataset.rows]
    if len(keys) != len(set(keys)):
        raise FrozenHoldoutError("a (hotel, date) appears more than once in the dataset")


def _block(record: Mapping[str, object], name: str) -> Mapping[str, object]:
    block = record.get(name)
    if not isinstance(block, Mapping):
        raise FrozenHoldoutError(f"the artifact record has no {name!r} block")
    return block


def measured_canonical_digest(estimator: Any, artifact_record: Mapping[str, object]) -> str:
    """The canonical model digest of *estimator*, measured from its own probe predictions.

    The other inputs -- columns, configuration, training extent -- are read from the artifact
    record, laid out exactly as :func:`ml.artifact.canonical_model_digest` lays them out. Only
    the probe predictions come from the live model, and they are what changes if the model does.
    """
    artifact = _block(artifact_record, "artifact")
    model = _block(artifact_record, "model")
    dataset = _block(artifact_record, "dataset")
    training = _block(artifact_record, "training")
    payload = {
        "canonical_digest_version": artifact_record.get("schema_version"),
        "feature_columns": dataset.get("feature_columns"),
        "estimator_configuration": model.get("estimator_configuration"),
        "forecast_horizon_days": model.get("forecast_horizon_days"),
        "training_row_count": training.get("training_row_count"),
        "training_start_date": training.get("training_start_date"),
        "training_end_date": training.get("training_end_date"),
        "probe_rows": artifact.get("probe_rows"),
        "probe_digits": artifact.get("probe_digits"),
        "probe_predictions": list(probe_predictions(estimator)),
    }
    return sha256_hex(serialise(payload))


def require_frozen_model(estimator: Any, artifact_record: Mapping[str, object]) -> str:
    """The model must compute what the frozen artifact computes -- measured, not assumed.

    Returns the measured canonical digest, which equals :data:`FROZEN_CANONICAL_MODEL_DIGEST`.
    """
    recorded = _block(artifact_record, "artifact").get("canonical_model_digest")
    if recorded != FROZEN_CANONICAL_MODEL_DIGEST:
        raise FrozenHoldoutError(
            f"the artifact record names canonical digest {recorded}, the protocol fixes "
            f"{FROZEN_CANONICAL_MODEL_DIGEST}"
        )
    measured = measured_canonical_digest(estimator, artifact_record)
    if measured != FROZEN_CANONICAL_MODEL_DIGEST:
        raise FrozenHoldoutError(
            f"the model's canonical digest measures {measured}, the protocol fixes "
            f"{FROZEN_CANONICAL_MODEL_DIGEST}; it is not the served model"
        )
    return measured


def period_rows(dataset: ProcessedDataset, period: Period) -> tuple[ProcessedRow, ...]:
    """A held-out partition's rows, ordered, with the date range enforced as well."""
    rows = tuple(
        sorted(
            (row for row in dataset.rows if row.partition == period.name),
            key=lambda row: (row.target_date, row.hotel_key),
        )
    )
    if not rows:
        raise FrozenHoldoutError(f"the dataset has no {period.name!r} rows")
    outside = [row.target_date for row in rows if not period.start <= row.target_date <= period.end]
    if outside:
        raise FrozenHoldoutError(
            f"{len(outside)} {period.name} row(s) fall outside {period.start}..{period.end}"
        )
    return rows


# --- leakage checks --------------------------------------------------------------------------


def _calendar(date: dt.date) -> dict[str, float]:
    iso = date.isocalendar()
    return {
        "day_of_week": float(date.weekday()),
        "day_of_month": float(date.day),
        "month": float(date.month),
        "week_of_year": float(iso.week),
        "day_of_year": float(date.timetuple().tm_yday),
        "is_weekend": 1.0 if date.weekday() >= 5 else 0.0,
    }


def check_features(dataset: ProcessedDataset, rows: Sequence[ProcessedRow]) -> dict[str, int]:
    """Every feature on *rows* must be knowable at D - 7 and equal to what it claims to be.

    A lag is recomputed from the dataset's own targets -- an independent reconstruction, not a
    second read of the same column -- and its source date is checked against the cutoff. A
    calendar feature is recomputed from the date. Any disagreement stops the evaluation.
    """
    realised = {(row.hotel_key, row.target_date): row.target_room_nights for row in dataset.rows}
    lags_checked = 0
    calendar_checked = 0
    for row in rows:
        cutoff = row.target_date - dt.timedelta(days=HORIZON_DAYS)
        for name in LAG_COLUMNS:
            lead = feature_lead_days(name)
            if lead is None or lead < HORIZON_DAYS:
                raise FrozenHoldoutError(f"{name} is not knowable {HORIZON_DAYS} days ahead")
            source = row.target_date - dt.timedelta(days=lead)
            if source > cutoff:
                raise FrozenHoldoutError(f"{name} on {row.target_date} reads past the cutoff")
            value = row.features.get(name)
            if value is None:
                continue
            expected = realised.get((row.hotel_key, source))
            if expected is None:
                raise FrozenHoldoutError(
                    f"{name} on {row.target_date} ({row.hotel_key}) names {source}, which the "
                    "dataset does not contain; it cannot be checked"
                )
            if value != float(expected):
                raise FrozenHoldoutError(
                    f"{name} on {row.target_date} ({row.hotel_key}) is {value}, but the realised "
                    f"target on {source} is {expected}"
                )
            lags_checked += 1
        for name, expected_value in _calendar(row.target_date).items():
            if row.features.get(name) != expected_value:
                raise FrozenHoldoutError(
                    f"{name} on {row.target_date} is {row.features.get(name)}, expected "
                    f"{expected_value}"
                )
            calendar_checked += 1
    return {"rows": len(rows), "lags_checked": lags_checked, "calendar_checked": calendar_checked}


# --- scoring ---------------------------------------------------------------------------------

Forecaster = Callable[[Sequence[ProcessedRow]], tuple[float | None, ...]]


def estimator_forecaster(estimator: Any) -> Forecaster:
    """Batch predictions from a fitted estimator over :data:`FEATURE_COLUMNS`, ``None`` where a
    feature is absent. One ``predict`` call per batch, as every forecaster in ``ml/`` does."""

    def forecast(rows: Sequence[ProcessedRow]) -> tuple[float | None, ...]:
        matrix = design_matrix(rows, FEATURE_COLUMNS)
        out: list[float | None] = [None] * len(rows)
        if not matrix.features:
            return tuple(out)
        raw = estimator.predict([list(values) for values in matrix.features])
        for position, index in enumerate(matrix.used):
            out[index] = float(raw[position])
        return tuple(out)

    return forecast


def served_forecaster(artifact: LoadedArtifact) -> Forecaster:
    """Batch predictions through ``ml.inference.predict_demand``: the path the API serves, with
    its column-order, horizon and finiteness checks. ``None`` where a feature is absent -- the
    serving path refuses an incomplete row, so such a row is skipped here, never filled."""
    if tuple(artifact.feature_columns) != FEATURE_COLUMNS:
        raise FrozenHoldoutError(
            f"the artifact's columns are {list(artifact.feature_columns)}, the protocol fixes "
            f"{list(FEATURE_COLUMNS)}"
        )
    if artifact.forecast_horizon_days != HORIZON_DAYS:
        raise FrozenHoldoutError(
            f"the artifact forecasts {artifact.forecast_horizon_days} days ahead, the protocol "
            f"fixes {HORIZON_DAYS}"
        )

    def forecast(rows: Sequence[ProcessedRow]) -> tuple[float | None, ...]:
        matrix = design_matrix(rows, FEATURE_COLUMNS)
        vectors = [
            FeatureVector(
                hotel_public_id=rows[index].hotel_public_id,
                target_date=rows[index].target_date,
                forecast_horizon_days=HORIZON_DAYS,
                features=dict(zip(FEATURE_COLUMNS, values, strict=True)),
            )
            for index, values in zip(matrix.used, matrix.features, strict=True)
        ]
        out: list[float | None] = [None] * len(rows)
        for index, prediction in zip(matrix.used, predict_demand(artifact, vectors), strict=True):
            out[index] = prediction.prediction
        return tuple(out)

    return forecast


@dataclass(frozen=True, slots=True)
class Scored:
    row: ProcessedRow
    learned: float | None
    baseline: float | None


def score(rows: Sequence[ProcessedRow], forecaster: Forecaster) -> tuple[Scored, ...]:
    learned = forecaster(rows)
    baseline = seasonal_naive_predictions(rows, horizon_days=HORIZON_DAYS)
    for value in learned:
        if value is not None and not math.isfinite(value):
            raise FrozenHoldoutError(f"a forecast is not a finite number: {value!r}")
    return tuple(Scored(row, a, b) for row, a, b in zip(rows, learned, baseline, strict=True))


def _metrics(pairs: Sequence[tuple[float, float]]) -> MetricSet:
    return metric_set([actual for actual, _ in pairs], [forecast for _, forecast in pairs])


def compare(scored: Sequence[Scored]) -> dict[str, object]:
    """Each method on everything it forecast, and both on the rows they share."""
    learned_rows = [
        (float(s.row.target_room_nights), s.learned) for s in scored if s.learned is not None
    ]
    baseline_rows = [
        (float(s.row.target_room_nights), s.baseline) for s in scored if s.baseline is not None
    ]
    paired = [s for s in scored if s.learned is not None and s.baseline is not None]
    wins = losses = ties = 0
    learned_pairs: list[tuple[float, float]] = []
    baseline_pairs: list[tuple[float, float]] = []
    for s in paired:
        assert s.learned is not None and s.baseline is not None
        actual = float(s.row.target_room_nights)
        learned_error = abs(actual - s.learned)
        baseline_error = abs(actual - s.baseline)
        if learned_error < baseline_error:
            wins += 1
        elif learned_error > baseline_error:
            losses += 1
        else:
            ties += 1
        learned_pairs.append((actual, s.learned))
        baseline_pairs.append((actual, s.baseline))
    learned_paired = _metrics(learned_pairs)
    baseline_paired = _metrics(baseline_pairs)
    return {
        "rows": len(scored),
        "learned": metric_set(
            [a for a, _ in learned_rows],
            [f for _, f in learned_rows],
            skipped=len(scored) - len(learned_rows),
        ).as_dict(),
        "baseline": metric_set(
            [a for a, _ in baseline_rows],
            [f for _, f in baseline_rows],
            skipped=len(scored) - len(baseline_rows),
        ).as_dict(),
        "paired": {
            "observations": len(paired),
            "learned": learned_paired.as_dict(),
            "baseline": baseline_paired.as_dict(),
            "difference_learned_minus_baseline": {
                name: _round(
                    difference(getattr(learned_paired, name), getattr(baseline_paired, name))
                )
                for name in ("mae", "rmse", "smape")
            },
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "win_proportion": _round(wins / len(paired)) if paired else None,
        },
    }


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def _group(scored: Sequence[Scored]) -> dict[str, object]:
    paired = sum(1 for s in scored if s.learned is not None and s.baseline is not None)
    if paired < MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC:
        return {
            "sufficient_coverage": False,
            "rows": len(scored),
            "paired_observations": paired,
            "minimum_required": MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC,
            "reason": "insufficient coverage; no metric is reported",
        }
    return {"sufficient_coverage": True, **compare(scored)}


def breakdowns(scored: Sequence[Scored]) -> dict[str, object]:
    by_hotel: dict[str, list[Scored]] = {}
    by_month: dict[str, list[Scored]] = {}
    for s in scored:
        by_hotel.setdefault(s.row.hotel_key, []).append(s)
        by_month.setdefault(s.row.target_date.strftime("%Y-%m"), []).append(s)
    weak = [s for s in scored if s.row.target_room_nights < WEAK_RANGE_BELOW]
    return {
        "by_hotel": {key: _group(group) for key, group in sorted(by_hotel.items())},
        "by_month": {key: _group(group) for key, group in sorted(by_month.items())},
        f"actual_below_{WEAK_RANGE_BELOW}": _group(weak),
    }


def coverage(rows: Sequence[ProcessedRow]) -> dict[str, object]:
    dates = sorted({row.target_date for row in rows})
    return {
        "rows": len(rows),
        "dates": len(dates),
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
        "hotels": sorted({row.hotel_key for row in rows}),
    }


def period_result(
    dataset: ProcessedDataset, period: Period, forecaster: Forecaster, *, hotel: str | None = None
) -> dict[str, object]:
    rows = period_rows(dataset, period)
    if hotel is not None:
        rows = tuple(row for row in rows if row.hotel_key == hotel)
    checks = check_features(dataset, rows)
    scored = score(rows, forecaster)
    return {
        "period": period.as_dict(),
        "coverage": coverage(rows),
        "feature_checks": checks,
        "overall": compare(scored),
        "breakdowns": breakdowns(scored),
    }


# --- the cross-hotel experiment --------------------------------------------------------------


def cross_hotel_training_rows(dataset: ProcessedDataset, hotel: str) -> tuple[ProcessedRow, ...]:
    rows = tuple(
        sorted(
            (
                row
                for row in dataset.rows
                if row.hotel_key == hotel and row.partition == TRAINING_PARTITION
            ),
            key=lambda row: (row.target_date, row.hotel_key),
        )
    )
    if not rows:
        raise FrozenHoldoutError(f"{hotel} has no {TRAINING_PARTITION!r} rows")
    if any(row.partition != TRAINING_PARTITION or row.hotel_key != hotel for row in rows):
        raise FrozenHoldoutError(
            "a cross-hotel training set reaches outside its hotel's train rows"
        )
    earliest_held_out = min(period.start for period in PERIODS)
    if max(row.target_date for row in rows) >= earliest_held_out:
        raise FrozenHoldoutError("a cross-hotel training set reaches into the held-out months")
    return rows


def cross_hotel_direction(
    dataset: ProcessedDataset, train_hotel: str, evaluate_hotel: str
) -> dict[str, object]:
    if train_hotel == evaluate_hotel:
        raise FrozenHoldoutError("a cross-hotel direction must use two different hotels")
    training = cross_hotel_training_rows(dataset, train_hotel)
    model = LearnedModel.fit(training, FEATURE_COLUMNS, EstimatorConfig())
    fitted = [training[index] for index in design_matrix(training, FEATURE_COLUMNS).used]
    forecaster = estimator_forecaster(model.estimator)
    return {
        "train_hotel": train_hotel,
        "evaluate_hotel": evaluate_hotel,
        "training": {
            "partition_rows": len(training),
            "fitted_rows": len(fitted),
            "first_date": min(row.target_date for row in fitted).isoformat(),
            "last_date": max(row.target_date for row in fitted).isoformat(),
        },
        "periods": {
            period.name: period_result(dataset, period, forecaster, hotel=evaluate_hotel)
            for period in PERIODS
        },
    }


# --- the record --------------------------------------------------------------------------------


def build_record(
    dataset: ProcessedDataset,
    frozen: Forecaster,
    *,
    model_identity: Mapping[str, object],
    expected_dataset_sha256: str = DATASET_SHA256,
) -> dict[str, object]:
    """The complete, deterministic evaluation record, without its ``generation`` block.

    *frozen* forecasts with the served model and nothing else; the caller has already proved
    its identity (:func:`require_frozen_model`). The content checksum is stamped by the caller,
    into ``generation``, as every other record in ``ml/`` is. ``expected_dataset_sha256`` exists
    for synthetic test panels; the pipeline never passes it.
    """
    assert_protocol_frozen()
    require_dataset(dataset, expected_sha256=expected_dataset_sha256)
    record: dict[str, object] = {
        "protocol": {"version": PROTOCOL_VERSION, "sha256": PROTOCOL_SHA256},
        "dataset": {
            "dataset_version": DATASET_VERSION,
            "feature_version": FEATURE_VERSION,
            "sha256": dataset.sha256,
            "rows": len(dataset.rows),
        },
        "frozen_model": dict(model_identity),
        "frozen_holdout": {
            "label": (
                "the served artifact, unrefitted, on the partitions it was never fitted on; not a "
                "pristine hold-out (see limitations)"
            ),
            "periods": {period.name: period_result(dataset, period, frozen) for period in PERIODS},
        },
        "cross_hotel": {
            "label": (
                "a separate experiment: the same configuration refitted on one hotel's train "
                "partition, scored on the other hotel; these models are not the served artifact"
            ),
            "directions": [
                cross_hotel_direction(dataset, source, target)
                for source, target in CROSS_HOTEL_DIRECTIONS
            ],
        },
        "comparison_rule": COMPARISON_RULE,
        "limitations": list(LIMITATIONS),
        "claims": dict(CLAIMS),
    }
    return record


__all__ = [
    "CROSS_HOTEL_DIRECTIONS",
    "DATASET_SHA256",
    "FEATURE_COLUMNS",
    "FROZEN_CANONICAL_MODEL_DIGEST",
    "FROZEN_PAYLOAD_SHA256",
    "PERIODS",
    "PROTOCOL_SHA256",
    "PROTOCOL_VERSION",
    "FrozenHoldoutError",
    "Period",
    "assert_protocol_frozen",
    "build_record",
    "check_features",
    "compare",
    "cross_hotel_direction",
    "estimator_forecaster",
    "measured_canonical_digest",
    "period_rows",
    "protocol_digest",
    "protocol_settings",
    "require_dataset",
    "require_frozen_model",
    "score",
    "served_forecaster",
]
