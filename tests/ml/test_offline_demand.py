"""The offline training dataset's rules, tested without touching the network.

Two kinds of input, deliberately:

* **hand-built CSV text**, where every number is chosen so the expected answer can be computed
  by hand. Almost every rule below is checked this way, because a rule checked against real
  data is really only checked against whatever that data happened to contain.
* **a committed excerpt of the real source** (``fixtures/hotel_booking_demand_sample.csv``,
  see its README), which exists to prove the parser survives the real file's shapes -- English
  month names, the three-value status vocabulary, zero-night rows.

No test downloads anything. The pinned source URL appears here only as a string to be checked
for shape.

The leakage tests are the ones that matter, and they are written to fail loudly: each mutates
the future and requires the past to come back **byte-identical**.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from app.ml.dataset import (
    DATASET_VERSION,
    FEATURE_VERSION,
    DemandRow,
    InsufficientDataError,
    build_feature_specs,
)
from ml.pipelines.offline_demand import (
    CANCELLED_STATUSES,
    HOTEL_KEYS,
    IDENTITY_COLUMNS,
    MONTH_NUMBERS,
    OCCUPIED_STATUS,
    OFFLINE_DATASET_NAME,
    REQUIRED_COLUMNS,
    SOURCE_CITATION,
    SOURCE_COMMIT,
    SOURCE_DOI,
    SOURCE_LICENSE,
    SOURCE_NAME,
    SOURCE_SHA256,
    SOURCE_URL,
    OfflineDataset,
    OfflineSourceError,
    ParsedSource,
    build_from_source,
    build_manifest,
    build_offline_dataset,
    coverage_window,
    demand_by_date,
    offline_hotel_id,
    on_books_by_date,
    read_source,
    serialise_dataset,
    serialise_manifest,
    sha256_hex,
    validate_source_schema,
)

FIXTURE = Path(__file__).parent / "fixtures" / "hotel_booking_demand_sample.csv"

#: The real source's header, verbatim. Kept as a literal so the schema check is exercised
#: against all 32 published columns and not only against the nine the pipeline reads.
REAL_HEADER = (
    "hotel,is_canceled,lead_time,arrival_date_year,arrival_date_month,"
    "arrival_date_week_number,arrival_date_day_of_month,stays_in_weekend_nights,"
    "stays_in_week_nights,adults,children,babies,meal,country,market_segment,"
    "distribution_channel,is_repeated_guest,previous_cancellations,"
    "previous_bookings_not_canceled,reserved_room_type,assigned_room_type,booking_changes,"
    "deposit_type,agent,company,days_in_waiting_list,customer_type,adr,"
    "required_car_parking_spaces,total_of_special_requests,reservation_status,"
    "reservation_status_date"
)

MONTH_NAMES = {number: name for name, number in MONTH_NUMBERS.items()}


# --- builders ---------------------------------------------------------------------------------


def booking(
    *,
    hotel: str = "City Hotel",
    arrival: dt.date,
    nights: int,
    lead_time: int = 30,
    status: str = OCCUPIED_STATUS,
    status_date: dt.date | None = None,
) -> dict[str, str]:
    """One source row, spelled the way the real file spells it."""
    return {
        "hotel": hotel,
        "lead_time": str(lead_time),
        "arrival_date_year": str(arrival.year),
        "arrival_date_month": MONTH_NAMES[arrival.month],
        "arrival_date_day_of_month": str(arrival.day),
        "stays_in_weekend_nights": "0",
        "stays_in_week_nights": str(nights),
        "reservation_status": status,
        "reservation_status_date": (status_date or arrival).isoformat(),
    }


def as_csv(rows: Sequence[Mapping[str, str]], *, header: Sequence[str] | None = None) -> str:
    """Rows to CSV text, so the schema check and the parser both get exercised."""
    columns = list(header or REQUIRED_COLUMNS)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue()


def parse(rows: Sequence[Mapping[str, str]]) -> ParsedSource:
    return read_source(io.StringIO(as_csv(rows), newline=""))


BASE = dt.date(2016, 1, 1)


def spread(
    count: int,
    *,
    hotel: str = "City Hotel",
    nights: int = 1,
    start: dt.date = BASE,
    lead_time: int = 30,
) -> list[dict[str, str]]:
    """``count`` one-night stays on consecutive days -- the simplest history that splits."""
    return [
        booking(
            hotel=hotel,
            arrival=start + dt.timedelta(days=offset),
            nights=nights,
            lead_time=lead_time,
        )
        for offset in range(count)
    ]


def fixture_source() -> ParsedSource:
    """The committed excerpt of the real source. Read as text; its bytes are never hashed."""
    with FIXTURE.open(newline="", encoding="utf-8") as handle:
        return read_source(handle)


def row_tuple(row: DemandRow) -> tuple[object, ...]:
    """A row reduced to a comparable value, for the byte-identity leakage assertions."""
    return (
        row.hotel_public_id,
        row.target_date,
        row.prediction_cutoff,
        row.horizon_days,
        row.target_room_nights,
        tuple(sorted(row.features.items(), key=lambda item: item[0])),
    )


def identity_and_features(rows: Sequence[DemandRow]) -> list[tuple[object, ...]]:
    """Everything about a row except its target -- what a leakage test must find unchanged."""
    return [(*row_tuple(row)[:4], row_tuple(row)[5]) for row in rows]


# --- 1. source schema validation ---------------------------------------------------------------


def test_the_real_thirty_two_column_header_validates() -> None:
    validate_source_schema(REAL_HEADER.split(","))


def test_every_required_column_is_present_in_the_real_header() -> None:
    published = set(REAL_HEADER.split(","))
    assert set(REQUIRED_COLUMNS) <= published


def test_a_missing_column_is_refused_and_named() -> None:
    columns = [name for name in REAL_HEADER.split(",") if name != "reservation_status"]
    with pytest.raises(OfflineSourceError, match="reservation_status"):
        validate_source_schema(columns)


def test_a_file_without_a_header_is_refused() -> None:
    with pytest.raises(OfflineSourceError, match="no header"):
        validate_source_schema(None)


def test_a_renamed_column_fails_the_schema_rather_than_every_row() -> None:
    renamed = [c if c != "lead_time" else "leadtime" for c in REQUIRED_COLUMNS]
    text = as_csv([booking(arrival=BASE, nights=2)], header=renamed)
    with pytest.raises(OfflineSourceError, match="lead_time"):
        read_source(io.StringIO(text, newline=""))


# --- 2. target derivation ----------------------------------------------------------------------


def test_a_stay_becomes_one_room_night_per_date_excluding_the_checkout_day() -> None:
    parsed = parse([booking(arrival=dt.date(2016, 3, 10), nights=3)])
    series = demand_by_date(parsed.bookings)["city_hotel"]
    assert series == {
        dt.date(2016, 3, 10): 1,
        dt.date(2016, 3, 11): 1,
        dt.date(2016, 3, 12): 1,
    }
    assert dt.date(2016, 3, 13) not in series


def test_room_nights_from_different_bookings_add_up() -> None:
    parsed = parse(
        [
            booking(arrival=dt.date(2016, 3, 10), nights=2),
            booking(arrival=dt.date(2016, 3, 11), nights=2),
        ]
    )
    assert demand_by_date(parsed.bookings)["city_hotel"] == {
        dt.date(2016, 3, 10): 1,
        dt.date(2016, 3, 11): 2,
        dt.date(2016, 3, 12): 1,
    }


@pytest.mark.parametrize("status", sorted(CANCELLED_STATUSES))
def test_a_cancelled_booking_holds_no_room_night(status: str) -> None:
    parsed = parse(
        [
            booking(
                arrival=dt.date(2016, 3, 10),
                nights=3,
                status=status,
                status_date=dt.date(2016, 2, 1),
            )
        ]
    )
    assert demand_by_date(parsed.bookings) == {}


def test_arrivals_are_not_counted_as_room_nights() -> None:
    """The mistake this whole module exists to avoid: one row is not one observation."""
    parsed = parse([booking(arrival=dt.date(2016, 3, 10), nights=5)])
    series = demand_by_date(parsed.bookings)["city_hotel"]
    assert sum(series.values()) == 5
    assert len(series) == 5


def test_derivation_over_the_real_excerpt_matches_the_sum_of_its_stay_lengths() -> None:
    parsed = fixture_source()
    expected = sum(b.nights for b in parsed.bookings if b.occupied)
    produced = sum(sum(days.values()) for days in demand_by_date(parsed.bookings).values())
    assert produced == expected > 0


# --- 3. duplicate detection --------------------------------------------------------------------


def test_identical_source_rows_are_both_counted() -> None:
    """The source carries no booking id, so two identical rows may be two real bookings.

    De-duplicating would silently delete demand. The pipeline keeps both, and the
    documentation says so.
    """
    one = booking(arrival=dt.date(2016, 3, 10), nights=1)
    assert demand_by_date(parse([one, dict(one)]).bookings)["city_hotel"] == {
        dt.date(2016, 3, 10): 2
    }


def test_the_pipeline_never_emits_two_rows_for_one_hotel_day() -> None:
    dataset = build_offline_dataset(parse(spread(60) + spread(60, hotel="Resort Hotel")))
    keys = [(row.hotel_public_id, row.target_date) for row in dataset.rows]
    assert len(keys) == len(set(keys))


def test_the_real_excerpt_produces_no_duplicate_hotel_day() -> None:
    dataset = build_offline_dataset(fixture_source())
    keys = [(row.hotel_public_id, row.target_date) for row in dataset.rows]
    assert len(keys) == len(set(keys))


# --- 4. chronological ordering -----------------------------------------------------------------


def test_rows_come_back_in_date_order() -> None:
    dataset = build_offline_dataset(parse(spread(60) + spread(60, hotel="Resort Hotel")))
    dates = [row.target_date for row in dataset.rows]
    assert dates == sorted(dates)


def test_rows_are_ordered_identically_whatever_order_the_source_listed_hotels_in() -> None:
    forwards = build_offline_dataset(parse(spread(60) + spread(60, hotel="Resort Hotel")))
    backwards = build_offline_dataset(parse(spread(60, hotel="Resort Hotel") + spread(60)))
    assert serialise_dataset(forwards) == serialise_dataset(backwards)


# --- 5. missing dates --------------------------------------------------------------------------


def test_a_gap_in_the_source_stays_a_gap_and_is_not_filled_with_zero() -> None:
    """Nothing in the source says an unsold day and an unobserved day are the same thing."""
    rows = spread(20) + spread(20, start=BASE + dt.timedelta(days=30))
    series = demand_by_date(parse(rows).bookings)["city_hotel"]
    assert BASE + dt.timedelta(days=25) not in series
    assert len(series) == 40


def test_a_lag_reaching_across_a_gap_is_missing_rather_than_zero() -> None:
    rows = spread(40) + spread(40, start=BASE + dt.timedelta(days=60))
    dataset = build_offline_dataset(parse(rows), train_fraction=0.5, validation_fraction=0.25)
    after_gap = next(r for r in dataset.rows if r.target_date == BASE + dt.timedelta(days=60))
    assert after_gap.features["demand_lag_1"] is None
    assert after_gap.features["demand_rolling_mean_7"] is None


def test_the_real_excerpt_has_no_missing_date_inside_a_coverage_window() -> None:
    """Measured, not assumed: whether gaps exist is a property of the source, not a choice."""
    parsed = fixture_source()
    demand = demand_by_date(parsed.bookings)
    for hotel_key, (first, last) in coverage_window(parsed.bookings).items():
        covered = {d for d in demand[hotel_key] if first <= d <= last}
        assert covered, hotel_key


# --- 6. hotel / group isolation ----------------------------------------------------------------


def test_hotel_labels_map_to_distinct_offline_keys() -> None:
    assert sorted(HOTEL_KEYS.values()) == ["city_hotel", "resort_hotel"]
    assert len(set(HOTEL_KEYS.values())) == len(HOTEL_KEYS)


def test_offline_hotel_ids_are_version_five_so_they_cannot_pass_for_production_ids() -> None:
    """Production ``hotels.public_id`` values are random v4. A v5 id is provably not one."""
    for key in HOTEL_KEYS.values():
        assert offline_hotel_id(key).version == 5


def test_offline_hotel_ids_are_deterministic_and_distinct() -> None:
    first = {key: offline_hotel_id(key) for key in HOTEL_KEYS.values()}
    again = {key: offline_hotel_id(key) for key in HOTEL_KEYS.values()}
    assert first == again
    assert len(set(first.values())) == len(first)


def test_one_hotels_bookings_cannot_move_another_hotels_rows() -> None:
    quiet = spread(60, hotel="Resort Hotel")
    baseline = build_offline_dataset(parse(spread(60) + quiet))
    busy = [
        booking(hotel="Resort Hotel", arrival=BASE + dt.timedelta(days=offset), nights=1)
        for offset in range(60)
        for _ in range(9)
    ]
    loud = build_offline_dataset(parse(spread(60) + quiet + busy))

    city = offline_hotel_id("city_hotel")
    before = [row_tuple(r) for r in baseline.rows if r.hotel_public_id == city]
    after = [row_tuple(r) for r in loud.rows if r.hotel_public_id == city]
    assert before == after


def test_row_identity_is_the_offline_uuid_and_carries_no_internal_key() -> None:
    dataset = build_offline_dataset(fixture_source())
    expected = {offline_hotel_id(key) for key in HOTEL_KEYS.values()}
    assert {row.hotel_public_id for row in dataset.rows} <= expected
    assert all(isinstance(row.hotel_public_id, uuid.UUID) for row in dataset.rows)


# --- 7. temporal partitioning ------------------------------------------------------------------


def test_the_three_partitions_do_not_overlap() -> None:
    dataset = build_offline_dataset(fixture_source())
    split = dataset.split
    keys = [
        {(row.hotel_public_id, row.target_date) for row in part}
        for part in (split.train, split.validation, split.test)
    ]
    assert keys[0] & keys[1] == set()
    assert keys[1] & keys[2] == set()
    assert keys[0] & keys[2] == set()
    assert sum(len(part) for part in keys) == len(dataset.rows)


def test_train_precedes_validation_precedes_test() -> None:
    split = build_offline_dataset(fixture_source()).split
    assert max(r.target_date for r in split.train) < min(r.target_date for r in split.validation)
    assert max(r.target_date for r in split.validation) < min(r.target_date for r in split.test)


def test_a_calendar_date_is_never_split_across_partitions() -> None:
    dataset = build_offline_dataset(fixture_source())
    seen: dict[dt.date, set[str]] = {}
    for key, partition in dataset.partition_of.items():
        seen.setdefault(key[1], set()).add(partition)
    assert all(len(partitions) == 1 for partitions in seen.values())


def test_the_serialised_partition_column_agrees_with_the_split() -> None:
    dataset = build_offline_dataset(fixture_source())
    reader = csv.DictReader(io.StringIO(serialise_dataset(dataset).decode("utf-8")))
    expected = {
        (str(key[0]), key[1].isoformat()): value for key, value in dataset.partition_of.items()
    }
    rows = list(reader)
    assert len(rows) == len(dataset.rows)
    for record in rows:
        assert expected[(record["hotel_public_id"], record["target_date"])] == record["partition"]


# --- 8. feature compatibility with the Stage 6.1 contract ---------------------------------------


def test_the_offline_columns_are_exactly_the_stage_61_contract() -> None:
    dataset = build_offline_dataset(fixture_source())
    assert dataset.feature_names == tuple(spec.name for spec in build_feature_specs())
    assert dataset.dataset_version == DATASET_VERSION
    assert dataset.feature_version == FEATURE_VERSION


def test_calendar_features_are_fully_supported() -> None:
    dataset = build_offline_dataset(parse(spread(60)))
    row = next(r for r in dataset.rows if r.target_date == dt.date(2016, 1, 30))
    assert row.features["day_of_week"] == 5
    assert row.features["is_weekend"] == 1
    assert row.features["month"] == 1
    assert row.features["day_of_year"] == 30


def test_capacity_is_unsupported_and_reports_as_missing_rather_than_zero() -> None:
    """The source publishes no room inventory. ``None`` says that; ``0`` would lie about it."""
    dataset = build_offline_dataset(fixture_source())
    assert all(row.features["rooms_existing_at_cutoff"] is None for row in dataset.rows)
    assert dataset.report.missing_feature_counts["rooms_existing_at_cutoff"] == len(dataset.rows)


def test_on_the_books_is_supported_and_actually_populated() -> None:
    dataset = build_offline_dataset(fixture_source())
    populated = [
        row for row in dataset.rows if row.features["on_books_room_nights_at_cutoff"] is not None
    ]
    assert len(populated) > len(dataset.rows) // 2


def test_an_unsupported_feature_is_never_invented() -> None:
    dataset = build_offline_dataset(fixture_source())
    assert "rooms_existing_at_cutoff" in dataset.feature_names
    assert not any(row.features["rooms_existing_at_cutoff"] == 0 for row in dataset.rows)


# --- 9. leakage prevention ---------------------------------------------------------------------


def test_rolling_windows_never_reach_the_target_date() -> None:
    quiet = spread(60)
    poisoned = quiet + [booking(arrival=BASE + dt.timedelta(days=45), nights=1) for _ in range(500)]
    before = build_offline_dataset(parse(quiet))
    after = build_offline_dataset(parse(poisoned))
    target = BASE + dt.timedelta(days=45)
    a = next(r for r in before.rows if r.target_date == target)
    b = next(r for r in after.rows if r.target_date == target)
    assert b.target_room_nights == a.target_room_nights + 500
    for window in (7, 14, 28):
        assert (
            a.features[f"demand_rolling_mean_{window}"]
            == b.features[f"demand_rolling_mean_{window}"]
        )


def test_a_later_booking_cannot_change_an_earlier_row() -> None:
    quiet = spread(90)
    later = [
        booking(arrival=BASE + dt.timedelta(days=offset), nights=1)
        for offset in range(60, 90)
        for _ in range(999)
    ]
    before = build_offline_dataset(parse(quiet))
    after = build_offline_dataset(parse(quiet + later))
    cut = BASE + dt.timedelta(days=59)
    assert [row_tuple(r) for r in before.rows if r.target_date <= cut] == [
        row_tuple(r) for r in after.rows if r.target_date <= cut
    ]


def test_test_partition_targets_cannot_reach_train_or_validation_features() -> None:
    """Phase 9's central claim, checked by moving the test period and requiring silence."""
    quiet = spread(120)
    baseline = build_offline_dataset(parse(quiet))
    test_dates = {row.target_date for row in baseline.split.test}
    noise = [booking(arrival=day, nights=1) for day in sorted(test_dates) for _ in range(777)]
    mutated = build_offline_dataset(parse(quiet + noise))

    kept = {row.target_date for row in baseline.split.train} | {
        row.target_date for row in baseline.split.validation
    }
    before = [row_tuple(r) for r in baseline.rows if r.target_date in kept]
    after = [row_tuple(r) for r in mutated.rows if r.target_date in kept]
    assert before == after


def test_validation_targets_cannot_reach_train_features() -> None:
    quiet = spread(120)
    baseline = build_offline_dataset(parse(quiet))
    later = {row.target_date for row in baseline.split.validation} | {
        row.target_date for row in baseline.split.test
    }
    noise = [booking(arrival=day, nights=1) for day in sorted(later) for _ in range(321)]
    mutated = build_offline_dataset(parse(quiet + noise))

    train_dates = {row.target_date for row in baseline.split.train}
    before = [row_tuple(r) for r in baseline.rows if r.target_date in train_dates]
    after = [row_tuple(r) for r in mutated.rows if r.target_date in train_dates]
    assert before == after


def test_a_target_day_change_never_reaches_a_row_at_or_before_that_day() -> None:
    """A day's own demand is its answer, and no row may have already known it.

    Later rows legitimately move -- ``demand_lag_1`` on the following day *is* this day's
    demand, which is the point of a lag. The claim being checked is the other direction, and
    it is checked at the target day itself rather than a day before it: a row must not read its
    own answer.
    """
    quiet = spread(60)
    target = BASE + dt.timedelta(days=40)
    louder = quiet + [booking(arrival=target, nights=1, lead_time=0) for _ in range(50)]
    before = build_offline_dataset(parse(quiet))
    after = build_offline_dataset(parse(louder))

    assert identity_and_features(
        [r for r in before.rows if r.target_date <= target]
    ) == identity_and_features([r for r in after.rows if r.target_date <= target])

    moved = [
        (r.target_date, s.target_room_nights - r.target_room_nights)
        for r, s in zip(before.rows, after.rows, strict=True)
        if r.target_room_nights != s.target_room_nights
    ]
    assert moved == [(target, 50)]


def test_a_booking_entered_after_the_cutoff_is_not_on_the_books() -> None:
    target = dt.date(2016, 6, 1)
    early = booking(arrival=target, nights=1, lead_time=10)
    same_day = booking(arrival=target, nights=1, lead_time=0)
    series = on_books_by_date(parse([early, same_day]).bookings, horizon_days=1)
    assert series["city_hotel"][target] == 1


def test_a_booking_cancelled_after_the_cutoff_was_still_on_the_books() -> None:
    target = dt.date(2016, 6, 1)
    cancelled_later = booking(
        arrival=target, nights=1, lead_time=30, status="Canceled", status_date=target
    )
    series = on_books_by_date(parse([cancelled_later]).bookings, horizon_days=1)
    assert series["city_hotel"][target] == 1


def test_a_booking_cancelled_before_the_cutoff_is_not_on_the_books() -> None:
    target = dt.date(2016, 6, 1)
    cancelled_early = booking(
        arrival=target,
        nights=1,
        lead_time=30,
        status="Canceled",
        status_date=target - dt.timedelta(days=5),
    )
    series = on_books_by_date(parse([cancelled_early]).bookings, horizon_days=1)
    assert series.get("city_hotel", {}).get(target) is None


def test_a_longer_horizon_sees_less_of_the_book() -> None:
    target = dt.date(2016, 6, 1)
    rows = [
        booking(arrival=target, nights=1, lead_time=30),
        booking(arrival=target, nights=1, lead_time=3),
    ]
    parsed = parse(rows)
    assert on_books_by_date(parsed.bookings, horizon_days=1)["city_hotel"][target] == 2
    assert on_books_by_date(parsed.bookings, horizon_days=7)["city_hotel"][target] == 1


def test_the_cutoff_always_precedes_the_target_date() -> None:
    dataset = build_offline_dataset(fixture_source())
    for row in dataset.rows:
        assert row.cutoff_date < row.target_date
        assert row.prediction_cutoff.tzinfo is not None
        assert row.prediction_cutoff.utcoffset() == dt.timedelta(0)


# --- 10. deterministic transformation ----------------------------------------------------------


def test_two_builds_from_the_same_bytes_are_identical() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    first = build_from_source(io.StringIO(text, newline=""), source_checksum="x" * 64)
    second = build_from_source(io.StringIO(text, newline=""), source_checksum="x" * 64)
    assert first.processed == second.processed
    assert first.processed_checksum == second.processed_checksum


def test_a_rebuild_changes_only_the_generation_block_of_the_manifest() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    early = build_from_source(
        io.StringIO(text, newline=""),
        source_checksum="x" * 64,
    ).manifest
    late = build_from_source(io.StringIO(text, newline=""), source_checksum="x" * 64).manifest
    assert {k: v for k, v in early.items() if k != "generation"} == {
        k: v for k, v in late.items() if k != "generation"
    }


def test_float_features_are_written_with_a_round_tripping_spelling() -> None:
    dataset = build_offline_dataset(fixture_source())
    text = serialise_dataset(dataset).decode("utf-8")
    for record in csv.DictReader(io.StringIO(text)):
        raw = record["demand_rolling_mean_7"]
        if raw:
            assert float(raw) == float(repr(float(raw)))


# --- 11. checksum generation -------------------------------------------------------------------


def test_the_processed_bytes_use_lf_and_end_with_a_newline() -> None:
    payload = serialise_dataset(build_offline_dataset(fixture_source()))
    assert b"\r" not in payload
    assert payload.endswith(b"\n")


def test_the_checksum_is_sixty_four_hex_characters_of_the_processed_bytes() -> None:
    payload = serialise_dataset(build_offline_dataset(fixture_source()))
    digest = sha256_hex(payload)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert sha256_hex(payload) == digest


def test_one_changed_room_night_changes_the_checksum() -> None:
    quiet = spread(60)
    before = serialise_dataset(build_offline_dataset(parse(quiet)))
    louder = [*quiet, booking(arrival=BASE + dt.timedelta(days=30), nights=1)]
    after = serialise_dataset(build_offline_dataset(parse(louder)))
    assert sha256_hex(before) != sha256_hex(after)


# --- 12. manifest generation -------------------------------------------------------------------


def manifest_for(dataset: OfflineDataset) -> dict[str, object]:
    payload = serialise_dataset(dataset)
    return build_manifest(
        dataset,
        source_checksum=SOURCE_SHA256,
        processed_checksum=sha256_hex(payload),
        processed_bytes=len(payload),
        generated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


def test_the_manifest_carries_every_field_the_stage_requires() -> None:
    manifest = manifest_for(build_offline_dataset(fixture_source()))
    dataset_block = manifest["dataset"]
    source_block = manifest["source"]
    partitions = manifest["partitions"]
    assert isinstance(dataset_block, dict)
    assert isinstance(source_block, dict)
    assert isinstance(partitions, dict)

    assert dataset_block["name"] == OFFLINE_DATASET_NAME
    assert dataset_block["dataset_version"] == DATASET_VERSION
    assert dataset_block["feature_version"] == FEATURE_VERSION
    for key in (
        "rows",
        "hotels",
        "date_min",
        "date_max",
        "processed_sha256",
        "target_statistics",
        "columns",
    ):
        assert key in dataset_block, key
    for key in ("name", "url", "sha256", "license", "citation", "doi", "rejection_counts"):
        assert key in source_block, key
    for name in ("train", "validation", "test"):
        part = partitions[name]
        assert isinstance(part, dict)
        assert {"rows", "date_min", "date_max", "target"} <= set(part)


def test_the_manifest_checksum_matches_the_bytes_it_describes() -> None:
    dataset = build_offline_dataset(fixture_source())
    payload = serialise_dataset(dataset)
    manifest = manifest_for(dataset)
    block = manifest["dataset"]
    assert isinstance(block, dict)
    assert block["processed_sha256"] == sha256_hex(payload)
    assert block["processed_bytes"] == len(payload)


def test_the_manifest_records_that_no_model_was_trained() -> None:
    manifest = manifest_for(build_offline_dataset(fixture_source()))
    generation = manifest["generation"]
    assert isinstance(generation, dict)
    assert generation["model_trained"] is False


def test_the_manifest_holds_no_credential_shaped_value() -> None:
    text = serialise_manifest(manifest_for(build_offline_dataset(fixture_source()))).decode()
    lowered = text.lower()
    for forbidden in ("password", "secret", "postgresql://", "postgres://", "token", "@localhost"):
        assert forbidden not in lowered, forbidden


def test_the_manifest_serialises_stably() -> None:
    manifest = manifest_for(build_offline_dataset(fixture_source()))
    payload = serialise_manifest(manifest)
    assert payload == serialise_manifest(manifest)
    assert b"\r" not in payload
    assert payload.endswith(b"\n")


# --- 13. insufficient-data detection -----------------------------------------------------------


def test_too_few_observations_fail_loudly() -> None:
    with pytest.raises(InsufficientDataError):
        build_offline_dataset(parse(spread(5)))


def test_an_arrival_window_shorter_than_the_longest_stay_covers_nothing() -> None:
    """The truncation rule, refusing rather than emitting an under-counted target."""
    rows = [
        booking(arrival=BASE, nights=40),
        booking(arrival=BASE + dt.timedelta(days=3), nights=1),
    ]
    with pytest.raises(OfflineSourceError, match="fully covered"):
        coverage_window(parse(rows).bookings)


def test_a_source_with_no_occupancy_produces_no_dataset() -> None:
    rows = [
        booking(arrival=BASE, nights=2, status="Canceled", status_date=BASE),
        booking(arrival=BASE + dt.timedelta(days=1), nights=2, status="No-Show", status_date=BASE),
    ]
    with pytest.raises(OfflineSourceError, match="no occupancy history"):
        build_offline_dataset(parse(rows))


def test_truncated_dates_are_dropped_as_targets_and_counted() -> None:
    rows = [booking(arrival=BASE, nights=10), *spread(60, start=BASE)]
    dataset = build_offline_dataset(parse(rows))
    first, _ = dataset.coverage["city_hotel"]
    assert first == BASE + dt.timedelta(days=9)
    assert dataset.dates_outside_coverage == 9
    assert min(row.target_date for row in dataset.rows) == first


# --- 14. malformed source data -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field", "fragment"),
    [
        ({"hotel": "Beach Hotel"}, "hotel", "unknown hotel label"),
        ({"arrival_date_month": "Juli"}, "arrival_date_month", "unknown month name"),
        (
            {"arrival_date_day_of_month": "30", "arrival_date_month": "February"},
            "arrival_date_day_of_month",
            "not a real date",
        ),
        ({"arrival_date_year": "twenty-sixteen"}, "arrival_date_day_of_month", "not a real date"),
        ({"stays_in_week_nights": "two"}, "stays_in_week_nights", "not an integer"),
        ({"stays_in_week_nights": "1.0"}, "stays_in_week_nights", "not an integer"),
        ({"lead_time": ""}, "lead_time", "not an integer"),
        ({"reservation_status": "Departed"}, "reservation_status", "unknown status"),
        ({"reservation_status_date": "01/06/2016"}, "reservation_status_date", "ISO-8601"),
        ({"stays_in_week_nights": "0"}, "stays_in_week_nights", "zero-night"),
    ],
)
def test_a_malformed_row_is_rejected_with_its_field_and_reason(
    overrides: dict[str, str], field: str, fragment: str
) -> None:
    row = booking(arrival=dt.date(2016, 6, 1), nights=2) | overrides
    parsed = parse([row])
    assert parsed.bookings == ()
    assert len(parsed.rejected) == 1
    rejected = parsed.rejected[0]
    assert rejected.field == field
    assert fragment in rejected.reason
    assert rejected.row_number == 2


def test_one_bad_row_does_not_discard_the_good_ones() -> None:
    good = booking(arrival=dt.date(2016, 6, 1), nights=2)
    bad = booking(arrival=dt.date(2016, 6, 2), nights=2) | {"hotel": "Beach Hotel"}
    parsed = parse([good, bad, dict(good)])
    assert len(parsed.bookings) == 2
    assert [r.row_number for r in parsed.rejected] == [3]
    assert parsed.rows_read == 3


def test_rejection_counts_group_by_reason() -> None:
    rows = [
        booking(arrival=dt.date(2016, 6, 1), nights=0),
        booking(arrival=dt.date(2016, 6, 2), nights=0),
        booking(arrival=dt.date(2016, 6, 3), nights=2) | {"hotel": "Beach Hotel"},
    ]
    counts = parse(rows).rejection_counts
    assert counts == {
        "unknown hotel label 'Beach Hotel'": 1,
        "zero-night booking occupies no room night": 2,
    }


def test_the_real_excerpt_rejects_only_zero_night_rows() -> None:
    parsed = fixture_source()
    assert set(parsed.rejection_counts) == {"zero-night booking occupies no room night"}
    assert parsed.rows_read == len(parsed.bookings) + len(parsed.rejected)


# --- 15. negative demand rejection -------------------------------------------------------------


def test_a_negative_stay_length_is_refused() -> None:
    row = booking(arrival=dt.date(2016, 6, 1), nights=-3)
    rejected = parse([row]).rejected[0]
    assert rejected.field == "stays_in_week_nights"
    assert "negative stay length" in rejected.reason


def test_a_negative_lead_time_is_refused() -> None:
    row = booking(arrival=dt.date(2016, 6, 1), nights=2, lead_time=-5)
    rejected = parse([row]).rejected[0]
    assert rejected.field == "lead_time"
    assert "negative lead time" in rejected.reason


def test_demand_is_never_negative_in_the_built_dataset() -> None:
    dataset = build_offline_dataset(fixture_source())
    assert all(row.target_room_nights >= 0 for row in dataset.rows)


# --- 16. source provenance ---------------------------------------------------------------------


def test_the_source_url_is_https_and_pinned_to_a_commit() -> None:
    assert SOURCE_URL.startswith("https://")
    assert SOURCE_COMMIT in SOURCE_URL
    assert re.fullmatch(r"[0-9a-f]{40}", SOURCE_COMMIT)
    assert "/master/" not in SOURCE_URL
    assert "/main/" not in SOURCE_URL


def test_the_source_checksum_is_a_sha256_digest() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", SOURCE_SHA256)


def test_the_provenance_record_names_the_publication_and_its_licence() -> None:
    assert SOURCE_NAME == "hotel_booking_demand"
    assert SOURCE_DOI == "10.1016/j.dib.2018.11.126"
    assert "CC BY 4.0" in SOURCE_LICENSE
    assert "Data in Brief" in SOURCE_CITATION


def test_the_manifest_reproduces_the_provenance_record_verbatim() -> None:
    manifest = manifest_for(build_offline_dataset(fixture_source()))
    source_block = manifest["source"]
    assert isinstance(source_block, dict)
    assert source_block["url"] == SOURCE_URL
    assert source_block["sha256"] == SOURCE_SHA256
    assert source_block["doi"] == SOURCE_DOI
    assert source_block["license"] == SOURCE_LICENSE


# --- 17. no model, and no way to smuggle one in -------------------------------------------------


TRAINING_LIBRARIES = (
    "sklearn",
    "scikit_learn",
    "xgboost",
    "lightgbm",
    "catboost",
    "torch",
    "tensorflow",
    "keras",
    "prophet",
    "statsmodels",
    "pandas",
    "numpy",
)


#: The data-preparation half of `ml/`, named file by file. Stage 6.3 added an evaluation
#: pipeline beside these, which legitimately reaches scikit-learn through `ml/models.py`; the
#: repository-wide guard against the libraries that remain forbidden lives in
#: `test_demand_model_integration.py`. Scoping this one by name keeps it a true statement about
#: the dataset build rather than a claim about the directory that happens to still pass.
DATA_PREPARATION_MODULES = ("offline_demand.py", "build_demand_dataset.py")


@pytest.mark.parametrize("library", TRAINING_LIBRARIES)
def test_the_dataset_pipeline_imports_no_training_library(library: str) -> None:
    """Stage 6.2 prepares data. A dependency that could fit a model has no business in it."""
    root = Path(__file__).resolve().parents[2] / "ml" / "pipelines"
    for name in DATA_PREPARATION_MODULES:
        source = (root / name).read_text(encoding="utf-8")
        assert f"import {library}" not in source, name
        assert f"from {library}" not in source, name


def test_the_identity_columns_describe_an_observation_and_not_a_prediction() -> None:
    """No score, no estimate, no model version: there is no model to attribute one to."""
    assert IDENTITY_COLUMNS[-1] == "target_room_nights"
    forbidden = ("predicted", "forecast", "score", "model")
    assert not [c for c in IDENTITY_COLUMNS if any(word in c for word in forbidden)]
