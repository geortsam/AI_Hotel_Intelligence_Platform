"""The Stage 6.3 baseline, backtest and metrics, checked against hand-computed answers.

Nothing here reads the real dataset and nothing downloads anything. The panels are built by
calling the **Stage 6.1 contract itself** -- ``HotelHistory`` and ``build_rows`` -- over a short
demand series, so the lags and rolling means in a fixture are correct by construction rather
than by the test author agreeing with themselves.

The leakage tests are the ones to read first. Each mutates the future and requires the past to
come back unchanged, and two of them require it **byte-identical**: a backtest that leaks does
not fail, it flatters, and the only defence is an assertion that would notice.

Metric arithmetic is checked against values computed by hand in the test body, because a metric
verified against its own implementation verifies nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from app.ml.dataset import HotelHistory, build_rows
from ml.evaluation import (
    MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC,
    EvaluationError,
    EvaluationResult,
    Prediction,
    RollingOriginPolicy,
    assert_fold_is_chronological,
    comparison,
    evaluate,
    per_hotel_metrics,
    pooled_metrics,
    rolling_origins,
)
from ml.loading import (
    DatasetLoadError,
    ProcessedRow,
    dataset_versions,
    parse_processed_csv,
    require_versions,
    rows_by_hotel_and_date,
    verify_against_manifest,
)
from ml.manifests import (
    BASELINE_DEFINITION,
    build_evaluation_manifest,
    content_checksum,
    serialise,
    stamp_content_checksum,
)
from ml.metrics import (
    METRIC_DEFINITIONS,
    MetricSet,
    difference,
    mean_absolute_error,
    metric_set,
    pooled,
    root_mean_squared_error,
    symmetric_mean_absolute_percentage_error,
)
from ml.models import (
    CALENDAR_FEATURES,
    MODEL_VERSION,
    OFFLINE_UNAVAILABLE_FEATURES,
    SEASONAL_NAIVE_LAG_DAYS,
    EstimatorConfig,
    LearnedModel,
    ModelContractError,
    design_matrix,
    feature_lead_days,
    seasonal_naive_prediction,
    seasonal_naive_predictions,
    select_model_features,
)
from ml.pipelines.offline_demand import offline_hotel_id

START = dt.date(2016, 1, 1)

#: Short enough to backtest in a second, long enough that `demand_lag_28` exists before the
#: first origin -- otherwise no training row would have a complete feature vector and every
#: fold would refuse to fit. Every number is passed explicitly so that no test silently depends
#: on a production default.
FAST_POLICY = RollingOriginPolicy(horizon_days=2, minimum_train_days=60, step_days=2)


# --- fixtures ----------------------------------------------------------------------------------


def panel(
    series: Mapping[str, Sequence[int]], *, start: dt.date = START
) -> tuple[ProcessedRow, ...]:
    """A processed panel built through the Stage 6.1 contract.

    Lags and rolling means are produced by ``build_rows``, not by this helper, so a fixture
    cannot disagree with the definitions the pipeline uses. ``on_books`` is set equal to
    realised demand purely so the column is populated; no test below depends on its value.
    """
    histories = []
    keys: dict[uuid.UUID, str] = {}
    for hotel_key, values in series.items():
        demand = {start + dt.timedelta(days=i): value for i, value in enumerate(values)}
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
    rows = build_rows(histories)
    return tuple(
        ProcessedRow(
            hotel_key=keys[row.hotel_public_id],
            hotel_public_id=row.hotel_public_id,
            target_date=row.target_date,
            horizon_days=row.horizon_days,
            prediction_cutoff=row.prediction_cutoff,
            partition="train",
            target_room_nights=row.target_room_nights,
            features=dict(row.features),
        )
        for row in rows
    )


def wave(days: int, *, base: int = 100, amplitude: int = 20, phase: int = 0) -> list[int]:
    """A deterministic weekly-periodic series. No randomness anywhere in these tests."""
    return [base + amplitude * ((i + phase) % 7) // 6 + (i // 30) for i in range(days)]


DATASET_FEATURES: tuple[str, ...] = (
    *CALENDAR_FEATURES,
    "demand_lag_1",
    "demand_lag_7",
    "demand_lag_14",
    "demand_lag_28",
    "demand_rolling_mean_7",
    "demand_rolling_mean_14",
    "demand_rolling_mean_28",
    "on_books_room_nights_at_cutoff",
    "rooms_existing_at_cutoff",
)


def prediction(
    *,
    fold: int = 0,
    hotel: str = "city_hotel",
    day: int = 0,
    actual: int,
    baseline: float | None,
    learned: float | None,
) -> Prediction:
    return Prediction(
        fold_index=fold,
        hotel_key=hotel,
        target_date=START + dt.timedelta(days=day),
        actual=actual,
        baseline=baseline,
        learned=learned,
    )


# --- 1. dataset checksum / version compatibility -------------------------------------------------


CSV = (
    "hotel_key,hotel_public_id,target_date,horizon_days,prediction_cutoff,partition,"
    "target_room_nights,day_of_week,demand_lag_7\n"
    "city_hotel,c7fb00b8-6205-5d73-95ad-5f774926f2cc,2016-01-01,1,"
    "2016-01-01T00:00:00+00:00,train,10,4,\n"
    "city_hotel,c7fb00b8-6205-5d73-95ad-5f774926f2cc,2016-01-02,1,"
    "2016-01-02T00:00:00+00:00,train,12,5,10\n"
)


def manifest_for(payload: bytes, **overrides: object) -> dict[str, object]:
    dataset = parse_processed_csv(payload)
    block: dict[str, object] = {
        "processed_sha256": dataset.sha256,
        "processed_bytes": dataset.size_bytes,
        "rows": len(dataset.rows),
        "hotels": len(dataset.hotel_keys),
        "date_min": dataset.dates[0].isoformat(),
        "date_max": dataset.dates[-1].isoformat(),
        "dataset_version": "v1",
        "feature_version": "v1",
        "columns": [
            "hotel_key",
            "hotel_public_id",
            "target_date",
            "horizon_days",
            "prediction_cutoff",
            "partition",
            "target_room_nights",
            "day_of_week",
            "demand_lag_7",
        ],
    }
    block.update(overrides)
    return {"dataset": block}


def test_a_matching_dataset_and_manifest_verify() -> None:
    payload = CSV.encode("utf-8")
    verify_against_manifest(parse_processed_csv(payload), manifest_for(payload))


def test_a_changed_byte_is_caught_by_the_checksum() -> None:
    payload = CSV.encode("utf-8")
    manifest = manifest_for(payload)
    tampered = CSV.replace("train,10,4", "train,11,4").encode("utf-8")
    with pytest.raises(DatasetLoadError, match="processed_sha256"):
        verify_against_manifest(parse_processed_csv(tampered), manifest)


def test_a_changed_row_count_is_named_rather_than_only_hashed() -> None:
    payload = CSV.encode("utf-8")
    manifest = manifest_for(payload, rows=99, processed_sha256=parse_processed_csv(payload).sha256)
    with pytest.raises(DatasetLoadError, match="rows"):
        verify_against_manifest(parse_processed_csv(payload), manifest)


def test_a_different_dataset_version_is_refused_rather_than_substituted() -> None:
    manifest = manifest_for(CSV.encode("utf-8"), dataset_version="v2")
    assert dataset_versions(manifest) == ("v2", "v1")
    with pytest.raises(DatasetLoadError, match="must not be substituted silently"):
        require_versions(manifest, dataset_version="v1", feature_version="v1")


def test_an_absent_feature_parses_as_none_and_never_as_zero() -> None:
    dataset = parse_processed_csv(CSV.encode("utf-8"))
    assert dataset.rows[0].features["demand_lag_7"] is None
    assert dataset.rows[1].features["demand_lag_7"] == 10.0


def test_a_file_missing_an_identity_column_is_refused() -> None:
    broken = CSV.replace("target_room_nights,", "nights,")
    with pytest.raises(DatasetLoadError, match="target_room_nights"):
        parse_processed_csv(broken.encode("utf-8"))


def test_a_duplicate_hotel_day_is_refused_by_the_index() -> None:
    rows = panel({"city_hotel": wave(12)})
    with pytest.raises(DatasetLoadError, match="duplicate observation"):
        rows_by_hotel_and_date([*rows, rows[0]])


# --- 2. explicit feature selection ---------------------------------------------------------------


def test_the_seven_day_horizon_admits_exactly_nine_features() -> None:
    selection = select_model_features(DATASET_FEATURES, horizon_days=7)
    assert selection.selected == (
        "day_of_week",
        "day_of_month",
        "month",
        "week_of_year",
        "day_of_year",
        "is_weekend",
        "demand_lag_7",
        "demand_lag_14",
        "demand_lag_28",
    )


def test_a_one_day_horizon_admits_everything_except_the_unavailable_column() -> None:
    selection = select_model_features(DATASET_FEATURES, horizon_days=1)
    assert "demand_lag_1" in selection.selected
    assert "demand_rolling_mean_7" in selection.selected
    assert "on_books_room_nights_at_cutoff" in selection.selected
    assert "rooms_existing_at_cutoff" not in selection.selected
    assert len(selection.selected) == len(DATASET_FEATURES) - 1


def test_selection_order_follows_the_dataset_column_order() -> None:
    selection = select_model_features(DATASET_FEATURES, horizon_days=7)
    positions = [DATASET_FEATURES.index(name) for name in selection.selected]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    ("name", "lead"),
    [
        ("day_of_week", None),
        ("is_weekend", None),
        ("demand_lag_1", 1),
        ("demand_lag_7", 7),
        ("demand_lag_28", 28),
        ("demand_rolling_mean_7", 1),
        ("on_books_room_nights_at_cutoff", 1),
        ("rooms_existing_at_cutoff", 1),
    ],
)
def test_every_contract_feature_has_a_recorded_lead_time(name: str, lead: int | None) -> None:
    assert feature_lead_days(name) == lead


def test_an_unrecognised_feature_is_refused_rather_than_admitted() -> None:
    with pytest.raises(ModelContractError, match="no lead time is recorded"):
        feature_lead_days("competitor_price")


def test_a_horizon_below_one_day_is_refused() -> None:
    with pytest.raises(ModelContractError, match="at least 1"):
        select_model_features(DATASET_FEATURES, horizon_days=0)


def test_a_horizon_no_feature_survives_is_refused() -> None:
    with pytest.raises(ModelContractError, match="knowable"):
        select_model_features(("demand_lag_1", "rooms_existing_at_cutoff"), horizon_days=7)


# --- 3. the unavailable capacity feature is never fabricated --------------------------------------


@pytest.mark.parametrize("horizon", [1, 3, 7, 28])
def test_capacity_is_excluded_at_every_horizon_and_for_its_own_reason(horizon: int) -> None:
    selection = select_model_features(DATASET_FEATURES, horizon_days=horizon)
    assert "rooms_existing_at_cutoff" not in selection.selected
    reasons = dict(selection.excluded)
    assert "room inventory" in reasons["rooms_existing_at_cutoff"]
    assert (
        reasons["rooms_existing_at_cutoff"]
        == OFFLINE_UNAVAILABLE_FEATURES["rooms_existing_at_cutoff"]
    )


def test_capacity_never_reaches_the_design_matrix() -> None:
    rows = panel({"city_hotel": wave(40)})
    selection = select_model_features(DATASET_FEATURES, horizon_days=7)
    matrix = design_matrix(rows, selection.selected)
    assert len(selection.selected) == 9
    assert all(len(values) == 9 for values in matrix.features)


def test_a_row_with_a_missing_selected_feature_is_held_out_not_imputed() -> None:
    rows = panel({"city_hotel": wave(40)})
    matrix = design_matrix(rows, ("day_of_week", "demand_lag_28"))
    # The first 28 dates have no lag_28; they are skipped, not zero-filled.
    assert len(matrix.skipped) == 28
    assert len(matrix.used) == len(rows) - 28
    assert all(math.isfinite(value) for values in matrix.features for value in values)


# --- 4./5. seasonal-naive baseline ----------------------------------------------------------------


def test_the_baseline_is_realised_demand_seven_days_earlier() -> None:
    values = list(range(200, 240))
    rows = panel({"city_hotel": values})
    index = {row.target_date: row for row in rows}
    day = START + dt.timedelta(days=20)
    assert seasonal_naive_prediction(index[day], horizon_days=7) == float(values[13])


def test_the_baseline_agrees_with_an_independently_reconstructed_history() -> None:
    """Guards the one shortcut taken: the baseline reads the dataset's own lag column."""
    values = wave(40)
    rows = panel({"city_hotel": values})
    for row in rows:
        earlier = row.target_date - dt.timedelta(days=SEASONAL_NAIVE_LAG_DAYS)
        offset = (earlier - START).days
        expected = float(values[offset]) if 0 <= offset < len(values) else None
        assert seasonal_naive_prediction(row, horizon_days=7) == expected


def test_the_baseline_declines_rather_than_inventing_a_value() -> None:
    rows = panel({"city_hotel": wave(12)})
    first_week = [r for r in rows if r.target_date < START + dt.timedelta(days=7)]
    assert len(first_week) == 7
    assert all(seasonal_naive_prediction(r, horizon_days=7) is None for r in first_week)
    assert seasonal_naive_predictions(first_week, horizon_days=7) == (None,) * 7


def test_the_batch_baseline_agrees_with_the_single_row_form() -> None:
    rows = panel({"city_hotel": wave(40)})
    assert seasonal_naive_predictions(rows, horizon_days=7) == tuple(
        seasonal_naive_prediction(row, horizon_days=7) for row in rows
    )


def test_a_seasonal_reference_shorter_than_the_horizon_is_refused() -> None:
    rows = panel({"city_hotel": wave(12)})
    with pytest.raises(ModelContractError, match="not knowable"):
        seasonal_naive_prediction(rows[-1], lag_days=1, horizon_days=7)


def test_the_baseline_definition_says_what_happens_when_it_cannot_predict() -> None:
    assert "no forecast" in BASELINE_DEFINITION
    assert "carried-forward" in BASELINE_DEFINITION


# --- 6. rolling-origin generation -----------------------------------------------------------------


def test_origins_follow_the_documented_rule_exactly() -> None:
    policy = RollingOriginPolicy(horizon_days=2, minimum_train_days=10, step_days=2)
    dates = [START + dt.timedelta(days=i) for i in range(20)]
    origins = rolling_origins(dates, policy)
    assert origins[0] == dates[9]
    assert origins == tuple(dates[9] + dt.timedelta(days=2 * k) for k in range(len(origins)))
    assert all(origin < dates[-1] for origin in origins)


def test_origins_are_strictly_increasing() -> None:
    dates = [START + dt.timedelta(days=i) for i in range(80)]
    origins = rolling_origins(dates, FAST_POLICY)
    assert list(origins) == sorted(set(origins))


def test_a_history_too_short_to_backtest_is_refused() -> None:
    dates = [START + dt.timedelta(days=i) for i in range(10)]
    with pytest.raises(EvaluationError, match="cannot support a backtest"):
        rolling_origins(dates, FAST_POLICY)


def test_overlapping_windows_are_refused_because_they_would_double_count() -> None:
    with pytest.raises(EvaluationError, match="overlap"):
        RollingOriginPolicy(horizon_days=7, minimum_train_days=10, step_days=3)


def test_the_policy_records_that_nothing_is_shuffled() -> None:
    described = FAST_POLICY.as_dict()
    assert described["shuffle"] is False
    assert described["random_cross_validation"] is False
    assert described["windows_tile"] is True


# --- 7. no train / evaluation overlap ------------------------------------------------------------


def backtest(days: int = 120, hotels: int = 2) -> EvaluationResult:
    series = {"city_hotel": wave(days)}
    if hotels == 2:
        series["resort_hotel"] = wave(days, base=80, phase=3)
    return evaluate(
        panel(series),
        policy=FAST_POLICY,
        feature_names=DATASET_FEATURES,
        config=EstimatorConfig(max_iter=20),
    )


def test_no_fold_evaluates_a_date_it_trained_on() -> None:
    result = backtest()
    by_fold: dict[int, list[Prediction]] = {}
    for item in result.predictions:
        by_fold.setdefault(item.fold_index, []).append(item)
    for fold in result.folds:
        evaluated = {p.target_date for p in by_fold[fold.fold.index]}
        assert min(evaluated) > fold.fold.train_end
        assert fold.fold.train_end <= fold.fold.origin < fold.fold.evaluation_start


def test_evaluation_windows_do_not_overlap_each_other() -> None:
    result = backtest()
    seen: set[tuple[str, dt.date]] = set()
    for item in result.predictions:
        key = (item.hotel_key, item.target_date)
        assert key not in seen
        seen.add(key)


def test_a_fold_whose_training_reaches_past_the_origin_is_refused() -> None:
    rows = panel({"city_hotel": wave(20)})
    origin = START + dt.timedelta(days=10)
    train = [r for r in rows if r.target_date <= origin + dt.timedelta(days=1)]
    evaluation = [r for r in rows if r.target_date > origin]
    with pytest.raises(EvaluationError, match="past the origin"):
        assert_fold_is_chronological(train, evaluation, origin)


def test_a_fold_evaluating_the_origin_itself_is_refused() -> None:
    rows = panel({"city_hotel": wave(20)})
    origin = START + dt.timedelta(days=10)
    train = [r for r in rows if r.target_date <= origin]
    with pytest.raises(EvaluationError, match="at or before the origin"):
        assert_fold_is_chronological(train, train, origin)


# --- 8./9. leakage -------------------------------------------------------------------------------


def test_mutating_future_targets_leaves_earlier_predictions_byte_identical() -> None:
    quiet = wave(120)
    loud = [*quiet[:100], *[999_999] * 20]
    before = evaluate(
        panel({"city_hotel": quiet}),
        policy=FAST_POLICY,
        feature_names=DATASET_FEATURES,
        config=EstimatorConfig(max_iter=20),
    )
    after = evaluate(
        panel({"city_hotel": loud}),
        policy=FAST_POLICY,
        feature_names=DATASET_FEATURES,
        config=EstimatorConfig(max_iter=20),
    )
    cut = START + dt.timedelta(days=99)
    kept_before = [p for p in before.predictions if p.target_date <= cut]
    kept_after = [p for p in after.predictions if p.target_date <= cut]
    assert kept_before == kept_after
    assert kept_before, "the test would pass vacuously with no retained predictions"


def test_mutating_evaluation_targets_leaves_the_training_matrix_unchanged() -> None:
    quiet = wave(120)
    rows = panel({"city_hotel": quiet})
    origin = START + dt.timedelta(days=99)
    selection = select_model_features(DATASET_FEATURES, horizon_days=7)

    loud = [*quiet[:100], *[777_777] * 20]
    mutated = panel({"city_hotel": loud})

    train_before = [r for r in rows if r.target_date <= origin]
    train_after = [r for r in mutated if r.target_date <= origin]
    assert design_matrix(train_before, selection.selected) == design_matrix(
        train_after, selection.selected
    )
    assert [r.target_room_nights for r in train_before] == [
        r.target_room_nights for r in train_after
    ]


def test_no_selected_feature_reaches_inside_the_forecast_horizon() -> None:
    for horizon in (1, 2, 7, 14):
        selection = select_model_features(DATASET_FEATURES, horizon_days=horizon)
        for name in selection.selected:
            lead = feature_lead_days(name)
            assert lead is None or lead >= horizon, (name, horizon)


def test_rolling_means_are_excluded_whenever_the_horizon_exceeds_one_day() -> None:
    selection = select_model_features(DATASET_FEATURES, horizon_days=2)
    assert not [name for name in selection.selected if name.startswith("demand_rolling_mean")]
    reasons = dict(selection.excluded)
    assert "inside a 2-day forecast horizon" in reasons["demand_rolling_mean_7"]


# --- 10./11. determinism -------------------------------------------------------------------------


def test_fitting_twice_on_the_same_rows_gives_the_same_predictions() -> None:
    rows = panel({"city_hotel": wave(60)})
    selection = select_model_features(DATASET_FEATURES, horizon_days=7)
    config = EstimatorConfig(max_iter=30)
    first = LearnedModel.fit(rows[:50], selection.selected, config).predict(rows[50:])
    second = LearnedModel.fit(rows[:50], selection.selected, config).predict(rows[50:])
    assert first == second
    assert any(value is not None for value in first)


def test_the_whole_backtest_is_reproducible() -> None:
    first = backtest()
    second = backtest()
    assert first.predictions == second.predictions
    assert first.baseline_pooled == second.baseline_pooled
    assert first.learned_pooled == second.learned_pooled


def test_the_estimator_never_carves_its_own_random_validation_split() -> None:
    """``early_stopping='auto'`` would split the training set at random. It is off, always."""
    config = EstimatorConfig()
    assert config.early_stopping is False
    assert config.random_state == 0
    assert config.build().get_params()["early_stopping"] is False
    with pytest.raises(TypeError):
        EstimatorConfig(early_stopping=True)  # type: ignore[call-arg]


# --- 12.-15. metric arithmetic --------------------------------------------------------------------


ACTUALS = [10.0, 20.0, 30.0]
FORECASTS = [12.0, 18.0, 36.0]
# |errors| = 2, 2, 6  -> MAE 10/3 ; squares 4, 4, 36 -> RMSE sqrt(44/3)


def test_mae_is_the_mean_absolute_error() -> None:
    assert mean_absolute_error(ACTUALS, FORECASTS) == pytest.approx(10 / 3)


def test_rmse_is_the_root_mean_squared_error() -> None:
    assert root_mean_squared_error(ACTUALS, FORECASTS) == pytest.approx(math.sqrt(44 / 3))


def test_rmse_punishes_one_large_miss_more_than_mae_does() -> None:
    even = ([10.0, 10.0], [12.0, 12.0])
    spiky = ([10.0, 10.0], [10.0, 14.0])
    even_rmse = root_mean_squared_error(*even)
    spiky_rmse = root_mean_squared_error(*spiky)
    assert even_rmse is not None and spiky_rmse is not None
    assert mean_absolute_error(*even) == mean_absolute_error(*spiky)
    assert spiky_rmse > even_rmse


def test_smape_matches_its_written_definition() -> None:
    expected = 100.0 * (2 * 2 / (10 + 12) + 2 * 2 / (20 + 18) + 2 * 6 / (30 + 36)) / 3
    assert symmetric_mean_absolute_percentage_error(ACTUALS, FORECASTS) == pytest.approx(expected)


def test_an_exact_forecast_scores_zero_on_every_metric() -> None:
    assert mean_absolute_error(ACTUALS, ACTUALS) == 0.0
    assert root_mean_squared_error(ACTUALS, ACTUALS) == 0.0
    assert symmetric_mean_absolute_percentage_error(ACTUALS, ACTUALS) == 0.0


def test_smape_survives_a_zero_denominator_instead_of_dividing_by_it() -> None:
    assert symmetric_mean_absolute_percentage_error([0.0], [0.0]) == 0.0
    assert symmetric_mean_absolute_percentage_error([0.0, 10.0], [0.0, 10.0]) == 0.0


def test_smape_is_bounded_at_two_hundred() -> None:
    assert symmetric_mean_absolute_percentage_error([0.0], [50.0]) == pytest.approx(200.0)


def test_nothing_measured_reports_none_rather_than_zero() -> None:
    assert mean_absolute_error([], []) is None
    assert root_mean_squared_error([], []) is None
    assert symmetric_mean_absolute_percentage_error([], []) is None
    empty = metric_set([], [], skipped=4)
    assert empty.observations == 0
    assert empty.skipped == 4
    assert (empty.mae, empty.rmse, empty.smape) == (None, None, None)


def test_mismatched_lengths_are_refused() -> None:
    with pytest.raises(ValueError, match="cannot be paired"):
        mean_absolute_error([1.0], [1.0, 2.0])


def test_every_metric_has_a_written_definition_including_its_denominator() -> None:
    assert set(METRIC_DEFINITIONS) >= {"mae", "rmse", "smape", "denominator", "skipped"}
    assert "denominator is zero" in METRIC_DEFINITIONS["smape"]
    assert "never counted as zero error" in METRIC_DEFINITIONS["skipped"]


# --- 16./17. pooling and per-fold ----------------------------------------------------------------


def test_a_skipped_prediction_is_counted_and_never_scored_as_zero_error() -> None:
    predictions = [
        prediction(day=0, actual=10, baseline=10.0, learned=None),
        prediction(day=1, actual=20, baseline=None, learned=22.0),
    ]
    baseline = pooled_metrics(predictions, "baseline")
    learned = pooled_metrics(predictions, "learned")
    assert (baseline.observations, baseline.skipped) == (1, 1)
    assert (learned.observations, learned.skipped) == (1, 1)
    assert baseline.mae == 0.0
    assert learned.mae == 2.0


def test_pooling_weights_by_observation_and_not_by_fold() -> None:
    """A two-row fold must not count as much as a ten-row one."""
    predictions = [
        prediction(fold=0, day=0, actual=100, baseline=100.0, learned=100.0),
        *[prediction(fold=1, day=1 + i, actual=100, baseline=90.0, learned=90.0) for i in range(9)],
    ]
    fold_maes = [
        pooled_metrics([p for p in predictions if p.fold_index == f], "baseline").mae
        for f in (0, 1)
    ]
    assert fold_maes == [0.0, 10.0]
    assert pooled_metrics(predictions, "baseline").mae == pytest.approx(9.0)
    assert sum(v for v in fold_maes if v is not None) / 2 == 5.0


def test_an_unknown_method_name_raises_instead_of_scoring_the_other_one() -> None:
    with pytest.raises(EvaluationError, match="unknown method"):
        pooled_metrics([], "learnd")


def test_pooled_counts_add_up_across_folds() -> None:
    sets = [MetricSet(3, 1, 1.0, 1.0, 1.0), MetricSet(4, 2, 2.0, 2.0, 2.0)]
    assert pooled(sets) == (7, 3)


def test_each_fold_reports_its_own_metrics_and_row_counts() -> None:
    result = backtest()
    assert len(result.folds) >= 3
    for fold in result.folds:
        assert fold.fold.evaluation_rows == len(fold.predictions)
        assert fold.baseline.observations + fold.baseline.skipped == len(fold.predictions)
        assert fold.learned.observations + fold.learned.skipped == len(fold.predictions)
        record = fold.as_dict()
        assert record["train_rows"] == fold.fold.train_rows
        assert isinstance(record["complete_window"], bool)


def test_the_pooled_prediction_count_equals_the_sum_over_folds() -> None:
    result = backtest()
    assert sum(len(fold.predictions) for fold in result.folds) == len(result.predictions)


# --- 18. per-hotel metrics ----------------------------------------------------------------------


def test_each_hotel_is_reported_separately() -> None:
    result = backtest()
    per_hotel = per_hotel_metrics(result.predictions, minimum=1)
    assert set(per_hotel) == {"city_hotel", "resort_hotel"}
    for block in per_hotel.values():
        assert isinstance(block, dict)
        assert block["sufficient_coverage"] is True


def test_a_hotel_with_too_few_days_reports_insufficient_coverage_not_a_number() -> None:
    predictions = [prediction(day=0, actual=10, baseline=9.0, learned=9.5)]
    block = per_hotel_metrics(predictions)["city_hotel"]
    assert isinstance(block, dict)
    assert block["sufficient_coverage"] is False
    assert block["minimum_required"] == MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC
    assert "mae" not in block


def test_one_hotel_cannot_borrow_another_hotels_days() -> None:
    predictions = [
        *[
            prediction(hotel="city_hotel", day=i, actual=10, baseline=10.0, learned=10.0)
            for i in range(40)
        ],
        *[
            prediction(hotel="resort_hotel", day=i, actual=10, baseline=0.0, learned=0.0)
            for i in range(40)
        ],
    ]
    per_hotel = per_hotel_metrics(predictions)
    city = per_hotel["city_hotel"]
    resort = per_hotel["resort_hotel"]
    assert isinstance(city, dict)
    assert isinstance(resort, dict)
    assert city["baseline"]["mae"] == 0.0
    assert resort["baseline"]["mae"] == 10.0


# --- 19. baseline comparison ---------------------------------------------------------------------


def test_the_comparison_is_paired_and_reports_a_difference_not_a_winner() -> None:
    predictions = [
        prediction(day=0, actual=100, baseline=90.0, learned=95.0),
        prediction(day=1, actual=100, baseline=110.0, learned=104.0),
        prediction(day=2, actual=100, baseline=None, learned=100.0),
    ]
    block = comparison(predictions)
    assert block["paired_observations"] == 2
    assert block["baseline"]["mae"] == 10.0  # type: ignore[index]
    assert block["learned"]["mae"] == pytest.approx(4.5)  # type: ignore[index]
    assert block["learned_minus_baseline"]["mae"] == pytest.approx(-5.5)  # type: ignore[index]
    assert "winner" not in json.dumps(block)
    assert "better" not in json.dumps(block)


def test_a_difference_against_nothing_measured_is_none() -> None:
    assert difference(None, 1.0) is None
    assert difference(1.0, None) is None
    assert difference(3.0, 1.0) == 2.0


def test_the_comparison_ignores_days_only_one_method_could_forecast() -> None:
    predictions = [
        prediction(day=0, actual=100, baseline=100.0, learned=None),
        prediction(day=1, actual=100, baseline=50.0, learned=50.0),
    ]
    block = comparison(predictions)
    assert block["paired_observations"] == 1
    assert block["baseline"]["mae"] == 50.0  # type: ignore[index]


# --- 20. manifest and checksum -------------------------------------------------------------------


def evaluation_record(result: EvaluationResult | None = None) -> dict[str, object]:
    return build_evaluation_manifest(
        result or backtest(),
        dataset_version="v1",
        feature_version="v1",
        dataset_sha256="a" * 64,
        dataset_rows=60,
        dataset_path="fixture.csv",
        generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


def test_the_record_carries_everything_the_stage_requires() -> None:
    record = evaluation_record()
    model = record["model"]
    dataset = record["dataset"]
    assert isinstance(model, dict)
    assert isinstance(dataset, dict)
    assert model["model_version"] == MODEL_VERSION
    assert model["model_artifact_persisted"] is False
    assert model["random_state"] == 0
    assert dataset["dataset_version"] == "v1"
    assert dataset["feature_version"] == "v1"
    for key in ("features", "protocol", "metrics", "generation"):
        assert key in record, key


def test_the_content_checksum_ignores_the_wall_clock() -> None:
    early = evaluation_record()
    late = dict(early)
    late["generation"] = {"generated_at": "2030-06-01T00:00:00+00:00"}
    assert content_checksum(early) == content_checksum(late)


def test_the_content_checksum_notices_a_changed_metric() -> None:
    record = evaluation_record()
    tampered = json.loads(json.dumps(record))
    tampered["metrics"]["pooled"]["baseline"]["mae"] = 0.0
    assert content_checksum(tampered) != content_checksum(record)


def test_the_record_serialises_stably_and_stamps_its_own_digest() -> None:
    record = stamp_content_checksum(evaluation_record())
    generation = record["generation"]
    assert isinstance(generation, dict)
    assert generation["content_sha256"] == content_checksum(record)
    payload = serialise(record)
    assert payload == serialise(record)
    assert b"\r" not in payload
    assert payload.endswith(b"\n")


def test_the_record_holds_no_credential_shaped_value() -> None:
    text = serialise(evaluation_record()).decode("utf-8").lower()
    for forbidden in ("password", "secret", "postgresql://", "postgres://", "token"):
        assert forbidden not in text, forbidden


def test_the_record_states_that_no_artifact_was_written() -> None:
    """Stage 6.3 persisted nothing, and its record still says so.

    Stage 6.5 later fitted and wrote ``model.pkl``, which is why this no longer asserts that no
    payload exists anywhere -- it asserts the two things that remain true: the Stage 6.3 record
    is unchanged, and no payload is *committed*. The gitignore whitelist is the enforcement, and
    the directory listing is checked in `test_demand_model_integration.py`.
    """
    record = evaluation_record()
    model = record["model"]
    assert isinstance(model, dict)
    assert model["model_artifact_persisted"] is False

    root = Path(__file__).resolve().parents[2]
    ignored = (root / ".gitignore").read_text(encoding="utf-8")
    assert "!ml/models/*/model.pkl" not in ignored
    # Only the per-model file whitelist matters here:  and the bare
    #  re-include exist so git descends into the directory at all.
    committed_records = [
        line.strip()
        for line in ignored.splitlines()
        if line.startswith("!ml/models/*/") and not line.rstrip().endswith("/")
    ]
    assert committed_records, ignored
    assert all(name.endswith(".json") for name in committed_records), committed_records
    stray = [
        path
        for path in (root / "ml" / "models").rglob("*")
        if path.is_file() and path.suffix in {".joblib", ".onnx", ".h5", ".pt", ".pb"}
    ]
    assert stray == []


# --- 21. insufficient data ------------------------------------------------------------------------


def test_a_dataset_too_short_for_the_protocol_fails_loudly() -> None:
    with pytest.raises(EvaluationError, match="cannot support a backtest"):
        evaluate(
            panel({"city_hotel": wave(8)}),
            policy=FAST_POLICY,
            feature_names=DATASET_FEATURES,
        )


def test_training_with_no_complete_row_is_refused_rather_than_fitted_on_nothing() -> None:
    rows = panel({"city_hotel": wave(10)})
    with pytest.raises(ModelContractError, match="no training row"):
        LearnedModel.fit(rows, ("demand_lag_28",), EstimatorConfig(max_iter=5))


def test_an_empty_row_set_is_refused() -> None:
    with pytest.raises(EvaluationError, match="no rows"):
        evaluate([], policy=FAST_POLICY)
