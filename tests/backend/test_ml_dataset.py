"""The V2 demand dataset's rules, tested without a database.

Everything in :mod:`app.ml.dataset` is pure, so every rule it claims can be checked against
hand-computed values rather than against whatever the pipeline happened to produce. The
database-backed half of the contract -- booking cutoffs, capacity, tenant isolation -- lives in
``tests/integration/test_ml_dataset_pipeline.py``, where there is a real PostgreSQL to lie to.

The leakage tests are the ones that matter. Two of them mutate the future and require the past
not to move; if either ever passes for the wrong reason the dataset is worthless, and it will
still look fine.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.ml.dataset import (
    DATASET_VERSION,
    DEFAULT_HORIZON_DAYS,
    FEATURE_VERSION,
    DatasetContractError,
    DemandDataset,
    FeatureAvailability,
    FeatureSpec,
    HotelHistory,
    InsufficientDataError,
    assert_no_feature_is_known_only_after_prediction,
    assert_split_is_chronological,
    build_feature_specs,
    build_row,
    build_rows,
    calendar_features,
    cutoff_date_for,
    is_known_at_cutoff,
    lag_features,
    prediction_cutoff,
    rolling_mean_features,
    split_chronologically,
    validate_rows,
)

HOTEL_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
HOTEL_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
BASE = dt.date(2026, 1, 1)


def series(values: dict[int, int]) -> dict[dt.date, int]:
    """A demand series keyed by day offset from BASE, for readable fixtures."""
    return {BASE + dt.timedelta(days=offset): value for offset, value in values.items()}


def history(
    demand: dict[dt.date, int],
    *,
    hotel: uuid.UUID = HOTEL_A,
    on_books: dict[dt.date, int] | None = None,
    rooms: dict[dt.date, int] | None = None,
) -> HotelHistory:
    return HotelHistory(
        hotel_public_id=hotel,
        demand_by_date=demand,
        on_books_by_date=on_books if on_books is not None else dict.fromkeys(demand, 0),
        rooms_existing_by_date=rooms if rooms is not None else dict.fromkeys(demand, 10),
    )


def dense(days: int, *, hotel: uuid.UUID = HOTEL_A, start: int = 0) -> HotelHistory:
    """A contiguous series whose value is the day offset, so every lag is checkable by eye."""
    return history(series({i: i for i in range(start, start + days)}), hotel=hotel)


# --- 1. row identity --------------------------------------------------------------------------


def test_a_row_is_identified_by_hotel_and_date_only() -> None:
    rows = build_rows([dense(40)])
    row = rows[30]
    assert row.hotel_public_id == HOTEL_A
    assert row.target_date == BASE + dt.timedelta(days=30)


def test_identity_is_the_public_uuid_and_no_internal_key_is_present() -> None:
    """Internal BIGINT keys are used to join and are dropped at this boundary."""
    row = build_rows([dense(40)])[10]
    assert isinstance(row.hotel_public_id, uuid.UUID)
    assert not any("_id" in name for name in row.features), row.features.keys()
    assert not hasattr(row, "hotel_id")


# --- 2. target ---------------------------------------------------------------------------------


def test_the_target_is_the_realised_demand_for_that_date() -> None:
    rows = build_rows([history(series({0: 5, 1: 9, 2: 0}))])
    assert [r.target_room_nights for r in rows] == [5, 9, 0]


def test_a_target_date_absent_from_the_extract_is_an_error_not_a_zero() -> None:
    """Not selling a room and not being asked about the day are different things."""
    with pytest.raises(DatasetContractError, match="no realised demand"):
        build_row(history(series({0: 1})), BASE + dt.timedelta(days=9))


# --- 3. duplicates -----------------------------------------------------------------------------


def test_a_duplicate_hotel_day_is_refused() -> None:
    row = build_rows([dense(40)])[5]
    with pytest.raises(DatasetContractError, match="duplicate observation"):
        validate_rows([row, row])


# --- 4. chronological ordering -------------------------------------------------------------------


def test_rows_come_back_in_chronological_order() -> None:
    rows = build_rows([dense(40)])
    assert [r.target_date for r in rows] == sorted(r.target_date for r in rows)


def test_validation_refuses_rows_that_are_out_of_order() -> None:
    rows = list(build_rows([dense(40)]))
    with pytest.raises(DatasetContractError, match="chronological order"):
        validate_rows(list(reversed(rows)))


# --- 5-6. splitting ------------------------------------------------------------------------------


def test_the_split_is_chronological_and_the_partitions_do_not_overlap() -> None:
    split = split_chronologically(build_rows([dense(100)]))
    assert_split_is_chronological(split)
    assert max(r.target_date for r in split.train) < min(r.target_date for r in split.validation)
    assert max(r.target_date for r in split.validation) < min(r.target_date for r in split.test)


def test_splitting_is_not_random_and_takes_no_seed() -> None:
    """A seed argument would imply shuffling is a supported mode. It is not."""
    import inspect

    parameters = inspect.signature(split_chronologically).parameters
    assert "shuffle" not in parameters
    assert "random_state" not in parameters
    assert "seed" not in parameters


def test_the_same_rows_split_the_same_way_every_time() -> None:
    rows = build_rows([dense(100)])
    first = split_chronologically(rows)
    second = split_chronologically(rows)
    assert first.sizes == second.sizes
    assert first.train_end == second.train_end and first.validation_end == second.validation_end


def test_a_day_is_never_split_across_partitions() -> None:
    """With several hotels a row-index split would put one hotel's Tuesday in train and
    another's in test. Splitting on the date axis keeps a day whole."""
    rows = build_rows([dense(100, hotel=HOTEL_A), dense(100, hotel=HOTEL_B)])
    split = split_chronologically(rows)

    # Both hotels appear in every partition, and no date appears in two of them.
    for partition in (split.train, split.validation, split.test):
        assert {r.hotel_public_id for r in partition} == {HOTEL_A, HOTEL_B}
    train_dates = {r.target_date for r in split.train}
    validation_dates = {r.target_date for r in split.validation}
    test_dates = {r.target_date for r in split.test}
    assert not (train_dates & validation_dates)
    assert not (validation_dates & test_dates)
    assert not (train_dates & test_dates)


# --- 7. lags --------------------------------------------------------------------------------------


def test_a_lag_reads_exactly_that_many_days_before_the_target() -> None:
    demand = series({i: i * 10 for i in range(40)})
    got = lag_features(demand, BASE + dt.timedelta(days=30), 1, (1, 7, 14, 28))
    assert got == {
        "demand_lag_1": 290,
        "demand_lag_7": 230,
        "demand_lag_14": 160,
        "demand_lag_28": 20,
    }


def test_a_lag_shorter_than_the_horizon_is_refused() -> None:
    """It would read a day the forecaster cannot have seen."""
    with pytest.raises(DatasetContractError, match="not known at the cutoff"):
        lag_features(series({0: 1}), BASE, horizon_days=7, lag_days=(1,))


def test_build_rows_refuses_the_same_configuration() -> None:
    with pytest.raises(DatasetContractError, match="at least the horizon"):
        build_rows([dense(40)], horizon_days=7, lag_days=(1, 7))


# --- 8. rolling windows ---------------------------------------------------------------------------


def test_a_rolling_mean_covers_the_window_ending_at_the_cutoff_date() -> None:
    demand = series({i: i for i in range(40)})
    target = BASE + dt.timedelta(days=30)
    got = rolling_mean_features(demand, target, 1, (7,))
    # horizon 1 -> cutoff_date is day 29; the window is days 23..29 inclusive.
    assert got["demand_rolling_mean_7"] == pytest.approx(sum(range(23, 30)) / 7)


def test_a_rolling_mean_never_reaches_the_target_day() -> None:
    demand = series(dict.fromkeys(range(40), 0))
    demand[BASE + dt.timedelta(days=30)] = 10_000
    got = rolling_mean_features(demand, BASE + dt.timedelta(days=30), 1, (7, 14, 28))
    assert set(got.values()) == {0.0}, "the target day leaked into its own rolling feature"


def test_a_partial_window_is_missing_rather_than_averaged() -> None:
    """An average over a partially present window silently changes meaning with the amount of
    history available. Absent history stays absent."""
    demand = series(dict.fromkeys(range(3), 1))
    got = rolling_mean_features(demand, BASE + dt.timedelta(days=2), 1, (7,))
    assert got["demand_rolling_mean_7"] is None


# --- 9. missing history ---------------------------------------------------------------------------


def test_absent_history_is_none_and_never_zero() -> None:
    """Zero is a real demand value. A hotel with no history must not look like one that sold
    nothing."""
    rows = build_rows([dense(40)])
    first = rows[0]
    assert first.features["demand_lag_1"] is None
    assert first.features["demand_lag_28"] is None
    assert not first.has_complete_features


def test_the_report_counts_what_is_missing_without_raising() -> None:
    report = validate_rows(build_rows([dense(40)]))
    assert report.rows == 40
    assert report.rows_with_complete_features == 12  # 40 - 28, the longest lookback
    assert report.missing_feature_counts["demand_lag_28"] == 28
    assert report.missing_feature_counts["demand_lag_1"] == 1


# --- 10-11. leakage -------------------------------------------------------------------------------


def test_changing_the_future_does_not_change_an_earlier_row() -> None:
    """THE leakage test. A later observation is altered dramatically; the earlier row's
    features and target must be byte-identical."""
    base_history = dense(60)
    before = build_row(base_history, BASE + dt.timedelta(days=30))

    poisoned = dict(base_history.demand_by_date)
    for offset in range(31, 60):
        poisoned[BASE + dt.timedelta(days=offset)] = 999_999
    after = build_row(history(poisoned), BASE + dt.timedelta(days=30))

    assert dict(after.features) == dict(before.features)
    assert after.target_room_nights == before.target_room_nights
    assert after.prediction_cutoff == before.prediction_cutoff


def test_adding_an_entirely_new_future_observation_does_not_move_the_past() -> None:
    base_history = dense(40)
    before = build_rows([base_history])

    extended = dict(base_history.demand_by_date)
    extended[BASE + dt.timedelta(days=40)] = 500
    after = build_rows([history(extended)])

    assert [dict(r.features) for r in after[:40]] == [dict(r.features) for r in before]


def test_moving_the_target_date_moves_the_cutoff_with_it() -> None:
    """Changing which day is predicted must change the cutoff, not leave it pinned."""
    a = build_row(dense(60), BASE + dt.timedelta(days=30))
    b = build_row(dense(60), BASE + dt.timedelta(days=45))
    assert b.prediction_cutoff - a.prediction_cutoff == dt.timedelta(days=15)
    assert a.cutoff_date < a.target_date and b.cutoff_date < b.target_date


def test_the_cutoff_is_the_instant_the_cutoff_day_ends() -> None:
    target = dt.date(2026, 3, 10)
    assert cutoff_date_for(target, 1) == dt.date(2026, 3, 9)
    assert prediction_cutoff(target, 1) == dt.datetime(2026, 3, 10, tzinfo=dt.UTC)
    assert cutoff_date_for(target, 7) == dt.date(2026, 3, 3)
    assert prediction_cutoff(target, 7) == dt.datetime(2026, 3, 4, tzinfo=dt.UTC)


def test_a_fact_on_the_cutoff_day_is_known_and_one_the_next_day_is_not() -> None:
    target = dt.date(2026, 3, 10)
    assert is_known_at_cutoff(dt.datetime(2026, 3, 9, 23, 59, tzinfo=dt.UTC), target, 1)
    assert not is_known_at_cutoff(dt.datetime(2026, 3, 10, 0, 0, tzinfo=dt.UTC), target, 1)


def test_a_naive_timestamp_is_refused_rather_than_assumed_to_be_utc() -> None:
    """An assumed zone is how a few hours of leakage gets in unnoticed."""
    with pytest.raises(DatasetContractError, match="naive datetime"):
        is_known_at_cutoff(dt.datetime(2026, 3, 9, 12, 0), dt.date(2026, 3, 10), 1)


def test_a_zero_horizon_is_refused() -> None:
    with pytest.raises(DatasetContractError, match="at least 1 day"):
        cutoff_date_for(dt.date(2026, 3, 10), 0)


def test_validation_refuses_a_row_whose_cutoff_reaches_its_target() -> None:
    row = build_rows([dense(40)])[10]
    broken = type(row)(
        hotel_public_id=row.hotel_public_id,
        target_date=row.target_date,
        prediction_cutoff=row.prediction_cutoff,
        horizon_days=0,
        features=row.features,
        target_room_nights=row.target_room_nights,
    )
    with pytest.raises(DatasetContractError):
        validate_rows([broken])


# --- 12. capacity ---------------------------------------------------------------------------------


def test_capacity_travels_as_a_feature_and_is_reported_when_absent() -> None:
    demand = series(dict.fromkeys(range(5), 1))
    rooms = {BASE + dt.timedelta(days=i): 4 for i in range(3)}
    rows = build_rows([history(demand, rooms=rooms)])
    assert rows[0].features["rooms_existing_at_cutoff"] == 4
    assert rows[4].features["rooms_existing_at_cutoff"] is None


def test_demand_above_the_known_capacity_is_reported_not_dropped() -> None:
    """Back-dated imports genuinely produce this. Dropping the rows would hide it."""
    demand = series(dict.fromkeys(range(5), 50))
    rooms = {BASE + dt.timedelta(days=i): 4 for i in range(5)}
    report = validate_rows(build_rows([history(demand, rooms=rooms)]))
    assert report.missing_feature_counts["demand_exceeds_rooms_existing_at_cutoff"] == 5


# --- 13. determinism ------------------------------------------------------------------------------


def test_the_same_history_produces_identical_rows_every_time() -> None:
    first = build_rows([dense(60, hotel=HOTEL_A), dense(60, hotel=HOTEL_B)])
    second = build_rows([dense(60, hotel=HOTEL_B), dense(60, hotel=HOTEL_A)])
    assert [(r.hotel_public_id, r.target_date, dict(r.features)) for r in first] == [
        (r.hotel_public_id, r.target_date, dict(r.features)) for r in second
    ], "row order or content depends on the order hotels were passed in"


# --- 14. insufficient data ------------------------------------------------------------------------


def test_too_few_rows_to_split_fails_loudly() -> None:
    with pytest.raises(InsufficientDataError, match="below the minimum"):
        split_chronologically(build_rows([dense(10)]))


def test_too_few_distinct_dates_fails_even_with_many_rows() -> None:
    """Twelve hotels on two days is twenty-four rows and still cannot be split in time."""
    hotels = [history(series({0: 1, 1: 2}), hotel=uuid.UUID(int=n)) for n in range(1, 21)]
    with pytest.raises(InsufficientDataError):
        split_chronologically(build_rows(hotels), minimum_rows=5)


def test_an_empty_dataset_is_refused() -> None:
    with pytest.raises(InsufficientDataError, match="at least 1 required"):
        validate_rows([])


def test_fractions_that_leave_nothing_for_test_are_refused() -> None:
    rows = build_rows([dense(100)])
    with pytest.raises(DatasetContractError, match="leaves nothing for test"):
        split_chronologically(rows, train_fraction=0.8, validation_fraction=0.2)


# --- 15. malformed source data --------------------------------------------------------------------


def test_negative_demand_is_refused() -> None:
    rows = build_rows([history(series({0: 1}))])
    broken = type(rows[0])(
        hotel_public_id=rows[0].hotel_public_id,
        target_date=rows[0].target_date,
        prediction_cutoff=rows[0].prediction_cutoff,
        horizon_days=rows[0].horizon_days,
        features=rows[0].features,
        target_room_nights=-1,
    )
    with pytest.raises(DatasetContractError, match="negative demand"):
        validate_rows([broken])


def test_a_non_finite_feature_is_refused() -> None:
    rows = build_rows([history(series({0: 1}))])
    features = dict(rows[0].features) | {"demand_rolling_mean_7": float("nan")}
    broken = type(rows[0])(
        hotel_public_id=rows[0].hotel_public_id,
        target_date=rows[0].target_date,
        prediction_cutoff=rows[0].prediction_cutoff,
        horizon_days=rows[0].horizon_days,
        features=features,
        target_room_nights=1,
    )
    with pytest.raises(DatasetContractError, match="non-finite"):
        validate_rows([broken])


def test_a_mismatched_cutoff_is_refused() -> None:
    rows = build_rows([dense(40)])
    broken = type(rows[0])(
        hotel_public_id=rows[0].hotel_public_id,
        target_date=rows[0].target_date,
        prediction_cutoff=rows[0].prediction_cutoff + dt.timedelta(days=5),
        horizon_days=rows[0].horizon_days,
        features=rows[0].features,
        target_room_nights=rows[0].target_room_nights,
    )
    with pytest.raises(DatasetContractError, match="does not match the horizon"):
        validate_rows([broken])


# --- 16. multi-hotel isolation --------------------------------------------------------------------


def test_one_hotels_demand_cannot_reach_another_hotels_features() -> None:
    """Hotel A sells nothing; hotel B sells a great deal on the same dates. A's rows must not
    notice."""
    quiet = history(series(dict.fromkeys(range(40), 0)), hotel=HOTEL_A)
    busy = history(series(dict.fromkeys(range(40), 900)), hotel=HOTEL_B)

    alone = build_rows([quiet])
    together = build_rows([quiet, busy])
    a_rows = [r for r in together if r.hotel_public_id == HOTEL_A]

    assert [dict(r.features) for r in a_rows] == [dict(r.features) for r in alone]
    assert all(r.target_room_nights == 0 for r in a_rows)


def test_every_row_carries_exactly_one_hotel_identity() -> None:
    rows = build_rows([dense(20, hotel=HOTEL_A), dense(20, hotel=HOTEL_B)])
    assert {r.hotel_public_id for r in rows} == {HOTEL_A, HOTEL_B}
    for row in rows:
        assert isinstance(row.hotel_public_id, uuid.UUID)


# --- 17-18. contract and versioning ---------------------------------------------------------------


def test_the_dataset_carries_its_versions() -> None:
    dataset = DemandDataset(rows=build_rows([dense(40)]), feature_specs=build_feature_specs())
    assert dataset.dataset_version == DATASET_VERSION == "v1"
    assert dataset.feature_version == FEATURE_VERSION == "v1"
    assert dataset.horizon_days == DEFAULT_HORIZON_DAYS


def test_every_feature_in_a_row_is_declared_in_the_contract() -> None:
    specs = build_feature_specs()
    row = build_rows([dense(40)])[-1]
    assert set(row.features) == {spec.name for spec in specs}


def test_the_contract_order_is_stable() -> None:
    assert build_feature_specs() == build_feature_specs()
    assert [s.name for s in build_feature_specs()][:6] == [
        "day_of_week",
        "day_of_month",
        "month",
        "week_of_year",
        "day_of_year",
        "is_weekend",
    ]


def test_no_declared_feature_is_knowable_only_after_the_target_date() -> None:
    assert_no_feature_is_known_only_after_prediction(build_feature_specs())


def test_a_feature_declared_after_prediction_is_refused() -> None:
    """The check has to be able to fail, or it is decoration."""
    specs = (
        *build_feature_specs(),
        FeatureSpec("tomorrows_answer", FeatureAvailability.AFTER_PREDICTION, "cheating"),
    )
    with pytest.raises(DatasetContractError, match="tomorrows_answer"):
        assert_no_feature_is_known_only_after_prediction(specs)


def test_calendar_features_are_derived_from_the_target_date_alone() -> None:
    got = calendar_features(dt.date(2026, 3, 7))  # a Saturday
    assert got == {
        "day_of_week": 5,
        "day_of_month": 7,
        "month": 3,
        "week_of_year": 10,
        "day_of_year": 66,
        "is_weekend": 1,
    }


def test_a_weekday_is_not_flagged_as_a_weekend() -> None:
    assert calendar_features(dt.date(2026, 3, 9))["is_weekend"] == 0  # Monday
