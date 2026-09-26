"""Measure the three Stage 7.14 horizons under the frozen ``multi_horizon_v1`` protocol.

    python -m ml.pipelines.evaluate_horizons            # measure, accept, fit, write records
    python -m ml.pipelines.evaluate_horizons --check    # re-measure and compare; write nothing

For each horizon, in the protocol's order (7, 14, 28): refuse to start unless the protocol's
settings still hash to the frozen checksum; load the committed horizon-matched dataset and
verify it against its manifest and the protocol's digest; run the rolling-origin backtest twice;
re-check leakage at the dataset's own horizon; apply ``acceptance_v2`` (counts, versions, digests
and booleans only -- never a metric); fit the model once on the declared training partition.

Four records are written to ``ml/models/demand_h{h}_v1/``: ``metrics.json``,
``validation.json``, ``registry.json`` and ``artifact.json``. The fitted payload, ``model.pkl``,
is written beside them for reproducibility and is ignored by git: it is never committed, and
nothing loads it.

Nothing here touches ``demand_baseline_v1``, its records, its artifact or ``APPROVED_MODEL``.
"""

from __future__ import annotations

import argparse
import json
import sys

from ml.horizons import (
    HORIZONS,
    HorizonError,
    HorizonSpec,
    artifact_record,
    assert_protocol_frozen,
    model_payload,
    run_horizon,
    side_by_side,
    train_horizon_artifact,
)
from ml.loading import (
    DatasetLoadError,
    load_dataset_manifest,
    load_processed_dataset,
    require_versions,
    verify_against_manifest,
)
from ml.manifests import content_checksum, serialise, stamp_content_checksum

RECORDS = ("metrics.json", "validation.json", "registry.json", "artifact.json")


def measure(spec: HorizonSpec) -> dict[str, dict[str, object]]:
    """Every record for one horizon, stamped, plus the payload under the key ``model.pkl``."""
    manifest = load_dataset_manifest(spec.manifest_path)
    require_versions(manifest, dataset_version="v1", feature_version="v1")
    dataset = load_processed_dataset(spec.dataset_path)
    verify_against_manifest(dataset, manifest)

    run = run_horizon(spec, dataset)
    if not run.acceptance.passed:
        failed = ", ".join(check.name for check in run.acceptance.failed)
        raise HorizonError(f"{spec.model_version}: acceptance_v2 FAILED ({failed})")

    artifact = train_horizon_artifact(spec, dataset)
    payload = model_payload(artifact)
    metrics = stamp_content_checksum(run.metrics)
    validation = stamp_content_checksum(run.validation)
    registry = stamp_content_checksum(run.registry)
    artifact_json = stamp_content_checksum(
        artifact_record(
            artifact,
            payload=payload,
            validation_sha256=content_checksum(validation),
            registry_sha256=content_checksum(registry),
        )
    )
    return {
        "metrics.json": metrics,
        "validation.json": validation,
        "registry.json": registry,
        "artifact.json": artifact_json,
        "model.pkl": {"payload": payload},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-measure every horizon and compare content checksums with the committed records",
    )
    args = parser.parse_args(argv)

    assert_protocol_frozen()
    for spec in HORIZONS:
        records = measure(spec)
        directory = spec.model_directory
        if args.check:
            for name in RECORDS:
                committed = json.loads((directory / name).read_text(encoding="utf-8"))
                fresh = records[name]
                if content_checksum(committed) != content_checksum(fresh):
                    if name == "artifact.json" and _differs_only_in_payload(committed, fresh):
                        continue
                    raise HorizonError(f"{directory / name} differs from a fresh measurement")
            print(f"{spec.model_version}: every record reproduces")
            continue

        directory.mkdir(parents=True, exist_ok=True)
        for name in RECORDS:
            (directory / name).write_bytes(serialise(records[name]))
        payload = records["model.pkl"]["payload"]
        assert isinstance(payload, bytes)
        (directory / "model.pkl").write_bytes(payload)

        report = side_by_side(records["registry.json"])
        baseline = report["baseline"]
        learned = report["learned"]
        assert isinstance(baseline, dict) and isinstance(learned, dict)
        print(
            f"{spec.model_version} ({spec.horizon_days} days, {report['paired_observations']} "
            f"paired): baseline {spec.baseline_method} MAE={baseline['mae']} "
            f"RMSE={baseline['rmse']} "
            f"sMAPE={baseline['smape']} | learned MAE={learned['mae']} RMSE={learned['rmse']} "
            f"sMAPE={learned['smape']} | acceptance_v2 PASS"
        )
    return 0


def _differs_only_in_payload(committed: dict[str, object], fresh: dict[str, object]) -> bool:
    """Pickle bytes are a property of the toolchain, not of the model. The canonical digest is."""

    def without_payload(record: dict[str, object]) -> dict[str, object]:
        block = dict(record.get("artifact", {}))  # type: ignore[call-overload]
        block.pop("sha256", None)
        block.pop("bytes", None)
        return {**record, "artifact": block}

    return content_checksum(without_payload(committed)) == content_checksum(without_payload(fresh))


if __name__ == "__main__":  # pragma: no cover - entry point
    try:
        raise SystemExit(main())
    except (HorizonError, DatasetLoadError) as exc:  # pragma: no cover
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
