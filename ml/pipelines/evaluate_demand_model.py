"""Run the Stage 6.3 rolling-origin backtest and write the evaluation record.

    python -m ml.pipelines.evaluate_demand_model
    python -m ml.pipelines.evaluate_demand_model --verify

Loads the committed Stage 6.2 dataset, checks it against the committed manifest, backtests the
seasonal-naive baseline and the learned model over every origin, and writes
``ml/models/demand_baseline_v1/metrics.json``.

**It writes no model artifact.** Configuration, metrics and provenance only -- see
``ml/manifests.py`` for why.

``--verify`` runs the whole evaluation twice and requires the two content checksums to match,
which is the reproducibility claim being tested rather than asserted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ml.evaluation import EvaluationResult, evaluate
from ml.loading import (
    DEFAULT_DATASET,
    DEFAULT_DATASET_MANIFEST,
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

EXPECTED_DATASET_VERSION = "v1"
EXPECTED_FEATURE_VERSION = "v1"


def _run(dataset_path: Path, manifest_path: Path) -> tuple[EvaluationResult, dict[str, object]]:
    manifest = load_dataset_manifest(manifest_path)
    require_versions(
        manifest,
        dataset_version=EXPECTED_DATASET_VERSION,
        feature_version=EXPECTED_FEATURE_VERSION,
    )
    dataset = load_processed_dataset(dataset_path)
    verify_against_manifest(dataset, manifest)

    result = evaluate(dataset.rows, feature_names=dataset.feature_names)
    record = build_evaluation_manifest(
        result,
        dataset_version=EXPECTED_DATASET_VERSION,
        feature_version=EXPECTED_FEATURE_VERSION,
        dataset_sha256=dataset.sha256,
        dataset_rows=len(dataset.rows),
        dataset_path=dataset_path.name,
    )
    return result, record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_DATASET_MANIFEST)
    parser.add_argument("--out", type=Path, default=DEFAULT_EVALUATION_RECORD)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="evaluate twice and require identical content checksums",
    )
    args = parser.parse_args(argv)

    result, record = _run(args.dataset, args.dataset_manifest)
    digest = content_checksum(record)

    if args.verify:
        _, again = _run(args.dataset, args.dataset_manifest)
        if content_checksum(again) != digest:
            print("::error::the evaluation is not reproducible", file=sys.stderr)
            return 1
        print(f"reproducible: two evaluations agree on {digest}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(serialise(stamp_content_checksum(record)))

    baseline = result.baseline_pooled
    learned = result.learned_pooled
    print(f"dataset     : {args.dataset.name} ({result.dataset_rows} rows)")
    print(f"hotels      : {', '.join(result.dataset_hotels)}")
    print(
        f"features    : {len(result.selection.selected)} selected, "
        f"{len(result.selection.excluded)} excluded at horizon "
        f"{result.policy.horizon_days}"
    )
    print(f"folds       : {len(result.folds)} evaluated, {len(result.skipped_folds)} skipped")
    print(f"predictions : {len(result.predictions)}")
    print(
        f"baseline    : MAE={baseline.mae} RMSE={baseline.rmse} sMAPE={baseline.smape} "
        f"(n={baseline.observations}, skipped={baseline.skipped})"
    )
    print(
        f"learned     : MAE={learned.mae} RMSE={learned.rmse} sMAPE={learned.smape} "
        f"(n={learned.observations}, skipped={learned.skipped})"
    )
    print(f"record      : {args.out}")
    print(f"content sha : {digest}")
    print(f"model       : {MODEL_VERSION} (no artifact persisted)")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
