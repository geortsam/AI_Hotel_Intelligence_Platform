"""R2.2: the ``frozen_holdout_v1`` protocol, its leakage checks and its arithmetic.

Synthetic panels only, built through the Stage 6.1 contract (``build_rows``), so a fixture's
lags and calendar columns are right by construction rather than by agreement with the code under
test. Nothing here scores the real validation or test partitions: the protocol was committed
before that happened, and these tests were written with it.

The real artifact appears in two places only -- its identity is rebuilt from the committed
dataset by ``tests/artifact_builder`` (which fits on the train partition and scores nothing),
and its pinned digests are cross-checked against the server's ``APPROVED_MODEL`` and the
committed records, because ``ml/`` may not import the server.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from app.ml.dataset import HotelHistory, build_rows
from app.ml.serving import APPROVED_MODEL
from ml import frozen_holdout as fh
from ml.artifact import LoadedArtifact, load_artifact, load_artifact_metadata
from ml.loading import REPOSITORY_ROOT, ProcessedDataset, ProcessedRow
from ml.manifests import content_checksum
from ml.models import EstimatorConfig, LearnedModel
from ml.pipelines import evaluate_frozen_holdout as pipeline
from ml.pipelines.offline_demand import offline_hotel_id
from tests.artifact_builder import build_artifact

ARTIFACT_JSON = REPOSITORY_ROOT / "ml" / "models" / "demand_baseline_v1" / "artifact.json"
DATASET_MANIFEST = REPOSITORY_ROOT / "ml" / "manifests" / "demand_daily_v1.json"
SYNTHETIC_SHA256 = "s" * 64

#: Long enough that the train partition has complete lag_28 rows to fit on, short enough to
#: fit four small models in about a second.
START = dt.date(2016, 6, 1)
END = dt.date(2017, 8, 31)


# --- fixtures ----------------------------------------------------------------------------------


def _partition(date: dt.date) -> str:
    for period in fh.PERIODS:
        if period.start <= date <= period.end:
            return period.name
    return "train"


def series(days: int, *, base: int, phase: int = 0) -> list[int]:
    """Deterministic: a weekly cycle on a slow trend. No randomness anywhere in these tests."""
    return [base + 25 * ((i + phase) % 7) // 6 + (i // 30) for i in range(days)]


def panel(
    values: Mapping[str, Sequence[int]] | None = None, *, start: dt.date = START
) -> ProcessedDataset:
    days = (END - start).days + 1
    values = values or {
        "city_hotel": series(days, base=120),
        "resort_hotel": series(days, base=80, phase=3),
    }
    histories = []
    keys: dict[uuid.UUID, str] = {}
    for hotel_key, demand_values in values.items():
        demand = {start + dt.timedelta(days=i): value for i, value in enumerate(demand_values)}
        public_id = offline_hotel_id(hotel_key)
        keys[public_id] = hotel_key
        histories.append(
            HotelHistory(
                hotel_public_id=public_id,
                demand_by_date=demand,
                on_books_by_date=dict(demand),
                rooms_existing_by_date={},
            )
        )
    built = build_rows(histories)
    rows = tuple(
        ProcessedRow(
            hotel_key=keys[row.hotel_public_id],
            hotel_public_id=row.hotel_public_id,
            target_date=row.target_date,
            horizon_days=row.horizon_days,
            prediction_cutoff=row.prediction_cutoff,
            partition=_partition(row.target_date),
            target_room_nights=row.target_room_nights,
            features=dict(row.features),
        )
        for row in built
    )
    return ProcessedDataset(
        rows=rows,
        feature_names=tuple(built[0].features),
        sha256=SYNTHETIC_SHA256,
        size_bytes=0,
        path=Path("synthetic.csv"),
    )


def replace_row(dataset: ProcessedDataset, index: int, **changes: object) -> ProcessedDataset:
    rows = list(dataset.rows)
    rows[index] = dataclasses.replace(rows[index], **changes)  # type: ignore[arg-type]
    return dataclasses.replace(dataset, rows=tuple(rows))


def held_out_index(dataset: ProcessedDataset, period: str = "validation") -> int:
    return next(i for i, row in enumerate(dataset.rows) if row.partition == period)


def fitted_forecaster(dataset: ProcessedDataset) -> fh.Forecaster:
    train = [row for row in dataset.rows if row.partition == "train"]
    model = LearnedModel.fit(train, fh.FEATURE_COLUMNS, EstimatorConfig())
    return fh.estimator_forecaster(model.estimator)


def scored(*triples: tuple[int, float | None, float | None]) -> tuple[fh.Scored, ...]:
    """(actual, learned, baseline) triples on consecutive dates of one hotel."""
    hotel = offline_hotel_id("city_hotel")
    out = []
    for i, (actual, learned, baseline) in enumerate(triples):
        date = dt.date(2017, 1, 1) + dt.timedelta(days=i)
        row = ProcessedRow(
            hotel_key="city_hotel",
            hotel_public_id=hotel,
            target_date=date,
            horizon_days=1,
            prediction_cutoff=dt.datetime(2016, 12, 25, tzinfo=dt.UTC),
            partition="validation",
            target_room_nights=actual,
            features={},
        )
        out.append(fh.Scored(row, learned, baseline))
    return tuple(out)


# --- the protocol is pinned, and pinned to the right things -------------------------------------


def test_the_protocol_settings_hash_to_the_committed_checksum() -> None:
    fh.assert_protocol_frozen()
    assert fh.protocol_digest() == fh.PROTOCOL_SHA256


def test_any_change_to_the_protocol_settings_stops_the_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fh, "WEAK_RANGE_BELOW", 41)
    with pytest.raises(fh.FrozenHoldoutError, match="not the frozen protocol"):
        fh.assert_protocol_frozen()


def test_the_pinned_identities_are_the_served_model_and_the_committed_dataset() -> None:
    artifact = json.loads(ARTIFACT_JSON.read_text(encoding="utf-8"))
    manifest = json.loads(DATASET_MANIFEST.read_text(encoding="utf-8"))
    assert APPROVED_MODEL.canonical_model_digest == fh.FROZEN_CANONICAL_MODEL_DIGEST
    assert artifact["artifact"]["canonical_model_digest"] == fh.FROZEN_CANONICAL_MODEL_DIGEST
    assert artifact["artifact"]["sha256"] == fh.FROZEN_PAYLOAD_SHA256
    assert APPROVED_MODEL.model_version == fh.FROZEN_MODEL_VERSION
    assert APPROVED_MODEL.dataset_sha256 == fh.DATASET_SHA256
    assert artifact["dataset"]["dataset_sha256"] == fh.DATASET_SHA256
    assert manifest["dataset"]["processed_sha256"] == fh.DATASET_SHA256
    assert APPROVED_MODEL.feature_columns == fh.FEATURE_COLUMNS
    assert APPROVED_MODEL.forecast_horizon_days == fh.HORIZON_DAYS


def test_the_periods_are_exactly_the_partitions_the_artifact_was_never_fitted_on() -> None:
    artifact = json.loads(ARTIFACT_JSON.read_text(encoding="utf-8"))
    training = artifact["training"]
    assert sorted(training["held_out_partitions"]) == sorted(p.name for p in fh.PERIODS)
    last_fitted = dt.date.fromisoformat(training["training_end_date"])
    assert all(period.start > last_fitted for period in fh.PERIODS)
    validation, test = fh.PERIODS
    assert validation.end + dt.timedelta(days=1) == test.start


def test_the_protocol_makes_no_claim() -> None:
    settings = fh.protocol_settings()
    assert settings["claims"] == dict.fromkeys(fh.CLAIMS, False)
    assert all(value is False for value in fh.CLAIMS.values())
    limitations = " ".join(fh.LIMITATIONS)
    assert "Same source" in limitations
    assert "generalisation" in limitations


# --- inputs are refused unless they are the declared ones ----------------------------------------


def test_a_dataset_with_another_digest_is_refused() -> None:
    with pytest.raises(fh.FrozenHoldoutError, match="hashes to"):
        fh.require_dataset(panel())


def test_a_dataset_with_a_repeated_hotel_day_is_refused() -> None:
    dataset = panel()
    doubled = dataclasses.replace(dataset, rows=(*dataset.rows, dataset.rows[0]))
    with pytest.raises(fh.FrozenHoldoutError, match="more than once"):
        fh.require_dataset(doubled, expected_sha256=SYNTHETIC_SHA256)


def test_a_period_row_outside_its_declared_dates_is_refused() -> None:
    dataset = panel()
    index = next(i for i, row in enumerate(dataset.rows) if row.partition == "train")
    leaked = replace_row(dataset, index, partition="validation")
    with pytest.raises(fh.FrozenHoldoutError, match="fall outside"):
        fh.period_rows(leaked, fh.PERIODS[0])


def test_a_missing_period_is_refused() -> None:
    dataset = panel()
    trimmed = dataclasses.replace(
        dataset, rows=tuple(row for row in dataset.rows if row.partition != "test")
    )
    with pytest.raises(fh.FrozenHoldoutError, match="no 'test' rows"):
        fh.period_rows(trimmed, fh.PERIODS[1])


# --- leakage checks --------------------------------------------------------------------------


def test_a_correct_panel_passes_every_feature_check() -> None:
    dataset = panel()
    rows = fh.period_rows(dataset, fh.PERIODS[0])
    checks = fh.check_features(dataset, rows)
    assert checks == {
        "rows": len(rows),
        "lags_checked": 3 * len(rows),
        "calendar_checked": 6 * len(rows),
    }


def test_a_lag_that_is_not_the_realised_target_it_names_stops_the_evaluation() -> None:
    dataset = panel()
    index = held_out_index(dataset)
    row = dataset.rows[index]
    leaked = replace_row(
        dataset, index, features={**row.features, "demand_lag_7": row.target_room_nights + 1.0}
    )
    with pytest.raises(fh.FrozenHoldoutError, match="demand_lag_7"):
        fh.check_features(leaked, [leaked.rows[index]])


def test_a_lag_whose_source_date_is_not_in_the_dataset_stops_the_evaluation() -> None:
    dataset = panel()
    index = held_out_index(dataset)
    row = dataset.rows[index]
    source = row.target_date - dt.timedelta(days=28)
    without_source = dataclasses.replace(
        dataset,
        rows=tuple(
            r
            for r in dataset.rows
            if not (r.hotel_key == row.hotel_key and r.target_date == source)
        ),
    )
    with pytest.raises(fh.FrozenHoldoutError, match="cannot be checked"):
        fh.check_features(without_source, [row])


def test_a_calendar_feature_that_disagrees_with_its_date_stops_the_evaluation() -> None:
    dataset = panel()
    index = held_out_index(dataset)
    row = dataset.rows[index]
    wrong = (row.features["day_of_week"] or 0.0) + 1.0
    tampered = replace_row(dataset, index, features={**row.features, "day_of_week": wrong})
    with pytest.raises(fh.FrozenHoldoutError, match="day_of_week"):
        fh.check_features(tampered, [tampered.rows[index]])


def test_an_absent_lag_is_skipped_by_the_check_and_never_invented() -> None:
    dataset = panel()
    index = held_out_index(dataset)
    row = dataset.rows[index]
    absent = replace_row(dataset, index, features={**row.features, "demand_lag_28": None})
    checks = fh.check_features(absent, [absent.rows[index]])
    assert checks["lags_checked"] == 2


# --- the comparison arithmetic, by hand -----------------------------------------------------------


def test_wins_losses_and_ties_are_counted_on_absolute_errors() -> None:
    result = fh.compare(
        scored(
            (100, 98.0, 90.0),  # learned error 2 < baseline 10: win
            (100, 110.0, 105.0),  # 10 > 5: loss
            (100, 104.0, 96.0),  # 4 == 4: tie
            (100, 100.0, 101.0),  # 0 < 1: win
        )
    )
    paired = result["paired"]
    assert isinstance(paired, dict)
    assert (paired["wins"], paired["losses"], paired["ties"]) == (2, 1, 1)
    assert paired["win_proportion"] == 0.5
    learned_mae = (2 + 10 + 4 + 0) / 4
    baseline_mae = (10 + 5 + 4 + 1) / 4
    assert paired["learned"]["mae"] == pytest.approx(learned_mae)
    assert paired["baseline"]["mae"] == pytest.approx(baseline_mae)
    assert paired["difference_learned_minus_baseline"]["mae"] == pytest.approx(
        learned_mae - baseline_mae
    )
    assert paired["learned"]["rmse"] == pytest.approx(((4 + 100 + 16 + 0) / 4) ** 0.5)


def test_a_row_one_method_cannot_forecast_is_counted_skipped_and_left_out_of_the_pair() -> None:
    result = fh.compare(scored((100, 98.0, None), (100, None, 90.0), (100, 101.0, 102.0)))
    learned = result["learned"]
    baseline = result["baseline"]
    paired = result["paired"]
    assert isinstance(learned, dict) and isinstance(baseline, dict) and isinstance(paired, dict)
    assert (learned["observations"], learned["skipped"]) == (2, 1)
    assert (baseline["observations"], baseline["skipped"]) == (2, 1)
    assert paired["observations"] == 1
    assert paired["learned"]["mae"] == 1.0
    assert paired["win_proportion"] == 1.0


def test_an_empty_pair_reports_no_metric_and_no_proportion() -> None:
    result = fh.compare(scored((100, None, 90.0)))
    paired = result["paired"]
    assert isinstance(paired, dict)
    assert paired["observations"] == 0
    assert paired["win_proportion"] is None
    assert paired["learned"]["mae"] is None
    assert paired["difference_learned_minus_baseline"]["mae"] is None


def test_smape_on_a_zero_actual_and_zero_forecast_contributes_zero() -> None:
    result = fh.compare(scored((0, 0.0, 0.0), (100, 50.0, 100.0)))
    paired = result["paired"]
    assert isinstance(paired, dict)
    # sMAPE row 1: 0 (zero denominator); row 2: 200*|50|/150 for learned, 0 for baseline.
    assert paired["learned"]["smape"] == pytest.approx((0.0 + 200 * 50 / 150) / 2, abs=1e-6)
    assert paired["baseline"]["smape"] == 0.0


def test_a_group_below_the_minimum_reports_counts_and_no_metric() -> None:
    small = scored(*[(100, 99.0, 98.0)] * (fh.MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC - 1))
    enough = scored(*[(100, 99.0, 98.0)] * fh.MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC)
    below = fh.breakdowns(small)["by_hotel"]
    at = fh.breakdowns(enough)["by_hotel"]
    assert isinstance(below, dict) and isinstance(at, dict)
    assert below["city_hotel"]["sufficient_coverage"] is False
    assert "paired" not in below["city_hotel"]
    assert below["city_hotel"]["paired_observations"] == 29
    assert at["city_hotel"]["sufficient_coverage"] is True
    assert at["city_hotel"]["paired"]["observations"] == 30


def test_the_weak_range_is_selected_by_the_actual_target() -> None:
    triples = [(39, 50.0, 40.0)] * 30 + [(40, 50.0, 40.0)] * 5
    weak = fh.breakdowns(scored(*triples))[f"actual_below_{fh.WEAK_RANGE_BELOW}"]
    assert isinstance(weak, dict)
    assert weak["paired"]["observations"] == 30


def test_a_non_finite_forecast_stops_the_evaluation() -> None:
    rows = [s.row for s in scored((100, 1.0, 1.0))]

    def broken(batch: Sequence[ProcessedRow]) -> tuple[float | None, ...]:
        return tuple(float("nan") for _ in batch)

    with pytest.raises(fh.FrozenHoldoutError, match="not a finite number"):
        fh.score(rows, broken)


# --- forecasters ------------------------------------------------------------------------------


def _loaded(estimator: object, **changes: object) -> LoadedArtifact:
    artifact = LoadedArtifact(
        metadata={},
        estimator=estimator,
        model_version=fh.FROZEN_MODEL_VERSION,
        feature_columns=fh.FEATURE_COLUMNS,
        forecast_horizon_days=fh.HORIZON_DAYS,
        feature_version="v1",
        dataset_sha256=SYNTHETIC_SHA256,
        canonical_model_digest="c" * 64,
    )
    return dataclasses.replace(artifact, **changes)  # type: ignore[arg-type]


def test_the_serving_path_and_the_estimator_give_identical_forecasts() -> None:
    dataset = panel()
    train = [row for row in dataset.rows if row.partition == "train"]
    model = LearnedModel.fit(train, fh.FEATURE_COLUMNS, EstimatorConfig())
    rows = fh.period_rows(dataset, fh.PERIODS[1])
    absent = dataclasses.replace(rows[0], features={**rows[0].features, "demand_lag_14": None})
    batch = (absent, *rows[1:])
    served = fh.served_forecaster(_loaded(model.estimator))(batch)
    direct = fh.estimator_forecaster(model.estimator)(batch)
    assert served == direct
    assert served[0] is None
    assert all(value is not None for value in served[1:])


def test_the_serving_path_refuses_an_artifact_with_other_columns_or_horizon() -> None:
    with pytest.raises(fh.FrozenHoldoutError, match="columns"):
        fh.served_forecaster(_loaded(None, feature_columns=fh.FEATURE_COLUMNS[::-1]))
    with pytest.raises(fh.FrozenHoldoutError, match="days ahead"):
        fh.served_forecaster(_loaded(None, forecast_horizon_days=14))


# --- the frozen model's identity -------------------------------------------------------------


def test_a_rebuilt_approved_artifact_measures_the_frozen_canonical_digest(tmp_path: Path) -> None:
    location = build_artifact(tmp_path)
    record = load_artifact_metadata(location.metadata)
    loaded = load_artifact(location.payload, location.metadata)
    assert fh.require_frozen_model(loaded.estimator, record) == fh.FROZEN_CANONICAL_MODEL_DIGEST
    assert (
        fh.measured_canonical_digest(loaded.estimator, record)
        == (
            record["artifact"]["canonical_model_digest"]  # type: ignore[index]
        )
    )


def test_a_different_model_is_refused_even_under_the_frozen_record() -> None:
    record = json.loads(ARTIFACT_JSON.read_text(encoding="utf-8"))
    dataset = panel()
    train = [row for row in dataset.rows if row.partition == "train"]
    imposter = LearnedModel.fit(train, fh.FEATURE_COLUMNS, EstimatorConfig()).estimator
    with pytest.raises(fh.FrozenHoldoutError, match="not the served model"):
        fh.require_frozen_model(imposter, record)


def test_a_record_naming_another_digest_is_refused() -> None:
    record = json.loads(ARTIFACT_JSON.read_text(encoding="utf-8"))
    record["artifact"]["canonical_model_digest"] = "0" * 64
    with pytest.raises(fh.FrozenHoldoutError, match="names canonical digest"):
        fh.require_frozen_model(object(), record)


def test_the_pipeline_refuses_a_payload_that_is_not_the_pinned_one_before_unpickling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "model.pkl"
    payload.write_bytes(b"not a pickle, and never unpickled")
    unpickled: list[object] = []
    monkeypatch.setattr(pipeline, "load_artifact", lambda *a, **k: unpickled.append(a))
    with pytest.raises(fh.FrozenHoldoutError, match="the protocol pins"):
        pipeline.evaluate(payload, ARTIFACT_JSON)
    assert unpickled == []
    assert hashlib.sha256(payload.read_bytes()).hexdigest() != fh.FROZEN_PAYLOAD_SHA256


# --- the cross-hotel experiment ---------------------------------------------------------------


def test_cross_hotel_training_rows_are_one_hotels_train_partition_only() -> None:
    dataset = panel()
    rows = fh.cross_hotel_training_rows(dataset, "city_hotel")
    assert {row.hotel_key for row in rows} == {"city_hotel"}
    assert {row.partition for row in rows} == {"train"}
    assert max(row.target_date for row in rows) < fh.PERIODS[0].start
    assert [row.target_date for row in rows] == sorted(row.target_date for row in rows)


def test_a_cross_hotel_direction_needs_two_hotels() -> None:
    with pytest.raises(fh.FrozenHoldoutError, match="two different hotels"):
        fh.cross_hotel_direction(panel(), "city_hotel", "city_hotel")


def test_a_cross_hotel_direction_scores_only_the_other_hotel() -> None:
    result = fh.cross_hotel_direction(panel(), "resort_hotel", "city_hotel")
    assert result["train_hotel"] == "resort_hotel"
    periods = result["periods"]
    assert isinstance(periods, dict)
    for name in ("validation", "test"):
        assert periods[name]["coverage"]["hotels"] == ["city_hotel"]
    training = result["training"]
    assert isinstance(training, dict)
    assert training["fitted_rows"] < training["partition_rows"]


# --- the record --------------------------------------------------------------------------------


def _record(dataset: ProcessedDataset) -> dict[str, object]:
    return fh.build_record(
        dataset,
        fitted_forecaster(dataset),
        model_identity={"model_version": "synthetic", "refitted": False},
        expected_dataset_sha256=SYNTHETIC_SHA256,
    )


def test_the_record_is_deterministic_and_complete() -> None:
    dataset = panel()
    first = _record(dataset)
    second = _record(dataset)
    assert content_checksum(first) == content_checksum(second)
    assert first["protocol"] == {"version": fh.PROTOCOL_VERSION, "sha256": fh.PROTOCOL_SHA256}
    holdout = first["frozen_holdout"]
    cross = first["cross_hotel"]
    assert isinstance(holdout, dict) and isinstance(cross, dict)
    assert set(holdout["periods"]) == {"validation", "test"}
    assert "not a pristine hold-out" in holdout["label"]
    assert "not the served artifact" in cross["label"]
    directions = [(d["train_hotel"], d["evaluate_hotel"]) for d in cross["directions"]]
    assert directions == list(fh.CROSS_HOTEL_DIRECTIONS)
    assert first["claims"] == dict(fh.CLAIMS)
    assert "generation" not in first
    for period in holdout["periods"].values():
        assert set(period["breakdowns"]) == {"by_hotel", "by_month", "actual_below_40"}


def test_the_record_refuses_to_build_under_a_changed_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fh, "MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC", 10)
    with pytest.raises(fh.FrozenHoldoutError, match="not the frozen protocol"):
        _record(panel())
