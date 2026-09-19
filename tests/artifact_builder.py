"""Build a real `demand_baseline_v1` artifact pair into a temporary directory.

The payload is never committed -- ``.gitignore`` has excluded model weights since Stage 1, and
a pickle is arbitrary code on load -- so a test that read one from the working tree would be
testing the developer's disk and would be skipped, or wrong, in CI. Every test that needs a
loadable artifact builds one here instead, from the committed dataset and the pinned
configuration, which costs about a quarter of a second and proves reproducibility on the way
past.

Shared by ``tests/backend/test_ml_serving.py`` and ``tests/integration/test_ml_serving_api.py``
rather than copied into each, for the reason every other shared helper in this suite exists:
two copies of a fixture is two definitions of what "the approved artifact" means.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from app.ml.artifact_store import ArtifactLocation

#: A fixed build stamp, so the metadata two calls produce differs in nothing at all.
BUILT_AT = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def build_artifact(directory: Path) -> ArtifactLocation:
    """Fit the approved model on the committed dataset and write the pair into *directory*.

    The metadata is produced by ``ml.artifact.build_artifact_metadata`` -- the same function the
    real build pipeline uses -- rather than hand-written here. A hand-written one would be a
    second description of the artifact format, and the loader would then be verified against a
    fixture rather than against what the pipeline actually emits.
    """
    from ml.artifact import build_artifact_metadata, serialise_model, train_artifact
    from ml.loading import DEFAULT_DATASET, load_processed_dataset

    dataset = load_processed_dataset(DEFAULT_DATASET)
    trained = train_artifact(dataset, dataset_version="v1", feature_version="v1")
    payload = serialise_model(trained.estimator)

    payload_path = directory / "model.pkl"
    metadata_path = directory / "artifact.json"
    payload_path.write_bytes(payload)
    metadata = build_artifact_metadata(
        trained,
        artifact_sha256=hashlib.sha256(payload).hexdigest(),
        artifact_bytes=len(payload),
        artifact_filename=payload_path.name,
        dataset_path=DEFAULT_DATASET.name,
        validation_sha256="v" * 64,
        registry_sha256="r" * 64,
        created_at=BUILT_AT,
    )
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return ArtifactLocation(payload=payload_path, metadata=metadata_path)


def rewrite(location: ArtifactLocation, directory: Path, **changes: object) -> ArtifactLocation:
    """A copy of *location* with metadata fields edited, for the refusal tests.

    Keys are ``block__key``, or a bare name for a top-level field; a value of ``None`` deletes
    the key. The payload is referenced rather than copied -- the same file, so a metadata-only
    edit is a metadata-only edit and cannot be confused with a payload change.
    """
    metadata = json.loads(location.metadata.read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        block, _, key = dotted.partition("__")
        target = metadata[block] if key else metadata
        name = key or block
        if value is None:
            target.pop(name, None)
        else:
            target[name] = value
    edited = directory / "artifact.json"
    edited.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return ArtifactLocation(payload=location.payload, metadata=edited)


__all__ = ["BUILT_AT", "build_artifact", "rewrite"]
