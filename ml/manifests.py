"""The evaluation record: what was run, on what, and what it measured.

One file, `ml/models/demand_baseline_v1/metrics.json`, and it is committed. That location is
not invented here -- `ml/README.md` has said since Stage 1 that *"a metric may only be quoted
from a real evaluation run recorded in `models/<model>/metrics.json`"*. Stage 6.3 is the first
stage to produce one, so it is the first stage in which that sentence has anything to point at.

The record carries a **content checksum** over everything except the `generation` block, so
that "the numbers changed" and "it was run again" are answerable separately. A wall clock
inside the checksum would make every rerun look like a new result.

**No model artifact is written.** No pickle, no joblib dump, no weights. Stage 6.3 is offline
measurement; a serialised estimator would be a thing that could be loaded and served, and
nothing in this stage has earned that.
"""

from __future__ import annotations

import datetime as dt
import platform
import sys
from collections.abc import Mapping
from pathlib import Path

import numpy
import scipy
import sklearn

from ml.evaluation import EvaluationResult, comparison, method_names, per_hotel_metrics
from ml.metrics import METRIC_DEFINITIONS
from ml.models import MODEL_NAME, MODEL_VERSION, SEASONAL_NAIVE_LAG_DAYS
from ml.pipelines.offline_demand import serialise_manifest, sha256_hex

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVALUATION_RECORD = REPOSITORY_ROOT / "ml" / "models" / MODEL_VERSION / "metrics.json"

BASELINE_DEFINITION = (
    "seasonal naive at a 7-day period: the forecast for (hotel, date) is the realised room "
    "nights for that hotel 7 days earlier, read from the dataset's own demand_lag_7 column. "
    "Where that observation does not exist the baseline produces no forecast; the day is "
    "counted as skipped and is excluded from every metric rather than filled with a zero, a "
    "mean or a carried-forward value."
)


def build_evaluation_manifest(
    result: EvaluationResult,
    *,
    dataset_version: str,
    feature_version: str,
    dataset_sha256: str,
    dataset_rows: int,
    dataset_path: str,
    generated_at: dt.datetime | None = None,
) -> dict[str, object]:
    """Assemble the record. Everything outside ``generation`` is a function of the inputs."""
    pooled_baseline = result.baseline_pooled
    pooled_learned = result.learned_pooled

    manifest: dict[str, object] = {
        "model": {
            "name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "methods": dict(method_names()),
            "baseline_definition": BASELINE_DEFINITION,
            "baseline_lag_days": SEASONAL_NAIVE_LAG_DAYS,
            "estimator": result.config.as_dict(),
            "random_state": result.config.random_state,
            "deterministic": True,
            "model_artifact_persisted": False,
        },
        "dataset": {
            "dataset_version": dataset_version,
            "feature_version": feature_version,
            "sha256": dataset_sha256,
            "rows": dataset_rows,
            "path": dataset_path,
            "hotels": list(result.dataset_hotels),
        },
        "features": result.selection.as_dict(),
        "protocol": result.policy.as_dict(),
        "metrics": {
            "definitions": dict(METRIC_DEFINITIONS),
            "folds": len(result.folds),
            "skipped_folds": [fold.as_dict() for fold in result.skipped_folds],
            "predictions": len(result.predictions),
            "pooled": {
                "baseline": pooled_baseline.as_dict(),
                "learned": pooled_learned.as_dict(),
                "comparison": comparison(result.predictions),
            },
            "per_hotel": per_hotel_metrics(result.predictions),
            "per_fold": [fold.as_dict() for fold in result.folds],
        },
        "generation": {
            "note": (
                "Wall-clock metadata only, and deliberately outside the content checksum: "
                "re-running the evaluation must not look like a different result."
            ),
            "generated_at": (generated_at or dt.datetime.now(dt.UTC)).isoformat(),
            "pipeline": "ml/pipelines/evaluate_demand_model.py",
            # Which toolchain produced the learned numbers. A compiled tree ensemble is
            # bit-identical for a fixed build -- proven here across thread counts -- but the
            # guarantee does not extend across compilers, so the build is recorded rather than
            # assumed irrelevant. It sits inside `generation`, outside the content checksum,
            # so the checksum stays a statement about the result and not about the machine.
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.system(),
                "scikit_learn": sklearn.__version__,
                "numpy": numpy.__version__,
                "scipy": scipy.__version__,
            },
        },
    }
    return manifest


def content_checksum(manifest: Mapping[str, object]) -> str:
    """sha256 over the record with ``generation`` removed.

    The same two runs of the same code over the same dataset produce the same digest; a rerun
    an hour later produces the same digest too, which is the whole reason the wall clock is
    excluded.
    """
    without_generation = {key: value for key, value in manifest.items() if key != "generation"}
    return sha256_hex(serialise_manifest(without_generation))


def stamp_content_checksum(manifest: dict[str, object]) -> dict[str, object]:
    """Add the digest to the ``generation`` block, where it cannot feed back into itself."""
    digest = content_checksum(manifest)
    generation = manifest.get("generation")
    if isinstance(generation, dict):
        generation["content_sha256"] = digest
    return manifest


def serialise(manifest: Mapping[str, object]) -> bytes:
    """Sorted keys, two-space indent, LF, trailing newline -- the Stage 6.2 spelling."""
    return serialise_manifest(manifest)
