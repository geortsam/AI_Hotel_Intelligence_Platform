"""R2.2b: the committed ``hotel_transfer_v1`` record reproduces, and says what it must.

The experiment needs only the committed dataset, so the reproduction is a plain re-run: four
single-hotel fits, twice, and the content checksum must equal the committed one exactly.
"""

from __future__ import annotations

import json

import pytest

from ml import hotel_transfer as ht
from ml.frozen_holdout import DATASET_SHA256
from ml.loading import REPOSITORY_ROOT
from ml.manifests import content_checksum
from ml.pipelines import evaluate_hotel_transfer as pipeline

RECORD_PATH = REPOSITORY_ROOT / "ml" / "evaluations" / "hotel_transfer_v1.json"
FROZEN_RECORD_PATH = REPOSITORY_ROOT / "ml" / "evaluations" / "frozen_holdout_v1.json"
REPORT = REPOSITORY_ROOT / "docs" / "ml-hotel-transfer-diagnostic.md"


@pytest.fixture(scope="module")
def committed() -> dict[str, object]:
    loaded = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def experiments(record: dict[str, object]) -> dict[str, dict[str, object]]:
    listed = record["experiments"]
    assert isinstance(listed, list)
    return {e["name"]: e for e in listed}


def test_a_fresh_run_reproduces_the_committed_record(committed: dict[str, object]) -> None:
    fresh = pipeline.evaluate()
    assert content_checksum(fresh) == content_checksum(committed)


def test_the_record_names_its_protocol_dataset_and_provenance(
    committed: dict[str, object],
) -> None:
    assert committed["protocol"] == {
        "version": ht.PROTOCOL_VERSION,
        "sha256": ht.PROTOCOL_SHA256,
        "committed_in": pipeline.PROTOCOL_COMMIT,
    }
    dataset = committed["dataset"]
    assert isinstance(dataset, dict)
    assert dataset["sha256"] == DATASET_SHA256
    generation = committed["generation"]
    assert isinstance(generation, dict)
    assert generation["content_sha256"] == content_checksum(committed)
    assert generation["runs_compared"] == 2
    assert generation["served_artifact_files_unchanged"] is True
    assert committed["claims"] == dict(ht.CLAIMS)


def test_every_experiment_reports_both_periods_on_fully_paired_rows(
    committed: dict[str, object],
) -> None:
    by_name = experiments(committed)
    assert list(by_name) == [e.name for e in ht.EXPERIMENTS]
    for experiment in by_name.values():
        training = experiment["training"]
        assert isinstance(training, dict)
        assert training["fitted_rows"] + training["skipped_rows"] == training["partition_rows"]
        periods = experiment["periods"]
        assert isinstance(periods, dict) and set(periods) == {"validation", "test"}
        for period in periods.values():
            overall = period["overall"]
            rows = period["coverage"]["rows"]
            assert period["excluded_rows"] == 0
            assert overall["paired"]["observations"] == rows
            assert overall["diagnostics"]["learned"]["paired_observations"] == rows
            assert overall["diagnostics"]["baseline"]["paired_observations"] == rows


def test_the_controls_share_rows_and_baseline_with_the_r2_2_evaluation(
    committed: dict[str, object],
) -> None:
    """Same rows, same baseline values: the evaluated hotel's naive metrics must equal R2.2's."""
    frozen = json.loads(FROZEN_RECORD_PATH.read_text(encoding="utf-8"))
    by_name = experiments(committed)
    for name, hotel in (
        ("city_hotel->city_hotel", "city_hotel"),
        ("resort_hotel->resort_hotel", "resort_hotel"),
        ("resort_hotel->city_hotel", "city_hotel"),
        ("city_hotel->resort_hotel", "resort_hotel"),
    ):
        for period in ("validation", "test"):
            ours = by_name[name]["periods"][period]["overall"]["paired"]["baseline"]  # type: ignore[index]
            theirs = frozen["frozen_holdout"]["periods"][period]["breakdowns"]["by_hotel"][hotel][
                "paired"
            ]["baseline"]
            assert ours == theirs


def test_the_report_quotes_the_committed_record(committed: dict[str, object]) -> None:
    report = REPORT.read_text(encoding="utf-8")
    generation = committed["generation"]
    assert isinstance(generation, dict)
    assert generation["content_sha256"] in report
    assert ht.PROTOCOL_SHA256 in report
    assert pipeline.PROTOCOL_COMMIT in report
    for experiment in experiments(committed).values():
        for period in experiment["periods"].values():  # type: ignore[attr-defined]
            learned = period["overall"]["diagnostics"]["learned"]
            paired = period["overall"]["paired"]
            assert f"{paired['learned']['mae']:.3f}" in report
            assert f"{learned['mean_signed_error']:+.3f}".replace("-", "\N{MINUS SIGN}") in report
    assert "not capacity" in report
