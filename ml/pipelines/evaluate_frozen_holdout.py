"""R2.2: score the frozen served artifact under the pre-registered ``frozen_holdout_v1`` protocol.

    python -m ml.pipelines.evaluate_frozen_holdout            # evaluate, write the record
    python -m ml.pipelines.evaluate_frozen_holdout --check    # re-evaluate, compare, write nothing

In order: refuse to start unless the protocol's settings still hash to the committed checksum;
load the committed dataset and verify it against its manifest and the pinned digest; hash the
local payload (it must be the pinned one); load it through ``ml.artifact.load_artifact``, which
verifies the payload digest before unpickling; measure the model's canonical digest from its own
probe predictions; build the record **twice** and require identical content checksums; measure
the canonical digest and the payload hash again; write ``ml/evaluations/frozen_holdout_v1.json``.

Nothing is refitted except the two cross-hotel models, which live only in memory. Nothing is
written to ``ml/models/``, the dataset or its manifest. No database is touched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import numpy
import scipy
import sklearn

from ml.artifact import (
    DEFAULT_ARTIFACT_METADATA,
    DEFAULT_MODEL_PAYLOAD,
    ArtifactError,
    load_artifact,
    load_artifact_metadata,
)
from ml.frozen_holdout import (
    DATASET_SHA256,
    FROZEN_MODEL_VERSION,
    FROZEN_PAYLOAD_SHA256,
    PROTOCOL_VERSION,
    FrozenHoldoutError,
    assert_protocol_frozen,
    build_record,
    require_frozen_model,
    served_forecaster,
)
from ml.loading import (
    REPOSITORY_ROOT,
    DatasetLoadError,
    load_dataset_manifest,
    load_processed_dataset,
    require_versions,
    verify_against_manifest,
)
from ml.manifests import content_checksum, serialise, stamp_content_checksum

DEFAULT_RECORD = REPOSITORY_ROOT / "ml" / "evaluations" / f"{PROTOCOL_VERSION}.json"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


def evaluate(
    payload_path: Path = DEFAULT_MODEL_PAYLOAD,
    metadata_path: Path = DEFAULT_ARTIFACT_METADATA,
    *,
    require_pinned_payload: bool = True,
    generated_at: dt.datetime | None = None,
) -> dict[str, object]:
    """The stamped record. ``require_pinned_payload=False`` admits a rebuilt payload, whose bytes
    differ by machine; its canonical digest must still be the frozen one."""
    assert_protocol_frozen()

    manifest = load_dataset_manifest()
    require_versions(manifest, dataset_version="v1", feature_version="v1")
    dataset = load_processed_dataset()
    verify_against_manifest(dataset, manifest)

    payload_before = _sha256_file(payload_path)
    metadata_before = _sha256_file(metadata_path)
    if require_pinned_payload and payload_before != FROZEN_PAYLOAD_SHA256:
        raise FrozenHoldoutError(
            f"{payload_path} hashes to {payload_before}, the protocol pins {FROZEN_PAYLOAD_SHA256}"
        )
    artifact_record = load_artifact_metadata(metadata_path)
    loaded = load_artifact(
        payload_path,
        metadata_path,
        expected_model_version=FROZEN_MODEL_VERSION,
        expected_dataset_sha256=DATASET_SHA256,
    )
    digest_before = require_frozen_model(loaded.estimator, artifact_record)

    identity: Mapping[str, object] = {
        "model_version": loaded.model_version,
        "canonical_model_digest": digest_before,
        "feature_columns": list(loaded.feature_columns),
        "forecast_horizon_days": loaded.forecast_horizon_days,
        "refitted": False,
    }
    first = build_record(dataset, served_forecaster(loaded), model_identity=identity)
    second = build_record(dataset, served_forecaster(loaded), model_identity=identity)
    if content_checksum(first) != content_checksum(second):
        raise FrozenHoldoutError("two runs in one process produced different records")

    digest_after = require_frozen_model(loaded.estimator, artifact_record)
    payload_after = _sha256_file(payload_path)
    metadata_after = _sha256_file(metadata_path)
    if (payload_after, metadata_after) != (payload_before, metadata_before):
        raise FrozenHoldoutError("the artifact files changed during the evaluation")

    record = dict(first)
    record["artifact_identity_check"] = {
        "canonical_model_digest_before": digest_before,
        "canonical_model_digest_after": digest_after,
        "unchanged": digest_before == digest_after,
    }
    record["generation"] = {
        "note": (
            "Wall-clock, revision and machine metadata, outside the content checksum: the payload "
            "bytes vary with the OpenMP thread count, the canonical digest does not."
        ),
        "generated_at": (generated_at or dt.datetime.now(dt.UTC)).isoformat(),
        "pipeline": "ml/pipelines/evaluate_frozen_holdout.py",
        "code_revision": _git("rev-parse", "HEAD"),
        "working_tree_clean": _git("status", "--porcelain") == "",
        "payload": {
            "path": payload_path.name,
            "sha256_before": payload_before,
            "sha256_after": payload_after,
            "pinned": payload_before == FROZEN_PAYLOAD_SHA256,
        },
        "artifact_json_sha256": metadata_before,
        "runs_compared": 2,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.system(),
            "numpy": numpy.__version__,
            "scipy": scipy.__version__,
            "scikit_learn": sklearn.__version__,
        },
    }
    return stamp_content_checksum(record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-evaluate and compare the content checksum with the committed record",
    )
    args = parser.parse_args(argv)

    record = evaluate()
    digest = content_checksum(record)
    if args.check:
        committed = json.loads(DEFAULT_RECORD.read_text(encoding="utf-8"))
        if content_checksum(committed) != digest:
            raise FrozenHoldoutError(f"{DEFAULT_RECORD} differs from a fresh evaluation")
        print(f"{PROTOCOL_VERSION}: the committed record reproduces ({digest})")
        return 0

    DEFAULT_RECORD.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_RECORD.write_bytes(serialise(record))
    print(f"{PROTOCOL_VERSION}: wrote {DEFAULT_RECORD.relative_to(REPOSITORY_ROOT)} ({digest})")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    try:
        raise SystemExit(main())
    except (FrozenHoldoutError, ArtifactError, DatasetLoadError) as exc:  # pragma: no cover
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
