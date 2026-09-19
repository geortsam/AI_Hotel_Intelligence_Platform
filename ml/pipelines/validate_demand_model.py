"""Validate the Stage 6.3 backtest and write the registry entry.

    python -m ml.pipelines.validate_demand_model

Re-runs the Stage 6.3 evaluation **unchanged**, requires it to reproduce the same 54 fold
boundaries the committed record describes, measures how much the result moves between origins,
breaks it down by month, quarter and hotel, lists the worst days for each method, and applies
the acceptance policy declared in :mod:`ml.policy`.

Two files are written, both into ``ml/models/demand_baseline_v1/`` beside the Stage 6.3 metrics:

    validation.json   robustness, regimes, error analysis, leakage re-check
    registry.json     the registry entry, including the acceptance result

``metrics.json`` is **not** rewritten. It is the Stage 6.3 record and this stage has no business
changing it; the validation compares against it instead.

**No model artifact is written.** No pickle, no joblib dump, no weights.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from ml.evaluation import EvaluationResult, evaluate, per_hotel_metrics
from ml.loading import (
    DEFAULT_DATASET,
    DEFAULT_DATASET_MANIFEST,
    DatasetLoadError,
    ProcessedDataset,
    load_dataset_manifest,
    load_processed_dataset,
    require_versions,
    verify_against_manifest,
)
from ml.manifests import (
    DEFAULT_EVALUATION_RECORD,
    build_evaluation_manifest,
    content_checksum,
    serialise,
    stamp_content_checksum,
)
from ml.models import MODEL_VERSION
from ml.policy import ACCEPTANCE_POLICY, AcceptanceEvidence, evaluate_acceptance
from ml.registry import build_registry_record
from ml.validation import (
    DEFAULT_ERROR_SAMPLE,
    ValidationError,
    assert_canonical_feature_order,
    assert_fold_boundaries_match,
    assert_model_version,
    build_validation_report,
    leakage_report,
)

EXPECTED_DATASET_VERSION = "v1"
EXPECTED_FEATURE_VERSION = "v1"

MODEL_DIRECTORY = DEFAULT_EVALUATION_RECORD.parent
DEFAULT_VALIDATION_RECORD = MODEL_DIRECTORY / "validation.json"
DEFAULT_REGISTRY_RECORD = MODEL_DIRECTORY / "registry.json"


def _evaluation_content_checksum(result: EvaluationResult, dataset: ProcessedDataset) -> str:
    """The Stage 6.3 content checksum for this run, computed the Stage 6.3 way."""
    return content_checksum(
        build_evaluation_manifest(
            result,
            dataset_version=EXPECTED_DATASET_VERSION,
            feature_version=EXPECTED_FEATURE_VERSION,
            dataset_sha256=dataset.sha256,
            dataset_rows=len(dataset.rows),
            dataset_path=dataset.path.name,
        )
    )


def _recorded_folds(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise ValidationError(
            f"{path} is missing; the Stage 6.3 record is what the fold boundaries are compared "
            "against. Regenerate it with `python -m ml.pipelines.evaluate_demand_model`."
        )
    record = json.loads(path.read_text(encoding="utf-8"))
    metrics = record.get("metrics")
    if not isinstance(metrics, dict) or not isinstance(metrics.get("per_fold"), list):
        raise ValidationError(f"{path} carries no per-fold block to compare against")
    folds = metrics["per_fold"]
    assert isinstance(folds, list)
    return [dict(fold) for fold in folds]


def _metrics_present(result: EvaluationResult, per_hotel: dict[str, object]) -> bool:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_DATASET_MANIFEST)
    parser.add_argument("--evaluation-record", type=Path, default=DEFAULT_EVALUATION_RECORD)
    parser.add_argument("--validation-out", type=Path, default=DEFAULT_VALIDATION_RECORD)
    parser.add_argument("--registry-out", type=Path, default=DEFAULT_REGISTRY_RECORD)
    parser.add_argument("--model-version", default=MODEL_VERSION)
    parser.add_argument("--error-sample", type=int, default=DEFAULT_ERROR_SAMPLE)
    args = parser.parse_args(argv)

    # --- identity, before anything is measured -------------------------------------------------
    assert_model_version(args.model_version)
    manifest = load_dataset_manifest(args.dataset_manifest)
    require_versions(
        manifest,
        dataset_version=EXPECTED_DATASET_VERSION,
        feature_version=EXPECTED_FEATURE_VERSION,
    )
    dataset = load_processed_dataset(args.dataset)
    verify_against_manifest(dataset, manifest)
    assert_canonical_feature_order(dataset.feature_names)

    # --- the Stage 6.3 protocol, re-run unchanged ----------------------------------------------
    first = evaluate(dataset.rows, feature_names=dataset.feature_names)
    assert_fold_boundaries_match(first, _recorded_folds(args.evaluation_record))

    second = evaluate(dataset.rows, feature_names=dataset.feature_names)
    deterministic = (
        first.predictions == second.predictions
        and first.baseline_pooled == second.baseline_pooled
        and first.learned_pooled == second.learned_pooled
        and _evaluation_content_checksum(first, dataset)
        == _evaluation_content_checksum(second, dataset)
    )

    leakage = leakage_report(first)
    per_hotel = per_hotel_metrics(first.predictions)
    sufficient = sum(
        1
        for block in per_hotel.values()
        if isinstance(block, dict) and block.get("sufficient_coverage")
    )

    validation = build_validation_report(first, error_sample=args.error_sample)

    # --- the policy, applied to counts and versions only ---------------------------------------
    evidence = AcceptanceEvidence(
        paired_observations=len(first.predictions),
        folds=len(first.folds),
        baseline_skipped=first.baseline_pooled.skipped,
        learned_skipped=first.learned_pooled.skipped,
        incomplete_windows=sum(1 for f in first.folds if not f.fold.complete_window),
        hotels_with_sufficient_coverage=sufficient,
        dataset_version=EXPECTED_DATASET_VERSION,
        feature_version=EXPECTED_FEATURE_VERSION,
        dataset_sha256=dataset.sha256,
        forecast_horizon_days=first.policy.horizon_days,
        model_version=args.model_version,
        deterministic=deterministic,
        leakage_checks_passed=bool(leakage["passed"]),
        metrics_present=_metrics_present(first, per_hotel),
    )
    acceptance = evaluate_acceptance(ACCEPTANCE_POLICY, evidence)

    validation["acceptance"] = acceptance.as_dict()
    # The checksum is taken BEFORE the wall clock is attached, and `content_checksum` strips the
    # `generation` block again afterwards, so the two agree by construction.
    validation_sha = content_checksum(validation)
    validation_record: dict[str, object] = {
        **validation,
        "generation": {
            "note": (
                "Wall-clock metadata only, and outside the content checksum: re-running the "
                "validation must not look like a different result."
            ),
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            "pipeline": "ml/pipelines/validate_demand_model.py",
        },
    }
    validation_bytes = serialise(stamp_content_checksum(validation_record))

    registry = build_registry_record(
        first,
        acceptance,
        dataset_version=EXPECTED_DATASET_VERSION,
        feature_version=EXPECTED_FEATURE_VERSION,
        dataset_sha256=dataset.sha256,
        dataset_rows=len(dataset.rows),
        dataset_path=dataset.path.name,
        deterministic=deterministic,
        leakage_passed=bool(leakage["passed"]),
        evaluation_record_sha256=_evaluation_content_checksum(first, dataset),
        validation_sha256=validation_sha,
    )

    args.validation_out.parent.mkdir(parents=True, exist_ok=True)
    args.validation_out.write_bytes(validation_bytes)
    args.registry_out.write_bytes(serialise(stamp_content_checksum(registry)))

    baseline = first.baseline_pooled
    learned = first.learned_pooled
    print(f"dataset       : {args.dataset.name} ({len(dataset.rows)} rows)")
    print(f"model         : {MODEL_VERSION} (no artifact persisted)")
    print(f"folds         : {len(first.folds)} (boundaries match the Stage 6.3 record)")
    print(f"predictions   : {len(first.predictions)} paired")
    print(f"deterministic : {deterministic}")
    print(f"leakage checks: {'passed' if leakage['passed'] else 'FAILED'}")
    print(
        f"baseline      : MAE={baseline.mae} RMSE={baseline.rmse} sMAPE={baseline.smape} "
        f"(n={baseline.observations}, skipped={baseline.skipped})"
    )
    print(
        f"learned       : MAE={learned.mae} RMSE={learned.rmse} sMAPE={learned.smape} "
        f"(n={learned.observations}, skipped={learned.skipped})"
    )
    print(f"policy        : {acceptance.policy_version} ({acceptance.policy_sha256[:16]}...)")
    print(
        f"acceptance    : {'PASS' if acceptance.passed else 'FAIL'} "
        f"({sum(1 for c in acceptance.checks if c.passed)}/{len(acceptance.checks)} criteria)"
    )
    for check in acceptance.failed:
        print(f"    FAILED {check.name}: required {check.requirement}, observed {check.observed}")
    print(f"validation    : {args.validation_out}")
    print(f"registry      : {args.registry_out}")
    return 0 if acceptance.passed else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    try:
        raise SystemExit(main())
    except (ValidationError, DatasetLoadError) as exc:  # pragma: no cover - entry point
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
