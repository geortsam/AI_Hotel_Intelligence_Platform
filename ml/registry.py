"""The offline model registry: metadata about a candidate, and nothing that could serve it.

A registry entry answers "what exactly was measured, against what, under which rules, and what
did the policy say about it". It contains **no weights, no serialised estimator and no loading
path** -- the configuration is recorded so the model can be rebuilt, not restored, and a later
stage that wanted to serve it would have to fit it again and say so.

Three checksums, each answering a different question:

* ``protocol.sha256``   -- has the rolling-origin protocol changed since Stage 6.3?
* ``model.configuration_sha256`` -- have the hyper-parameters changed since Stage 6.3?
* ``acceptance.policy_sha256``   -- were the rules the same rules?

and one over the record as a whole, taken with ``generation`` removed, so that re-running the
validation does not look like a different result.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

from ml.evaluation import EvaluationResult, comparison, per_hotel_metrics
from ml.manifests import serialise
from ml.metrics import METRIC_DEFINITIONS
from ml.models import BASELINE_METHOD, LEARNED_METHOD, MODEL_NAME, MODEL_VERSION
from ml.pipelines.offline_demand import sha256_hex
from ml.policy import AcceptanceResult
from ml.validation import VALIDATION_VERSION

#: Bump when the shape of a registry entry changes.
REGISTRY_VERSION = "registry_v1"

#: The Stage 6.3 rolling-origin protocol, named. The label is not the guarantee -- the checksum
#: beside it is, and it is taken over the protocol's own settings.
ROLLING_ORIGIN_PROTOCOL_VERSION = "rolling_origin_v1"

STATUS_OFFLINE_CANDIDATE = "offline_research_candidate"


def protocol_checksum(settings: Mapping[str, object]) -> str:
    return sha256_hex(serialise(dict(sorted(settings.items()))))


def configuration_checksum(configuration: Mapping[str, object]) -> str:
    return sha256_hex(serialise(dict(sorted(configuration.items()))))


def build_registry_record(
    result: EvaluationResult,
    acceptance: AcceptanceResult,
    *,
    dataset_version: str,
    feature_version: str,
    dataset_sha256: str,
    dataset_rows: int,
    dataset_path: str,
    deterministic: bool,
    leakage_passed: bool,
    evaluation_record_sha256: str,
    validation_sha256: str,
    created_at: dt.datetime | None = None,
) -> dict[str, object]:
    """One registry entry. Everything outside ``generation`` is a function of the inputs."""
    protocol_settings = result.policy.as_dict()
    estimator = result.config.as_dict()
    train_start = min(fold.fold.train_start for fold in result.folds)
    train_end = max(fold.fold.train_end for fold in result.folds)
    evaluation_start = min(fold.fold.evaluation_start for fold in result.folds)
    evaluation_end = max(fold.fold.evaluation_end for fold in result.folds)

    return {
        "registry_version": REGISTRY_VERSION,
        "model": {
            "model_version": MODEL_VERSION,
            "model_name": MODEL_NAME,
            "status": STATUS_OFFLINE_CANDIDATE,
            "status_note": (
                "offline research candidate. Not a production forecasting model: nothing is "
                "served, no artifact is persisted, and no endpoint loads one."
            ),
            "methods": {"baseline": BASELINE_METHOD, "learned": LEARNED_METHOD},
            "configuration": estimator,
            "configuration_sha256": configuration_checksum(estimator),
            "features": result.selection.as_dict(),
            "forecast_horizon_days": result.policy.horizon_days,
            "artifact_persisted": False,
            "serving_path": None,
        },
        "dataset": {
            "dataset_version": dataset_version,
            "feature_version": feature_version,
            "dataset_sha256": dataset_sha256,
            "rows": dataset_rows,
            "path": dataset_path,
            "hotels": list(result.dataset_hotels),
        },
        "protocol": {
            "protocol_version": ROLLING_ORIGIN_PROTOCOL_VERSION,
            "sha256": protocol_checksum(protocol_settings),
            "settings": protocol_settings,
            "folds": len(result.folds),
            "skipped_folds": len(result.skipped_folds),
            "paired_observations": len(result.predictions),
            "training_data_range": {
                "start": train_start.isoformat(),
                "end": train_end.isoformat(),
            },
            "evaluation_data_range": {
                "start": evaluation_start.isoformat(),
                "end": evaluation_end.isoformat(),
            },
        },
        "metrics": {
            "definitions": dict(METRIC_DEFINITIONS),
            "pooled": {
                "baseline": result.baseline_pooled.as_dict(),
                "learned": result.learned_pooled.as_dict(),
                "comparison": comparison(result.predictions),
            },
            "per_hotel": per_hotel_metrics(result.predictions),
        },
        "verification": {
            "deterministic": deterministic,
            "leakage_checks_passed": leakage_passed,
            "validation_version": VALIDATION_VERSION,
            "validation_sha256": validation_sha256,
            "evaluation_record_sha256": evaluation_record_sha256,
        },
        "acceptance": acceptance.as_dict(),
        "claims": {
            "production_ready": False,
            "production_accuracy_established": False,
            "cross_hotel_generalisation_established": False,
            "note": (
                "Measured on two Portuguese hotels observed 2015-2017 by a third party. Both "
                "appear in the training data at every origin, so no held-out-hotel claim is "
                "made or possible. See docs/ml-model-card.md."
            ),
        },
        "generation": {
            "note": (
                "Wall-clock metadata only, and outside the content checksum: re-running the "
                "validation must not look like a different registry entry."
            ),
            "created_at": (created_at or dt.datetime.now(dt.UTC)).isoformat(),
            "pipeline": "ml/pipelines/validate_demand_model.py",
        },
    }
