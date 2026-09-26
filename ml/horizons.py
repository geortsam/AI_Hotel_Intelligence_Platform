"""Stage 7.14 -- multi-horizon demand forecasting, offline.

Three fixed-horizon direct models, one per horizon, each measured against its own baseline on
its own horizon-matched dataset. **Offline only.** Nothing here is served, nothing in the
application imports this module, and the served ``demand_baseline_v1`` is untouched: its
artifact, its canonical digest and ``APPROVED_MODEL`` are exactly what Stage 6.7 approved.

## What "horizon-matched" means here

The Stage 6.2 dataset was built one day ahead, so its rolling means and its on-the-books count
are knowable only one day before the target date, and the seven-day model could not use them
(9 of 15 contract columns). A dataset built at horizon *h* reconstructs those same Stage 6.1
features at *its own* cutoff, ``target_date - h`` -- so a model forecasting *h* days ahead may
use every one of them. The feature DEFINITIONS are Stage 6.1's, unchanged (``FEATURE_VERSION``
stays ``v1``): the horizon was always a parameter of that contract, and each dataset records it
in every row and in its manifest. What differs between the datasets is identified by content,
by SHA-256, never by name alone.

One consequence is deliberate and stated rather than hidden: a column called
``demand_rolling_mean_7`` means "the seven days ending *h* days before the target", so the same
name in two datasets is two different quantities. That is why each model is bound to one
dataset digest, and why no feature, model or number is ever carried across horizons.

## The direct strategy

Each model predicts exactly *h* days ahead: for target date *D* its inputs are cut off at
``D - h``. That is the semantics the served endpoint already has (``horizon_days`` must equal
the model's horizon), so a later serving stage would not have to change what a horizon means.
The rolling-origin backtest is leakage-safe under it without modification: training rows are
those with ``target_date <= origin`` (their targets are realised by the origin), and every
evaluation row, ``origin < D <= origin + h``, has its inputs cut off at ``D - h <= origin``.

## What is NOT claimed

* No production accuracy, no generalisation, no business value, no winner. Each horizon is
  reported beside its own baseline; horizons are not ranked, and no number is aggregated across
  them.
* No uncertainty. The models produce point forecasts; no interval is computed or implied.
* Forecasts are **raw, uncapped room nights**. The offline source carries no room inventory,
  so there is no capacity to cap against. A future serving stage must declare its own capping
  rule; nothing here decides one.
* ``on_books_room_nights_at_cutoff`` is an **offline approximation**: day-resolution (the source
  records lead time in whole days) and status-agnostic (an upper bound on confirmed demand).
  Production reconstructs it from timestamps. A model fitted on this column is therefore not a
  model that could be served against production data unchanged.
"""

from __future__ import annotations

import datetime as dt
import platform
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy
import scipy
import sklearn

from app.ml.dataset import DATASET_VERSION, FEATURE_VERSION, build_feature_specs
from ml.artifact import (
    ARTIFACT_FORMAT,
    HELD_OUT_PARTITIONS,
    PICKLE_PROTOCOL,
    PROBE_DIGITS,
    PROBE_ROWS,
    TRAINING_PARTITION,
    TRAINING_ROW_ORDER,
    assert_all_finite,
    assert_no_held_out_row,
    serialise_model,
    training_rows,
)
from ml.evaluation import (
    DEFAULT_MINIMUM_TRAIN_DAYS,
    EvaluationResult,
    RollingOriginPolicy,
    evaluate,
    per_hotel_metrics,
)
from ml.loading import ProcessedDataset
from ml.manifests import build_evaluation_manifest, content_checksum, serialise
from ml.metrics import METRIC_DEFINITIONS
from ml.models import (
    CALENDAR_FEATURES,
    LEARNED_METHOD,
    OFFLINE_UNAVAILABLE_FEATURES,
    EstimatorConfig,
    LearnedModel,
    design_matrix,
    select_model_features,
)
from ml.pipelines.offline_demand import (
    OfflineDataset,
    ParsedSource,
    build_manifest,
    build_offline_dataset,
    serialise_dataset,
    sha256_hex,
)
from ml.policy import (
    CRITERION_RATIONALE,
    AcceptanceEvidence,
    AcceptancePolicy,
    AcceptanceResult,
    evaluate_acceptance,
)
from ml.registry import build_registry_record, configuration_checksum, protocol_checksum
from ml.validation import build_validation_report, leakage_report

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

#: The frozen protocol's name. Its checksum is :data:`PROTOCOL_SHA256`, pinned below and by test.
PROTOCOL_VERSION = "multi_horizon_v1"

#: The acceptance policy for these models. ``acceptance_v1`` is Stage 6.4's and is untouched.
ACCEPTANCE_POLICY_VERSION = "acceptance_v2"

#: A new model family, so a record can never be mistaken for the served ``demand_baseline``.
MODEL_NAME = "demand_horizon"

#: Rolling-mean windows. Stage 6.1's defaults, at every horizon.
ROLLING_WINDOWS: tuple[int, ...] = (7, 14, 28)

#: The Stage 6.1 lags a horizon may keep: those at least as long as the horizon. Lag 1 is never
#: admissible here, because every horizon below is at least seven days.
CANDIDATE_LAGS: tuple[int, ...] = (7, 14, 28)

#: The seasonal period of the baselines: same weekday, whole weeks back.
WEEK_DAYS = 7

STATUS = "offline_research_candidate"


class HorizonError(Exception):
    """A horizon, dataset or record that is not the one the frozen protocol describes."""


@dataclass(frozen=True, slots=True)
class HorizonSpec:
    """One horizon, and everything the protocol fixes about it before a number is computed."""

    horizon_days: int
    dataset_name: str
    dataset_sha256: str
    model_version: str
    lag_days: tuple[int, ...]
    baseline_lag_days: int
    minimum_folds: int
    rolling_windows: tuple[int, ...] = field(default=ROLLING_WINDOWS)

    def __post_init__(self) -> None:
        if self.horizon_days < 1:
            raise HorizonError(f"horizon_days must be at least 1, got {self.horizon_days}")
        if any(lag < self.horizon_days for lag in self.lag_days):
            raise HorizonError(
                f"lags {list(self.lag_days)} include one shorter than the {self.horizon_days}-day "
                "horizon; it would read a day the forecaster has not lived through"
            )
        expected_lags = tuple(lag for lag in CANDIDATE_LAGS if lag >= self.horizon_days)
        if self.lag_days != expected_lags:
            raise HorizonError(
                f"lags must be exactly the Stage 6.1 lags that survive the horizon, "
                f"{list(expected_lags)}; got {list(self.lag_days)}"
            )
        if self.baseline_lag_days != baseline_lag_for(self.horizon_days):
            raise HorizonError(
                f"the baseline lag must be {baseline_lag_for(self.horizon_days)} (the nearest "
                f"whole week at or beyond the horizon); got {self.baseline_lag_days}"
            )
        if self.baseline_lag_days not in self.lag_days:
            raise HorizonError("the baseline must read a lag column the dataset carries")

    @property
    def baseline_method(self) -> str:
        return f"seasonal_naive_{self.baseline_lag_days}"

    @property
    def feature_order(self) -> tuple[str, ...]:
        """The Stage 6.1 contract's column order for this lag set -- what the dataset must carry."""
        return tuple(spec.name for spec in build_feature_specs(self.lag_days, self.rolling_windows))

    @property
    def dataset_path(self) -> Path:
        return REPOSITORY_ROOT / "ml" / "data" / "processed" / f"{self.dataset_name}.csv"

    @property
    def manifest_path(self) -> Path:
        return REPOSITORY_ROOT / "ml" / "manifests" / f"{self.dataset_name}.json"

    @property
    def model_directory(self) -> Path:
        return REPOSITORY_ROOT / "ml" / "models" / self.model_version

    @property
    def rolling_origin(self) -> RollingOriginPolicy:
        """Stage 6.3's origin rule at this horizon: 365 training days, windows that tile."""
        return RollingOriginPolicy(
            horizon_days=self.horizon_days,
            minimum_train_days=DEFAULT_MINIMUM_TRAIN_DAYS,
            step_days=self.horizon_days,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "horizon_days": self.horizon_days,
            "dataset_name": self.dataset_name,
            "dataset_sha256": self.dataset_sha256,
            "dataset_horizon_days": self.horizon_days,
            "model_version": self.model_version,
            "lag_days": list(self.lag_days),
            "rolling_windows": list(self.rolling_windows),
            "feature_columns": list(self.feature_order),
            "baseline_method": self.baseline_method,
            "baseline_lag_days": self.baseline_lag_days,
            "minimum_folds": self.minimum_folds,
            "rolling_origin": self.rolling_origin.as_dict(),
        }


def baseline_lag_for(horizon_days: int) -> int:
    """Same weekday, the fewest whole weeks back that the horizon still allows."""
    weeks = -(-horizon_days // WEEK_DAYS)
    return weeks * WEEK_DAYS


#: The three horizons, in the one order every record and report uses. The digests are the
#: committed datasets' SHA-256s; ``ml/pipelines/build_horizon_datasets.py`` rebuilds each twice
#: from the pinned source and refuses to write anything that differs.
HORIZONS: tuple[HorizonSpec, ...] = (
    HorizonSpec(
        horizon_days=7,
        dataset_name="demand_daily_h7_v1",
        dataset_sha256="30d74aea92830fd2afb1e0670fb3d05040934a850d06f90ece73476a09a0d52e",
        model_version="demand_h7_v1",
        lag_days=(7, 14, 28),
        baseline_lag_days=7,
        minimum_folds=40,
    ),
    HorizonSpec(
        horizon_days=14,
        dataset_name="demand_daily_h14_v1",
        dataset_sha256="56f9dfcce9d0ed685d7d24174f22a76efa180d71da8d3eb4604deea352e80fef",
        model_version="demand_h14_v1",
        lag_days=(14, 28),
        baseline_lag_days=14,
        minimum_folds=20,
    ),
    HorizonSpec(
        horizon_days=28,
        dataset_name="demand_daily_h28_v1",
        dataset_sha256="61c51f4fe068bc3d52292cff659a1d32b88ab7794317b010f745364a1a139e11",
        model_version="demand_h28_v1",
        lag_days=(28,),
        baseline_lag_days=28,
        minimum_folds=10,
    ),
)


def spec_for(horizon_days: int) -> HorizonSpec:
    for spec in HORIZONS:
        if spec.horizon_days == horizon_days:
            return spec
    raise HorizonError(
        f"{horizon_days} is not a horizon of {PROTOCOL_VERSION}; "
        f"it declares {[spec.horizon_days for spec in HORIZONS]}"
    )


# --- the frozen protocol ---------------------------------------------------------------------

CAPACITY_STATEMENT = (
    "Forecasts are raw, uncapped room nights. The offline source carries no room inventory "
    "(rooms_existing_at_cutoff is empty on every row and excluded from every model), so no "
    "capacity cap exists to apply. Any future serving of these models must declare its own "
    "capacity-capping rule; this protocol decides none."
)

ON_BOOKS_STATEMENT = (
    "on_books_room_nights_at_cutoff is an offline approximation: the source records lead time in "
    "whole days, so the cutoff has day resolution rather than timestamp resolution, and it has "
    "no booking-status history, so the count is status-agnostic -- an upper bound on confirmed "
    "demand. Production reconstructs the feature from timestamps; a model fitted on this column "
    "is not one that could be served against production data unchanged."
)

COMPARISON_RULE = (
    "Each horizon is reported side by side with its own baseline, over the observations where "
    "both produced a forecast. No winner is declared, horizons are not ranked, and no metric is "
    "aggregated across horizons. No threshold is applied to any metric. No business value and no "
    "production accuracy is claimed or implied."
)

SKIP_RULE = (
    "An observation for which a method cannot produce a forecast -- a selected feature or the "
    "baseline's lag is missing -- is skipped, counted, and excluded from that method's metrics. "
    "It is never scored as zero error, and never imputed."
)

CLAIMS: Mapping[str, bool] = {
    "production_ready": False,
    "production_accuracy_established": False,
    "cross_hotel_generalisation_established": False,
    "business_value_established": False,
    "serving_enabled": False,
    "uncertainty_quantified": False,
}


def protocol_settings() -> dict[str, object]:
    """Everything ``multi_horizon_v1`` fixes, as data. Its digest is :data:`PROTOCOL_SHA256`."""
    configuration = EstimatorConfig().as_dict()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "strategy": (
            "one fixed-horizon direct model per horizon: for target date D the inputs are cut off "
            "at D - h, and the model is fitted only on its own horizon-matched dataset"
        ),
        "horizons": [spec.as_dict() for spec in HORIZONS],
        "dataset_contract": {
            "dataset_version": DATASET_VERSION,
            "feature_version": FEATURE_VERSION,
            "cutoff": "target_date - horizon_days; a fact is known if dated on or before it",
            "source": "the pinned Stage 6.2 source, rebuilt at each horizon",
            "identity": "by SHA-256 of the processed bytes",
        },
        "estimator": configuration,
        "estimator_sha256": configuration_checksum(configuration),
        "tuning": "none; the Stage 6.3 configuration at every horizon",
        "metrics": dict(METRIC_DEFINITIONS),
        "skip_rule": SKIP_RULE,
        "capacity": CAPACITY_STATEMENT,
        "on_books": ON_BOOKS_STATEMENT,
        "uncertainty": "none; point forecasts only, and no interval is computed or implied",
        "comparison_rule": COMPARISON_RULE,
        "leakage_checks": (
            "re-run at each horizon on the folds that ran: training ends at or before the origin "
            "and before evaluation starts; every evaluation window lies within the horizon; no "
            "hotel-day is evaluated twice; every selected feature is knowable at the horizon, "
            "measured against the dataset's own cutoff; the training window never contracts; no "
            "shuffle and no random split"
        ),
        "determinism": (
            "each backtest runs twice and must agree on every prediction, every pooled metric and "
            "the record's content checksum; each dataset is built twice and must agree byte "
            "for byte"
        ),
        "acceptance_policy_version": ACCEPTANCE_POLICY_VERSION,
        "acceptance_policy_digest": acceptance_v2_digest(),
        "claims": dict(CLAIMS),
    }


def protocol_digest() -> str:
    return sha256_hex(serialise(protocol_settings()))


#: ``multi_horizon_v1``'s checksum, frozen before the first measurement. Any change to a horizon,
#: dataset digest, lag, baseline, floor, metric, statement or claim changes it, and the
#: evaluation pipeline refuses to run until it matches.
PROTOCOL_SHA256 = "d8408e188e74867a99ad5eca24540e22636c3084089a84f5b2bf9a9970a0fdaf"


def assert_protocol_frozen() -> None:
    computed = protocol_digest()
    if computed != PROTOCOL_SHA256:
        raise HorizonError(
            f"{PROTOCOL_VERSION} is not the frozen protocol: its settings hash to {computed}, "
            f"the recorded checksum is {PROTOCOL_SHA256}"
        )


# --- acceptance_v2 -----------------------------------------------------------------------------

#: Where acceptance_v2 means something different from acceptance_v1, it says so. Every other
#: criterion keeps Stage 6.4's own rationale, word for word.
ACCEPTANCE_V2_RATIONALE: Mapping[str, str] = {
    "minimum_folds": (
        "40 / 20 / 10 origins at 7 / 14 / 28 days. Windows tile, so the dataset's year of "
        "evaluable dates yields about 54 / 27 / 14 origins; each floor sits under that count and "
        "was fixed from the arithmetic before any model was fitted. Stage 6.4's 40 is kept where "
        "the data can meet it."
    ),
    "required_forecast_horizon_days": (
        "the horizon this model was declared for. A different horizon admits a different "
        "feature set and a different baseline, and produces numbers that are not comparable."
    ),
    "required_dataset_sha256": (
        "identity by content: this horizon's own horizon-matched dataset, whose digest the "
        "frozen protocol records. A dataset built at another horizon hashes differently."
    ),
    "required_model_version": (
        "the horizon's candidate. A mismatch must stop the run rather than validate another "
        "horizon's model under this one's name."
    ),
}


def acceptance_policy(spec: HorizonSpec) -> AcceptancePolicy:
    """``acceptance_v2`` at one horizon: Stage 6.4's criteria, this horizon's identity and floor.

    Built from the Stage 6.4 dataclass rather than a new one, so the evaluator that applies it is
    Stage 6.4's -- and so is the structural guarantee that it cannot see a metric: the evidence it
    reads carries counts, versions, digests and booleans only.
    """
    return AcceptancePolicy(
        version=ACCEPTANCE_POLICY_VERSION,
        minimum_folds=spec.minimum_folds,
        required_dataset_version=DATASET_VERSION,
        required_feature_version=FEATURE_VERSION,
        required_dataset_sha256=spec.dataset_sha256,
        required_forecast_horizon_days=spec.horizon_days,
        required_model_version=spec.model_version,
    )


def acceptance_v2_settings() -> dict[str, object]:
    return {
        "version": ACCEPTANCE_POLICY_VERSION,
        "per_horizon": {
            str(spec.horizon_days): acceptance_policy(spec).as_dict() for spec in HORIZONS
        },
        "rationale": {
            name: ACCEPTANCE_V2_RATIONALE.get(name, text)
            for name, text in sorted(CRITERION_RATIONALE.items())
        },
        "metric_blind": (
            "the policy reads AcceptanceEvidence only, which carries no metric value, no "
            "difference and no comparison; which method performed better cannot reach it"
        ),
    }


def acceptance_v2_digest() -> str:
    return sha256_hex(serialise(acceptance_v2_settings()))


def evaluate_acceptance_v2(spec: HorizonSpec, evidence: AcceptanceEvidence) -> AcceptanceResult:
    """Stage 6.4's evaluator, applied with this horizon's policy and acceptance_v2's rationale."""
    result = evaluate_acceptance(acceptance_policy(spec), evidence)
    checks = tuple(
        replace(check, rationale=ACCEPTANCE_V2_RATIONALE.get(check.name, check.rationale))
        for check in result.checks
    )
    return replace(result, checks=checks)


# --- datasets ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HorizonDataset:
    """One horizon-matched dataset, serialised and described, before anything is written."""

    spec: HorizonSpec
    dataset: OfflineDataset
    processed: bytes
    sha256: str
    manifest: dict[str, object]


def build_horizon_dataset(
    parsed: ParsedSource,
    spec: HorizonSpec,
    *,
    source_checksum: str,
    generated_at: dt.datetime | None = None,
) -> HorizonDataset:
    """Stage 6.2's builder at this horizon, called -- not reimplemented -- with this lag set."""
    dataset = build_offline_dataset(
        parsed,
        horizon_days=spec.horizon_days,
        lag_days=spec.lag_days,
        rolling_windows=spec.rolling_windows,
    )
    if dataset.feature_names != spec.feature_order:
        raise HorizonError(
            f"{spec.dataset_name}: built columns {list(dataset.feature_names)} are not the "
            f"contract order {list(spec.feature_order)}"
        )
    processed = serialise_dataset(dataset)
    checksum = sha256_hex(processed)
    manifest = build_manifest(
        dataset,
        source_checksum=source_checksum,
        processed_checksum=checksum,
        processed_bytes=len(processed),
        generated_at=generated_at,
    )
    block = manifest["dataset"]
    assert isinstance(block, dict)
    block["name"] = spec.dataset_name
    manifest["horizon_matching"] = {
        "stage": "7.14",
        "protocol_version": PROTOCOL_VERSION,
        "dataset_horizon_days": spec.horizon_days,
        "note": (
            f"Every feature is reconstructed at target_date - {spec.horizon_days}. Rolling "
            "means and the on-the-books count therefore describe a different cutoff from the "
            "same-named columns of demand_daily_v1, which was built one day ahead."
        ),
        "capacity": CAPACITY_STATEMENT,
        "on_books": ON_BOOKS_STATEMENT,
    }
    generation = manifest["generation"]
    assert isinstance(generation, dict)
    generation["pipeline"] = "ml/pipelines/build_horizon_datasets.py"
    return HorizonDataset(spec, dataset, processed, checksum, manifest)


def assert_dataset_identity(spec: HorizonSpec, dataset: ProcessedDataset) -> None:
    """Refuse any dataset that is not the one the frozen protocol names, before measuring."""
    if dataset.sha256 != spec.dataset_sha256:
        raise HorizonError(
            f"{dataset.path.name} hashes to {dataset.sha256}; {PROTOCOL_VERSION} requires "
            f"{spec.dataset_sha256} for the {spec.horizon_days}-day horizon"
        )
    if dataset.feature_names != spec.feature_order:
        raise HorizonError(
            f"{dataset.path.name}: columns {list(dataset.feature_names)} are not "
            f"{list(spec.feature_order)}"
        )
    horizons = {row.horizon_days for row in dataset.rows}
    if horizons != {spec.horizon_days}:
        raise HorizonError(
            f"{dataset.path.name} carries rows built at {sorted(horizons)} days, "
            f"not only at {spec.horizon_days}"
        )


# --- measurement ---------------------------------------------------------------------------------


def evaluate_horizon(spec: HorizonSpec, dataset: ProcessedDataset) -> EvaluationResult:
    """The Stage 6.3 backtest at this horizon, on this horizon's dataset, with its baseline."""
    return evaluate(
        dataset.rows,
        policy=spec.rolling_origin,
        config=EstimatorConfig(),
        feature_names=dataset.feature_names,
        dataset_horizon_days=spec.horizon_days,
        baseline_lag_days=spec.baseline_lag_days,
    )


def metrics_present(result: EvaluationResult, per_hotel: Mapping[str, object]) -> bool:
    """Every metric that should exist does. A missing one is a measurement that did not happen."""
    for measured in (result.baseline_pooled, result.learned_pooled):
        if measured.mae is None or measured.rmse is None or measured.smape is None:
            return False
    for block in per_hotel.values():
        if not isinstance(block, dict) or not block.get("sufficient_coverage"):
            continue
        for method in ("baseline", "learned"):
            scores = block.get(method)
            if not isinstance(scores, dict):
                return False
            if any(scores.get(metric) is None for metric in ("mae", "rmse", "smape")):
                return False
    return True


def evaluation_record(
    spec: HorizonSpec, result: EvaluationResult, dataset: ProcessedDataset
) -> dict[str, object]:
    """``metrics.json``: the Stage 6.3 record's shape, naming this horizon's model and baseline."""
    record = build_evaluation_manifest(
        result,
        dataset_version=DATASET_VERSION,
        feature_version=FEATURE_VERSION,
        dataset_sha256=dataset.sha256,
        dataset_rows=len(dataset.rows),
        dataset_path=dataset.path.name,
        generated_at=dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
    )
    model = record["model"]
    assert isinstance(model, dict)
    model.update(
        {
            "name": MODEL_NAME,
            "model_version": spec.model_version,
            "methods": {"baseline": spec.baseline_method, "learned": LEARNED_METHOD},
            "baseline_definition": (
                f"seasonal naive at a {spec.baseline_lag_days}-day period: the forecast for "
                "(hotel, date) is the realised room nights for that hotel "
                f"{spec.baseline_lag_days} "
                f"days earlier, read from the dataset's own demand_lag_{spec.baseline_lag_days} "
                "column -- the same weekday, the fewest whole weeks back the horizon allows. "
                "Where that observation does not exist the baseline produces no forecast; the day "
                "is counted as skipped and excluded from every metric."
            ),
            "baseline_lag_days": spec.baseline_lag_days,
            "forecast_horizon_days": spec.horizon_days,
            "predictions": "raw, uncapped room nights",
        }
    )
    dataset_block = record["dataset"]
    assert isinstance(dataset_block, dict)
    dataset_block["name"] = spec.dataset_name
    dataset_block["dataset_horizon_days"] = spec.horizon_days
    record["multi_horizon_protocol"] = {
        "protocol_version": PROTOCOL_VERSION,
        "protocol_sha256": PROTOCOL_SHA256,
    }
    record["generation"] = _generation("ml/pipelines/evaluate_horizons.py")
    return record


def _generation(pipeline: str) -> dict[str, object]:
    return {
        "note": (
            "Wall-clock and toolchain metadata only, outside the content checksum: re-running "
            "the pipeline must not look like a different result."
        ),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "pipeline": pipeline,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.system(),
            "scikit_learn": sklearn.__version__,
            "numpy": numpy.__version__,
            "scipy": scipy.__version__,
        },
    }


@dataclass(frozen=True, slots=True)
class HorizonRun:
    """One horizon measured: the backtest, its checks, the policy's verdict and the records."""

    spec: HorizonSpec
    result: EvaluationResult
    deterministic: bool
    leakage: Mapping[str, object]
    acceptance: AcceptanceResult
    metrics: dict[str, object]
    validation: dict[str, object]
    registry: dict[str, object]


def run_horizon(spec: HorizonSpec, dataset: ProcessedDataset) -> HorizonRun:
    """Measure one horizon exactly as ``multi_horizon_v1`` fixes it.

    Identity first, then the backtest twice (determinism is compared on predictions, pooled
    metrics and the record's content checksum), then the leakage re-check at this dataset's
    horizon, then acceptance_v2 on counts, versions and digests only.
    """
    assert_protocol_frozen()
    assert_dataset_identity(spec, dataset)

    first = evaluate_horizon(spec, dataset)
    second = evaluate_horizon(spec, dataset)
    metrics_first = evaluation_record(spec, first, dataset)
    metrics_second = evaluation_record(spec, second, dataset)
    deterministic = (
        first.predictions == second.predictions
        and first.baseline_pooled == second.baseline_pooled
        and first.learned_pooled == second.learned_pooled
        and content_checksum(metrics_first) == content_checksum(metrics_second)
    )

    leakage = leakage_report(first, dataset_horizon_days=spec.horizon_days)
    per_hotel = per_hotel_metrics(first.predictions)
    sufficient = sum(
        1
        for block in per_hotel.values()
        if isinstance(block, dict) and block.get("sufficient_coverage")
    )
    evidence = AcceptanceEvidence(
        paired_observations=len(first.predictions),
        folds=len(first.folds),
        baseline_skipped=first.baseline_pooled.skipped,
        learned_skipped=first.learned_pooled.skipped,
        incomplete_windows=sum(1 for fold in first.folds if not fold.fold.complete_window),
        hotels_with_sufficient_coverage=sufficient,
        dataset_version=DATASET_VERSION,
        feature_version=FEATURE_VERSION,
        dataset_sha256=dataset.sha256,
        forecast_horizon_days=first.policy.horizon_days,
        model_version=spec.model_version,
        deterministic=deterministic,
        leakage_checks_passed=bool(leakage["passed"]),
        metrics_present=metrics_present(first, per_hotel),
    )
    acceptance = evaluate_acceptance_v2(spec, evidence)

    validation = build_validation_report(first, dataset_horizon_days=spec.horizon_days)
    validation["purpose"] = (
        f"descriptive robustness analysis of the {spec.horizon_days}-day backtest under "
        f"{PROTOCOL_VERSION}; no method is ranked, no regime is labelled good or bad, no "
        "horizon is compared with another, and no model configuration was changed"
    )
    validation["acceptance"] = acceptance.as_dict()
    validation["multi_horizon_protocol"] = {
        "protocol_version": PROTOCOL_VERSION,
        "protocol_sha256": PROTOCOL_SHA256,
        "dataset_horizon_days": spec.horizon_days,
    }
    validation_sha = content_checksum(validation)
    validation["generation"] = _generation("ml/pipelines/evaluate_horizons.py")

    registry = registry_record(
        spec,
        first,
        acceptance,
        dataset,
        deterministic=deterministic,
        leakage_passed=bool(leakage["passed"]),
        evaluation_sha256=content_checksum(metrics_first),
        validation_sha256=validation_sha,
    )
    return HorizonRun(
        spec=spec,
        result=first,
        deterministic=deterministic,
        leakage=leakage,
        acceptance=acceptance,
        metrics=metrics_first,
        validation=validation,
        registry=registry,
    )


def registry_record(
    spec: HorizonSpec,
    result: EvaluationResult,
    acceptance: AcceptanceResult,
    dataset: ProcessedDataset,
    *,
    deterministic: bool,
    leakage_passed: bool,
    evaluation_sha256: str,
    validation_sha256: str,
) -> dict[str, object]:
    """Stage 6.4's registry entry, re-labelled for this horizon's model and protocol."""
    record = build_registry_record(
        result,
        acceptance,
        dataset_version=DATASET_VERSION,
        feature_version=FEATURE_VERSION,
        dataset_sha256=dataset.sha256,
        dataset_rows=len(dataset.rows),
        dataset_path=dataset.path.name,
        deterministic=deterministic,
        leakage_passed=leakage_passed,
        evaluation_record_sha256=evaluation_sha256,
        validation_sha256=validation_sha256,
    )
    model = record["model"]
    assert isinstance(model, dict)
    model.update(
        {
            "model_version": spec.model_version,
            "model_name": MODEL_NAME,
            "status": STATUS,
            "status_note": (
                f"offline research candidate at a {spec.horizon_days}-day horizon (Stage 7.14). "
                "Not served: no endpoint loads it, APPROVED_MODEL does not name it, and its "
                "payload is never committed."
            ),
            "methods": {"baseline": spec.baseline_method, "learned": LEARNED_METHOD},
            "predictions": "raw, uncapped room nights",
        }
    )
    dataset_block = record["dataset"]
    assert isinstance(dataset_block, dict)
    dataset_block["name"] = spec.dataset_name
    dataset_block["dataset_horizon_days"] = spec.horizon_days
    protocol = record["protocol"]
    assert isinstance(protocol, dict)
    # `sha256` keeps its registry_v1 meaning -- the digest of the rolling-origin settings that
    # ran, here at this horizon -- and the frozen protocol that fixed them is named beside it.
    protocol["protocol_version"] = PROTOCOL_VERSION
    protocol["protocol_sha256"] = PROTOCOL_SHA256
    protocol["sha256_note"] = (
        "sha256 is the digest of the rolling-origin settings in `settings`, at this horizon; "
        "protocol_sha256 is multi_horizon_v1's, which fixed them"
    )
    if protocol.get("sha256") != protocol_checksum(result.policy.as_dict()):
        raise HorizonError("the registry's rolling-origin digest does not describe this run")
    protocol["acceptance_policy_digest"] = acceptance_v2_digest()
    record["claims"] = {
        **CLAIMS,
        "note": (
            "Measured offline on two Portuguese hotels observed 2015-2017 by a third party; both "
            "appear in the training data at every origin. Forecasts are raw, uncapped room nights. "
            "on_books_room_nights_at_cutoff is an offline approximation. No winner, no ranking of "
            "horizons, no aggregate across horizons, no production accuracy and no business value "
            "is claimed. See docs/ml-multi-horizon.md."
        ),
        "capacity": CAPACITY_STATEMENT,
        "on_books": ON_BOOKS_STATEMENT,
    }
    record["generation"] = {
        "note": (
            "Wall-clock metadata only, and outside the content checksum: re-running the "
            "pipeline must not look like a different registry entry."
        ),
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "pipeline": "ml/pipelines/evaluate_horizons.py",
    }
    return record


# --- the artifact: one fit per horizon, a digest, no committed payload ---------------------------


@dataclass(frozen=True, slots=True)
class HorizonArtifact:
    """One horizon's model fitted once on its dataset's declared training partition."""

    spec: HorizonSpec
    model: LearnedModel
    feature_columns: tuple[str, ...]
    training_start: dt.date
    training_end: dt.date
    partition_rows: int
    fitted_rows: int
    held_out_for_missing_features: int
    training_hotels: tuple[str, ...]
    dataset_sha256: str

    @property
    def estimator(self) -> Any:
        return self.model.estimator


def train_horizon_artifact(spec: HorizonSpec, dataset: ProcessedDataset) -> HorizonArtifact:
    """Stage 6.5's training rules -- declared partition, no held-out row, finite inputs."""
    assert_dataset_identity(spec, dataset)
    selection = select_model_features(
        dataset.feature_names,
        horizon_days=spec.horizon_days,
        dataset_horizon_days=spec.horizon_days,
    )
    rows = training_rows(dataset)
    assert_no_held_out_row(rows, dataset)
    assert_all_finite(rows, selection.selected)
    matrix = design_matrix(rows, selection.selected)
    if not matrix.features:
        raise HorizonError(f"no {TRAINING_PARTITION!r} row has every selected feature")
    fitted = [rows[index] for index in matrix.used]
    model = LearnedModel.fit(rows, selection.selected, EstimatorConfig())
    return HorizonArtifact(
        spec=spec,
        model=model,
        feature_columns=selection.selected,
        training_start=min(row.target_date for row in fitted),
        training_end=max(row.target_date for row in fitted),
        partition_rows=len(rows),
        fitted_rows=len(fitted),
        held_out_for_missing_features=len(matrix.skipped),
        training_hotels=tuple(sorted({row.hotel_key for row in fitted})),
        dataset_sha256=dataset.sha256,
    )


def probe_matrix(
    feature_columns: Sequence[str], rows: int = PROBE_ROWS
) -> tuple[tuple[float, ...], ...]:
    """A fixed synthetic grid over these columns, for the reproducibility digest.

    Calendar columns follow Stage 6.5's formulas; every demand-valued column ``k`` (in column
    order) is ``100 + (index * (3 + 2k)) % 90`` -- the same rule that produced Stage 6.5's three
    lag columns. Integers only, so the digest's input carries no floating-point noise.
    """
    if rows < 1:
        raise HorizonError(f"probe grid needs at least one row, got {rows}")
    calendar = {
        "day_of_week": lambda i: i % 7,
        "day_of_month": lambda i: i % 28 + 1,
        "month": lambda i: i % 12 + 1,
        "week_of_year": lambda i: i % 52 + 1,
        "day_of_year": lambda i: (i * 5) % 365 + 1,
        "is_weekend": lambda i: 1 if i % 7 >= 5 else 0,
    }
    unknown = [name for name in feature_columns if name in OFFLINE_UNAVAILABLE_FEATURES]
    if unknown:
        raise HorizonError(f"{unknown} can never be a model input")
    grid: list[tuple[float, ...]] = []
    for index in range(rows):
        values: list[float] = []
        demand_position = 0
        for name in feature_columns:
            if name in CALENDAR_FEATURES:
                values.append(float(calendar[name](index)))
            else:
                values.append(float(100 + (index * (3 + 2 * demand_position)) % 90))
                demand_position += 1
        grid.append(tuple(values))
    return tuple(grid)


def probe_predictions(artifact: HorizonArtifact) -> tuple[str, ...]:
    raw = artifact.estimator.predict([list(v) for v in probe_matrix(artifact.feature_columns)])
    return tuple(f"{float(value):.{PROBE_DIGITS}f}" for value in raw)


def canonical_model_digest(artifact: HorizonArtifact) -> str:
    """What the model computes, as a digest -- Stage 6.5's definition, over this horizon's grid."""
    payload = {
        "canonical_digest_version": "artifact_v1",
        "feature_columns": list(artifact.feature_columns),
        "estimator_configuration": artifact.model.config.as_dict(),
        "forecast_horizon_days": artifact.spec.horizon_days,
        "training_row_count": artifact.fitted_rows,
        "training_start_date": artifact.training_start.isoformat(),
        "training_end_date": artifact.training_end.isoformat(),
        "probe_rows": PROBE_ROWS,
        "probe_digits": PROBE_DIGITS,
        "probe_predictions": list(probe_predictions(artifact)),
    }
    return sha256_hex(serialise(payload))


def artifact_record(
    artifact: HorizonArtifact,
    *,
    payload: bytes,
    validation_sha256: str,
    registry_sha256: str,
) -> dict[str, object]:
    """``artifact.json`` for one horizon. The payload is written locally and never committed."""
    configuration = artifact.model.config.as_dict()
    spec = artifact.spec
    return {
        "schema_version": "artifact_v1",
        "model": {
            "model_version": spec.model_version,
            "model_name": MODEL_NAME,
            "status": STATUS,
            "forecast_horizon_days": spec.horizon_days,
            "estimator_configuration": configuration,
            "estimator_configuration_sha256": configuration_checksum(configuration),
            "predictions": "raw, uncapped room nights",
        },
        "artifact": {
            "format": ARTIFACT_FORMAT,
            "filename": "model.pkl",
            "sha256": sha256_hex(payload),
            "bytes": len(payload),
            "committed": False,
            "committed_note": (
                "The payload is generated, not committed: model weights are never committed and "
                "a pickle is arbitrary code on load. Nothing loads it: this model is not served."
            ),
            "canonical_model_digest": canonical_model_digest(artifact),
            "canonical_digest_note": (
                "sha256 over the feature columns, estimator configuration, training extent and the "
                "model's predictions on a fixed synthetic probe grid over this model's own "
                "columns, "
                f"each formatted to {PROBE_DIGITS} decimal places."
            ),
            "probe_rows": PROBE_ROWS,
            "probe_digits": PROBE_DIGITS,
            "probe_predictions": list(probe_predictions(artifact)),
        },
        "dataset": {
            "name": spec.dataset_name,
            "dataset_version": DATASET_VERSION,
            "feature_version": FEATURE_VERSION,
            "dataset_sha256": artifact.dataset_sha256,
            "dataset_horizon_days": spec.horizon_days,
            "feature_columns": list(artifact.feature_columns),
        },
        "training": {
            "partition": TRAINING_PARTITION,
            "partition_rows": artifact.partition_rows,
            "training_row_count": artifact.fitted_rows,
            "held_out_for_missing_features": artifact.held_out_for_missing_features,
            "training_start_date": artifact.training_start.isoformat(),
            "training_end_date": artifact.training_end.isoformat(),
            "training_hotels": list(artifact.training_hotels),
            "held_out_partitions": list(HELD_OUT_PARTITIONS),
            "row_order": list(TRAINING_ROW_ORDER),
        },
        "protocol": {
            "protocol_version": PROTOCOL_VERSION,
            "protocol_sha256": PROTOCOL_SHA256,
            "acceptance_policy_version": ACCEPTANCE_POLICY_VERSION,
            "acceptance_policy_digest": acceptance_v2_digest(),
            "validation_record_sha256": validation_sha256,
            "registry_record_sha256": registry_sha256,
        },
        "claims": {**CLAIMS},
        "generation": {
            "note": "Wall-clock and toolchain metadata, outside the content checksum.",
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            "created_by": "ml/pipelines/evaluate_horizons.py",
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.system(),
                "scikit_learn": sklearn.__version__,
                "numpy": numpy.__version__,
                "scipy": scipy.__version__,
                "pickle_protocol": PICKLE_PROTOCOL,
            },
        },
    }


def model_payload(artifact: HorizonArtifact) -> bytes:
    return serialise_model(artifact.estimator)


# --- the side-by-side report ------------------------------------------------------------------


def side_by_side(registry: Mapping[str, object]) -> dict[str, object]:
    """One horizon's pooled figures beside its own baseline, read from its registry entry.

    One horizon per call, by design: there is no function here that takes several horizons and
    returns a combined figure.
    """
    metrics = registry["metrics"]
    assert isinstance(metrics, dict)
    pooled = metrics["pooled"]
    assert isinstance(pooled, dict)
    model = registry["model"]
    assert isinstance(model, dict)
    comparison_block = pooled["comparison"]
    assert isinstance(comparison_block, dict)
    return {
        "forecast_horizon_days": model["forecast_horizon_days"],
        "model_version": model["model_version"],
        "baseline_method": model["methods"]["baseline"]
        if isinstance(model["methods"], dict)
        else None,
        "paired_observations": comparison_block["paired_observations"],
        "baseline": comparison_block["baseline"],
        "learned": comparison_block["learned"],
    }
