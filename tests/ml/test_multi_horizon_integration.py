"""Stage 7.14, end to end: re-measure every horizon from its committed dataset.

Offline in every sense: no network, no PostgreSQL, no model loaded from disk. Each horizon's
backtest, leakage re-check, acceptance and fit are run again here exactly as the pipeline runs
them, and the committed records must come back with the same content checksums -- which is what
makes the numbers in ``docs/ml-multi-horizon.md`` reproducible rather than merely recorded.
"""

from __future__ import annotations

import json
from functools import cache

import pytest

from ml.horizons import (
    HORIZONS,
    HorizonSpec,
    canonical_model_digest,
    evaluate_horizon,
    train_horizon_artifact,
)
from ml.loading import load_processed_dataset
from ml.manifests import content_checksum
from ml.pipelines.evaluate_horizons import measure


def horizon_ids(spec: HorizonSpec) -> str:
    return f"h{spec.horizon_days}"


@cache
def measured(spec: HorizonSpec) -> dict[str, dict[str, object]]:
    return measure(spec)


def committed(spec: HorizonSpec, name: str) -> dict[str, object]:
    return json.loads((spec.model_directory / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
@pytest.mark.parametrize("name", ["metrics.json", "validation.json", "registry.json"])
def test_a_fresh_measurement_reproduces_the_committed_record(spec: HorizonSpec, name: str) -> None:
    assert content_checksum(measured(spec)[name]) == content_checksum(committed(spec, name))


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_a_fresh_fit_reproduces_the_committed_canonical_digest(spec: HorizonSpec) -> None:
    artifact = train_horizon_artifact(spec, load_processed_dataset(spec.dataset_path))
    block = committed(spec, "artifact.json")["artifact"]
    assert isinstance(block, dict)
    assert canonical_model_digest(artifact) == block["canonical_model_digest"]


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_the_backtest_evaluates_every_day_after_the_first_origin_exactly_once(
    spec: HorizonSpec,
) -> None:
    result = evaluate_horizon(spec, load_processed_dataset(spec.dataset_path))
    keys = [(p.hotel_key, p.target_date) for p in result.predictions]
    assert len(keys) == len(set(keys)) == 744
    assert result.baseline_pooled.skipped == result.learned_pooled.skipped == 0
    for fold in result.folds:
        assert fold.fold.train_end <= fold.fold.origin < fold.fold.evaluation_start
    assert sum(1 for fold in result.folds if not fold.fold.complete_window) <= 1
