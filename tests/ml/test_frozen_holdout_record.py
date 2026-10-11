"""R2.2: the committed ``frozen_holdout_v1`` record reproduces, and says what it must.

The payload is never committed, so the reproduction rebuilds the approved artifact from the
committed dataset (``tests/artifact_builder``), proves it is the frozen model by its measured
canonical digest, and re-runs the whole evaluation. The rebuilt payload's bytes may differ by
machine; the canonical digest and every number in the record may not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml import frozen_holdout as fh
from ml.loading import REPOSITORY_ROOT
from ml.manifests import content_checksum
from ml.pipelines import evaluate_frozen_holdout as pipeline
from tests.artifact_builder import build_artifact

RECORD_PATH = REPOSITORY_ROOT / "ml" / "evaluations" / "frozen_holdout_v1.json"
REPORT = REPOSITORY_ROOT / "docs" / "ml-frozen-holdout-evaluation.md"


@pytest.fixture(scope="module")
def committed() -> dict[str, object]:
    loaded = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_a_fresh_evaluation_of_a_rebuilt_artifact_reproduces_the_committed_record(
    committed: dict[str, object], tmp_path: Path
) -> None:
    location = build_artifact(tmp_path)
    fresh = pipeline.evaluate(location.payload, location.metadata, require_pinned_payload=False)
    assert content_checksum(fresh) == content_checksum(committed)


def test_the_record_is_stamped_with_its_own_content_checksum(
    committed: dict[str, object],
) -> None:
    generation = committed["generation"]
    assert isinstance(generation, dict)
    assert generation["content_sha256"] == content_checksum(committed)
    assert generation["runs_compared"] == 2
    payload = generation["payload"]
    assert payload["sha256_before"] == payload["sha256_after"] == fh.FROZEN_PAYLOAD_SHA256


def test_the_record_names_the_frozen_protocol_model_and_dataset(
    committed: dict[str, object],
) -> None:
    assert committed["protocol"] == {
        "version": fh.PROTOCOL_VERSION,
        "sha256": fh.PROTOCOL_SHA256,
        "committed_in": pipeline.PROTOCOL_COMMIT,
    }
    model = committed["frozen_model"]
    assert isinstance(model, dict)
    assert model["canonical_model_digest"] == fh.FROZEN_CANONICAL_MODEL_DIGEST
    assert model["refitted"] is False
    assert committed["artifact_identity_check"] == {
        "canonical_model_digest_before": fh.FROZEN_CANONICAL_MODEL_DIGEST,
        "canonical_model_digest_after": fh.FROZEN_CANONICAL_MODEL_DIGEST,
        "unchanged": True,
    }
    dataset = committed["dataset"]
    assert isinstance(dataset, dict)
    assert dataset["sha256"] == fh.DATASET_SHA256


def test_the_record_keeps_the_periods_and_the_experiments_apart(
    committed: dict[str, object],
) -> None:
    holdout = committed["frozen_holdout"]
    cross = committed["cross_hotel"]
    assert isinstance(holdout, dict) and isinstance(cross, dict)
    periods = holdout["periods"]
    assert set(periods) == {"validation", "test"}
    assert periods["validation"]["coverage"]["rows"] == 294
    assert periods["test"]["coverage"]["rows"] == 296
    for period in periods.values():
        assert period["overall"]["paired"]["observations"] == period["coverage"]["rows"]
        assert period["feature_checks"]["lags_checked"] == 3 * period["coverage"]["rows"]
    directions = {(d["train_hotel"], d["evaluate_hotel"]) for d in cross["directions"]}
    assert directions == set(fh.CROSS_HOTEL_DIRECTIONS)
    assert committed["claims"] == dict(fh.CLAIMS)
    assert all(value is False for value in committed["claims"].values())


def test_the_report_quotes_the_committed_record(committed: dict[str, object]) -> None:
    report = REPORT.read_text(encoding="utf-8")
    generation = committed["generation"]
    assert isinstance(generation, dict)
    assert generation["content_sha256"] in report
    assert fh.PROTOCOL_SHA256 in report
    assert pipeline.PROTOCOL_COMMIT in report
    holdout = committed["frozen_holdout"]
    assert isinstance(holdout, dict)
    for period in holdout["periods"].values():
        paired = period["overall"]["paired"]
        assert f"{paired['learned']['mae']:.3f}" in report
        assert f"{paired['baseline']['mae']:.3f}" in report
    assert "same source" in report.lower()
    assert "not established" in report
