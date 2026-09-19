"""Fit and persist the ``demand_baseline_v1`` artifact.

    python -m ml.pipelines.build_demand_artifact --verify

Loads the pinned dataset, checks it against the committed manifest, fits the **exact** Stage 6.3
estimator once on the dataset's declared training partition, writes the payload and its metadata,
and amends the registry with an artifact block.

What it does not do: re-run the backtest, recompute acceptance, restate a Stage 6.4 fact, or tune
anything. The Stage 6.4 validation record remains authoritative and is referenced by checksum
rather than repeated.

``--verify`` fits twice and requires both the canonical model digest and the serialised bytes to
be compared explicitly, reporting each separately: the digest is the cross-build claim, the bytes
are only ever a same-build observation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

from ml.artifact import (
    DEFAULT_ARTIFACT_METADATA,
    DEFAULT_MODEL_PAYLOAD,
    ArtifactError,
    TrainedArtifact,
    build_artifact_metadata,
    canonical_model_digest,
    deserialise_model,
    probe_predictions,
    serialise_model,
    train_artifact,
)
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
    content_checksum,
    serialise,
    stamp_content_checksum,
)
from ml.models import MODEL_VERSION
from ml.registry import attach_artifact, without_artifact
from ml.validation import ValidationError

EXPECTED_DATASET_VERSION = "v1"
EXPECTED_FEATURE_VERSION = "v1"

MODEL_DIRECTORY = DEFAULT_EVALUATION_RECORD.parent
DEFAULT_VALIDATION_RECORD = MODEL_DIRECTORY / "validation.json"
DEFAULT_REGISTRY_RECORD = MODEL_DIRECTORY / "registry.json"


def _read_record(path: Path, label: str) -> dict[str, object]:
    if not path.is_file():
        raise ArtifactError(
            f"{path} is missing; the {label} record is a Stage 6.4 output and this stage "
            "references it. Regenerate it with `python -m ml.pipelines.validate_demand_model`."
        )
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ArtifactError(f"{path} is not a JSON object")
    return record


def _validation_checksum(path: Path) -> str:
    """The validation record is never amended, so its checksum is unconditional."""
    return content_checksum(_read_record(path, "validation"))


def _registry_checksum_before_artifact(path: Path) -> str:
    """The registry state this artifact is built *against*.

    Stripping any existing artifact block first is what makes the pipeline idempotent: run it
    twice and ``artifact.json`` comes out the same, because the second run references the same
    pre-artifact registry the first one did rather than the first one's output.
    """
    return content_checksum(without_artifact(_read_record(path, "registry")))


def _load_dataset(dataset_path: Path, manifest_path: Path) -> ProcessedDataset:
    manifest = load_dataset_manifest(manifest_path)
    require_versions(
        manifest,
        dataset_version=EXPECTED_DATASET_VERSION,
        feature_version=EXPECTED_FEATURE_VERSION,
    )
    dataset = load_processed_dataset(dataset_path)
    verify_against_manifest(dataset, manifest)
    return dataset


def _fit(dataset: ProcessedDataset) -> TrainedArtifact:
    return train_artifact(
        dataset,
        dataset_version=EXPECTED_DATASET_VERSION,
        feature_version=EXPECTED_FEATURE_VERSION,
    )


def registry_artifact_block(
    metadata: dict[str, object], *, metadata_filename: str
) -> dict[str, object]:
    """The compact summary the registry carries: a pointer, not a second copy.

    The registry's job is to say *that* an artifact exists and how to identify it. What it is
    made of belongs in ``artifact.json``, and duplicating it here would create two descriptions
    that can drift.
    """
    artifact = metadata["artifact"]
    training = metadata["training"]
    dataset = metadata["dataset"]
    model = metadata["model"]
    assert isinstance(artifact, dict)
    assert isinstance(training, dict)
    assert isinstance(dataset, dict)
    assert isinstance(model, dict)
    return {
        "record_kind": "MODEL ARTIFACT METADATA",
        "record_note": (
            "Distinct from the validation record above, which is the Stage 6.4 measurement and "
            "is unchanged by this stage."
        ),
        "schema_version": metadata["schema_version"],
        "metadata_file": metadata_filename,
        "format": artifact["format"],
        "filename": artifact["filename"],
        "sha256": artifact["sha256"],
        "bytes": artifact["bytes"],
        "committed": artifact["committed"],
        "canonical_model_digest": artifact["canonical_model_digest"],
        "estimator_configuration_sha256": model["estimator_configuration_sha256"],
        "forecast_horizon_days": model["forecast_horizon_days"],
        "dataset_sha256": dataset["dataset_sha256"],
        "feature_version": dataset["feature_version"],
        "feature_columns": list(dataset["feature_columns"]),
        "training_partition": training["partition"],
        "training_start_date": training["training_start_date"],
        "training_end_date": training["training_end_date"],
        "training_row_count": training["training_row_count"],
        "training_hotel_count": training["training_hotel_count"],
        "training_date_count": training["training_date_count"],
        "serving_enabled": False,
        "production_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_DATASET_MANIFEST)
    parser.add_argument("--payload-out", type=Path, default=DEFAULT_MODEL_PAYLOAD)
    parser.add_argument("--metadata-out", type=Path, default=DEFAULT_ARTIFACT_METADATA)
    parser.add_argument("--validation-record", type=Path, default=DEFAULT_VALIDATION_RECORD)
    parser.add_argument("--registry-record", type=Path, default=DEFAULT_REGISTRY_RECORD)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="fit twice and compare the canonical digest and the serialised bytes",
    )
    args = parser.parse_args(argv)

    dataset = _load_dataset(args.dataset, args.dataset_manifest)

    started = time.perf_counter()
    trained = _fit(dataset)
    fit_seconds = time.perf_counter() - started

    payload = serialise_model(trained.estimator)
    digest = hashlib.sha256(payload).hexdigest()
    canonical = canonical_model_digest(trained)

    if args.verify:
        again = _fit(dataset)
        second_payload = serialise_model(again.estimator)
        second_canonical = canonical_model_digest(again)
        if second_canonical != canonical:
            print("::error::the artifact is not reproducible", file=sys.stderr)
            return 1
        print(f"reproducible : two fits agree on canonical digest {canonical}")
        identical = second_payload == payload
        print(
            "bytes        : "
            + (
                "identical on this build (an observation, not a cross-build claim)"
                if identical
                else "differ between fits; the canonical digest is the reproducibility claim"
            )
        )
        if probe_predictions(again.estimator) != probe_predictions(trained.estimator):
            print("::error::probe predictions differ between fits", file=sys.stderr)
            return 1

    metadata = build_artifact_metadata(
        trained,
        artifact_sha256=digest,
        artifact_bytes=len(payload),
        artifact_filename=args.payload_out.name,
        dataset_path=args.dataset.name,
        validation_sha256=_validation_checksum(args.validation_record),
        registry_sha256=_registry_checksum_before_artifact(args.registry_record),
    )

    args.payload_out.parent.mkdir(parents=True, exist_ok=True)
    args.payload_out.write_bytes(payload)
    args.metadata_out.write_bytes(serialise(metadata))

    # Amend the registry rather than rebuilding it: every validation fact is carried across
    # untouched, because it is never recomputed.
    registry = without_artifact(_read_record(args.registry_record, "registry"))
    amended = attach_artifact(
        registry, registry_artifact_block(metadata, metadata_filename=args.metadata_out.name)
    )
    generation = amended.get("generation")
    if isinstance(generation, dict):
        amended["generation"] = {
            **generation,
            "artifact_attached_at": dt.datetime.now(dt.UTC).isoformat(),
            "artifact_attached_by": "ml/pipelines/build_demand_artifact.py",
        }
    args.registry_record.write_bytes(serialise(stamp_content_checksum(amended)))

    started = time.perf_counter()
    restored = deserialise_model(args.payload_out.read_bytes())
    load_seconds = time.perf_counter() - started
    if probe_predictions(restored) != probe_predictions(trained.estimator):
        print(
            "::error::the reloaded artifact does not reproduce its own predictions",
            file=sys.stderr,
        )
        return 1

    print(f"dataset      : {args.dataset.name} ({len(dataset.rows)} rows)")
    print(f"model        : {MODEL_VERSION}")
    print(f"features     : {len(trained.feature_columns)} at horizon {trained.horizon_days}")
    print(
        f"training     : {trained.fitted_rows} rows fitted of {trained.partition_rows} in the "
        f"{trained.training_start} .. {trained.training_end} partition "
        f"({trained.held_out_for_missing_features} held out for missing features)"
    )
    print(f"               {len(trained.training_hotels)} hotels, {trained.training_dates} dates")
    print(f"payload      : {args.payload_out} ({len(payload)} bytes)")
    print(f"artifact sha : {digest}")
    print(f"canonical    : {canonical}")
    print(f"metadata     : {args.metadata_out}")
    print(f"registry     : {args.registry_record} (artifact block attached)")
    print(f"fit time     : {fit_seconds * 1000:.1f} ms")
    print(f"load time    : {load_seconds * 1000:.2f} ms")
    print("serving      : disabled (no endpoint, no loading path in backend/)")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    try:
        raise SystemExit(main())
    except (ArtifactError, ValidationError, DatasetLoadError) as exc:  # pragma: no cover
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
