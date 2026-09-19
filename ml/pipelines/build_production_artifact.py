"""Regenerate the approved model for a production image, and refuse anything that is not it.

Stage 6.7. This runs **only inside a disposable Docker build stage** -- never in the API, never
at runtime, never from a request. Its output is two files: the payload the production image
serves, and the metadata that describes it.

## The question this module exists to answer

Stage 6.5 fitted the approved model and recorded its identity. Stage 6.6 served it. Neither
could put it inside the production image, because the payload is not committed: ``.gitignore``
has excluded model weights since Stage 1, and a pickle is arbitrary code on load. So the image
has to *regenerate* it from the committed dataset and the pinned configuration -- and the only
question that matters is whether the thing it regenerated is the approved model.

## Why the payload's SHA-256 is not the answer

It was the obvious answer and it is measurably wrong. Refitting this model on one machine, from
one dataset, with one seed, produces a **different payload digest for every OpenMP thread
count**:

    threads   1  ->  3e9986c5...      threads   8  ->  fb751fe4...
    threads   2  ->  b2c5802a...      threads  12  ->  bdfeb3b8...   <- the Stage 6.5 record
    threads   4  ->  d5b21ccd...

Every one of those is 711,530 bytes, and every one has the **same canonical model digest**.
Twelve is the physical core count of the machine Stage 6.5 was built on; it is not recorded in
``artifact.json``, which names the interpreter, the platform and the library versions but not
the thread count. So ``bdfeb3b8...`` identifies a machine's CPU topology as much as it
identifies a model, and requiring a Linux build to reproduce it would be requiring the build
container to have twelve cores.

The canonical model digest is what Stage 6.5 built for exactly this purpose: a hash over the
feature columns, the estimator configuration, the training extent and the model's predictions
on a fixed synthetic probe grid, each formatted to six decimal places. It is a fingerprint of
*what the model computes*, and it is invariant to everything above.

**Nothing is weakened by this.** The payload digest is still verified -- against the metadata
generated beside it, before any deserialisation, exactly as Stage 6.6 requires -- and eleven
further approved values are verified here that no digest comparison would have covered.

## What this refuses

Every check below is fatal. A mismatch exits non-zero, the ``RUN`` fails, and no image is
produced. There is no warning path and no flag that turns one off.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy
import scipy
import sklearn

from app.ml.dataset import DATASET_VERSION, FEATURE_VERSION
from ml.artifact import (
    ARTIFACT_FORMAT,
    ARTIFACT_SCHEMA_VERSION,
    DEFAULT_ARTIFACT_METADATA,
    PICKLE_PROTOCOL,
    TrainedArtifact,
    canonical_model_digest,
    load_artifact_metadata,
    probe_predictions,
    serialise_model,
    train_artifact,
)
from ml.loading import DEFAULT_DATASET, load_processed_dataset
from ml.models import MODEL_NAME, MODEL_VERSION
from ml.registry import configuration_checksum

#: The identity that travels across environments. Everything else about a pickle can move.
IDENTITY_FIELD = "canonical_model_digest"

#: The payload digest Stage 6.5 recorded, kept here as a FACT to report rather than a rule to
#: enforce. A production build that happens to reproduce it says something mildly interesting
#: about the toolchain; one that does not says nothing about the model.
STAGE_65_PAYLOAD_SHA256 = "bdfeb3b81b05a1884dba6b3cc687174204fe7dd13ebb26a0c65161e710fb140b"


class ProductionArtifactError(Exception):
    """The regenerated model is not the approved one. No image may be built from it."""


def _block(metadata: Mapping[str, object], name: str) -> Mapping[str, object]:
    block = metadata.get(name)
    if not isinstance(block, dict):
        raise ProductionArtifactError(f"the approved metadata has no {name!r} block")
    return block


class Checks:
    """Every comparison, collected so the log shows all of them rather than only the first."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def equal(self, what: str, produced: object, approved: object) -> None:
        if produced == approved:
            self.passed += 1
            print(f"  OK   {what}")
            return
        self.failures.append(what)
        print(f"  FAIL {what}")
        print(f"         approved : {approved!r}")
        print(f"         produced : {produced!r}")


def verify(trained: TrainedArtifact, approved: Mapping[str, object], payload: bytes) -> Checks:
    """Hold the regenerated model against every approved value Stage 6.5 recorded."""
    model = _block(approved, "model")
    dataset_block = _block(approved, "dataset")
    artifact_block = _block(approved, "artifact")
    claims = _block(approved, "claims")

    produced_configuration = trained.model.config.as_dict()
    checks = Checks()

    checks.equal("schema version", approved.get("schema_version"), ARTIFACT_SCHEMA_VERSION)
    checks.equal("model name", MODEL_NAME, model.get("model_name"))
    checks.equal("model version", MODEL_VERSION, model.get("model_version"))
    # Against the CODE constants, not against what the metadata says. `train_artifact` is told
    # these two, so comparing the fitted artifact's copy of them to the file that supplied them
    # would be comparing a value with itself -- which is what this did until a test tampered
    # with `feature_version` and the build cheerfully accepted it.
    checks.equal("feature version", FEATURE_VERSION, dataset_block.get("feature_version"))
    checks.equal("dataset version", DATASET_VERSION, dataset_block.get("dataset_version"))
    checks.equal("feature version reached the fit", trained.feature_version, FEATURE_VERSION)
    checks.equal("dataset version reached the fit", trained.dataset_version, DATASET_VERSION)
    checks.equal("dataset sha256", trained.dataset_sha256, dataset_block.get("dataset_sha256"))
    checks.equal(
        "feature columns and order",
        list(trained.feature_columns),
        dataset_block.get("feature_columns"),
    )
    checks.equal("forecast horizon", trained.horizon_days, model.get("forecast_horizon_days"))
    checks.equal(
        "estimator configuration", produced_configuration, model.get("estimator_configuration")
    )
    checks.equal(
        "estimator configuration sha256",
        configuration_checksum(produced_configuration),
        model.get("estimator_configuration_sha256"),
    )
    checks.equal("artifact format", ARTIFACT_FORMAT, artifact_block.get("format"))
    checks.equal(
        "claims",
        {
            key: claims.get(key)
            for key in (
                "production_ready",
                "production_accuracy_established",
                "cross_hotel_generalisation_established",
                "serving_enabled",
            )
        },
        {
            "production_ready": False,
            "production_accuracy_established": False,
            "cross_hotel_generalisation_established": False,
            "serving_enabled": False,
        },
    )

    # The two that bind the regenerated ESTIMATOR rather than its description. Probe predictions
    # are an input to the canonical digest, so they are checked first and separately: when the
    # digest moves, this line says whether the model's arithmetic moved or something else did.
    checks.equal(
        "probe predictions",
        list(probe_predictions(trained.estimator)),
        artifact_block.get("probe_predictions"),
    )
    checks.equal(
        "canonical model digest",
        canonical_model_digest(trained),
        artifact_block.get(IDENTITY_FIELD),
    )

    # Training extent: not identity, but a regenerated model fitted on a different number of
    # rows would be a different model whatever its digest said.
    training = _block(approved, "training")
    checks.equal("partition rows", trained.partition_rows, training.get("partition_rows"))
    checks.equal("rows reaching fit", trained.fitted_rows, training.get("training_row_count"))
    checks.equal(
        "training start", trained.training_start.isoformat(), training.get("training_start_date")
    )
    checks.equal(
        "training end", trained.training_end.isoformat(), training.get("training_end_date")
    )
    checks.equal("training dates", trained.training_dates, training.get("training_date_count"))
    checks.equal("training hotels", list(trained.training_hotels), training.get("training_hotels"))

    # Reported, never required. See the module docstring.
    produced_sha = hashlib.sha256(payload).hexdigest()
    print()
    print(f"  payload sha256 (this build) : {produced_sha}")
    print(f"  payload sha256 (Stage 6.5)  : {STAGE_65_PAYLOAD_SHA256}")
    print(
        "  identical                   : "
        f"{produced_sha == STAGE_65_PAYLOAD_SHA256}  (environment-scoped; not an identity)"
    )
    return checks


def production_metadata(approved: Mapping[str, object], payload: bytes) -> dict[str, Any]:
    """The approved metadata, with the two environment-scoped facts replaced and one added.

    **Deterministic.** No wall clock, no path, no hostname: two builds of one commit on one
    machine produce byte-identical output, which is what the image-reproducibility job
    requires. The ``generation`` block is carried over untouched -- it describes the Stage 6.5
    build that established the identity, and that is still what it describes.
    """
    metadata = json.loads(json.dumps(approved))  # a deep copy, through data rather than code
    digest = hashlib.sha256(payload).hexdigest()

    metadata["artifact"]["sha256"] = digest
    metadata["artifact"]["bytes"] = len(payload)
    metadata["production_build"] = {
        "note": (
            "Regenerated from the committed dataset inside a disposable Docker build stage. "
            "The model is the approved one: every value in the model, dataset, training and "
            "claims blocks was verified against the Stage 6.5 record, and so were the probe "
            "predictions and the canonical model digest."
        ),
        "identity": IDENTITY_FIELD,
        "identity_value": metadata["artifact"][IDENTITY_FIELD],
        "payload_sha256": digest,
        "stage_65_payload_sha256": STAGE_65_PAYLOAD_SHA256,
        "payload_sha256_matches_stage_65": digest == STAGE_65_PAYLOAD_SHA256,
        "payload_sha256_note": (
            "Environment-scoped, and deliberately not a requirement. The payload digest of "
            "this model changes with the OpenMP thread count of the machine that fits it -- "
            "measured at 1, 2, 4, 8 and 12 threads, five different digests, all 711530 bytes, "
            "all with the same canonical model digest. The Stage 6.5 value was produced at "
            "twelve threads, which is that machine's physical core count and is recorded "
            "nowhere in the artifact. The canonical digest is the identity that travels."
        ),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.system(),
            "scikit_learn": sklearn.__version__,
            "numpy": numpy.__version__,
            "scipy": scipy.__version__,
            "pickle_protocol": PICKLE_PROTOCOL,
            "openmp_threads": _effective_threads(),
        },
    }
    return metadata


def _effective_threads() -> int | None:
    """The thread count this build fitted at, recorded because Stage 6.5 did not record it."""
    try:
        from sklearn.utils._openmp_helpers import _openmp_effective_n_threads

        return int(_openmp_effective_n_threads())
    except Exception:  # pragma: no cover - a private helper that may move between releases
        return None


def build(out_dir: Path, approved_metadata_path: Path, dataset_path: Path) -> int:
    print("--- regenerating the approved demand model -------------------------------------")
    print(f"  dataset  : {dataset_path.name}")
    print(f"  approved : {approved_metadata_path.name}")
    print()

    if not dataset_path.is_file():
        raise ProductionArtifactError(f"the training dataset {dataset_path.name} is not present")
    approved = load_artifact_metadata(approved_metadata_path)

    dataset = load_processed_dataset(dataset_path)
    # The code's versions, not the file's. See the note in `verify`.
    trained = train_artifact(
        dataset, dataset_version=DATASET_VERSION, feature_version=FEATURE_VERSION
    )
    payload = serialise_model(trained.estimator)

    checks = verify(trained, approved, payload)
    print()
    if checks.failures:
        print(f"--- REFUSED: {len(checks.failures)} approved value(s) did not match ---")
        for failure in checks.failures:
            print(f"  {failure}")
        raise ProductionArtifactError(
            "the regenerated model is not the approved model; no image may be built from it"
        )
    print(f"--- {checks.passed} approved values verified ---")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "model.pkl").write_bytes(payload)
    (out_dir / "artifact.json").write_text(
        json.dumps(production_metadata(approved, payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # The last word: read back what was written and confirm the pair is self-consistent, which
    # is the property the runtime loader checks before it deserialises anything.
    written = json.loads((out_dir / "artifact.json").read_text(encoding="utf-8"))
    on_disk = hashlib.sha256((out_dir / "model.pkl").read_bytes()).hexdigest()
    if written["artifact"]["sha256"] != on_disk:
        raise ProductionArtifactError("the written payload does not match the written metadata")

    print(f"  wrote {out_dir / 'model.pkl'} ({len(payload)} bytes)")
    print(f"  wrote {out_dir / 'artifact.json'}")
    print(f"  identity ({IDENTITY_FIELD}): {written['artifact'][IDENTITY_FIELD]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="directory to write into")
    parser.add_argument("--approved-metadata", type=Path, default=DEFAULT_ARTIFACT_METADATA)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args(argv)
    try:
        return build(args.out, args.approved_metadata, args.dataset)
    except ProductionArtifactError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - the container's entry point
    raise SystemExit(main())


__all__ = [
    "IDENTITY_FIELD",
    "STAGE_65_PAYLOAD_SHA256",
    "Checks",
    "ProductionArtifactError",
    "build",
    "main",
    "production_metadata",
    "verify",
]
