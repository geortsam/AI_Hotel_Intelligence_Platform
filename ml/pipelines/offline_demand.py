"""Turn a published hotel-booking dataset into Stage 6.1 daily demand rows.

Stage 6.2 exists because Stage 6.1 measured, rather than assumed, that the demo PostgreSQL
database holds too little history to train on: 48 distinct dates for a single hotel. The answer
is not to manufacture history. It is to acquire real history from a documented source and to
put it through the *same* contract.

**Nothing here trains anything.** There is no estimator, no fitted parameter and no accuracy
figure in this stage, and no dependency that could produce one: the module is standard library
only, plus :mod:`app.ml.dataset` for the Stage 6.1 contract it must satisfy.

## The source

``hotels.csv`` from the R4DS TidyTuesday repository, which is the two data files published with
Antonio, de Almeida & Nunes (2019), *Hotel booking demand datasets*, Data in Brief 22, 41-49,
concatenated with a ``hotel`` column and snake-cased names by the cleaning script recorded in
that repository. The article is open access under CC BY 4.0 (confirmed from the Crossref record
for the DOI, not from the publisher's page furniture); the redistributing repository is CC0
1.0. :data:`SOURCE_URL` pins a commit rather than a branch, so the bytes it returns cannot move
underneath a rebuild.

This is an **offline training source**. It is two real Portuguese hotels observed 2015-2017. It
is emphatically *not* the production hotel's history, and nothing here pretends otherwise --
which is why offline hotel identity is a UUID**5** in a namespace of this pipeline's own
(:func:`offline_hotel_id`), distinguishable from a production ``hotels.public_id`` by its
version field alone.

## The target, and why it is not the obvious column

Stage 6.1's target is a **room night**: one room, occupied, on one calendar date. The source is
a table of *bookings*. A booking is not a room night, and counting arrivals -- the natural
reading of one row, one record -- would silently redefine the target.

So each occupancy booking is expanded across ``[arrival, arrival + nights)``: half-open, the
check-out day excluded, exactly as ``booking_room_nights`` is built in the production schema.
Occupancy means ``reservation_status == "Check-Out"``, the source's term for a guest who
checked in and departed; ``Canceled`` and ``No-Show`` hold no room. That is the same judgement
``OCCUPANCY_STATUSES`` encodes for the production database, made in the source's vocabulary.

## Truncation, which is the one thing that would quietly corrupt the target

The source contains bookings whose *arrival* falls in a fixed window. A night early in that
window may have been sold by a booking that arrived before it, and that booking is not in the
file; a night after the last arrival is missing every stay that would have started later. Both
ends therefore under-count, and an under-counted target does not announce itself -- it just
teaches a model that the season starts flat.

:func:`coverage_window` refuses to guess. A date is emitted only when every arrival that could
possibly have produced a night on it lies inside the source window:

    first covered date = earliest occupancy arrival + (longest realised stay - 1)
    last covered date  = latest occupancy arrival

Dates outside that are dropped as *targets*, with the count reported, rather than being kept
and quietly believed.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TextIO

from app.ml.dataset import (
    DATASET_VERSION,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_LAG_DAYS,
    DEFAULT_ROLLING_WINDOWS,
    FEATURE_VERSION,
    DatasetReport,
    DemandRow,
    HotelHistory,
    TemporalSplit,
    assert_no_feature_is_known_only_after_prediction,
    assert_split_is_chronological,
    build_feature_specs,
    build_rows,
    split_chronologically,
    validate_rows,
)

# --- provenance ------------------------------------------------------------------------------
#
# Recorded as code rather than prose so that the manifest cannot drift from the thing that was
# actually read. Every field here ends up in the manifest verbatim.

SOURCE_NAME = "hotel_booking_demand"
SOURCE_TITLE = "Hotel booking demand datasets"
SOURCE_CITATION = (
    "Antonio, N., de Almeida, A., & Nunes, L. (2019). Hotel booking demand datasets. "
    "Data in Brief, 22, 41-49."
)
SOURCE_DOI = "10.1016/j.dib.2018.11.126"
SOURCE_LICENSE = "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)"

#: Pinned to a commit, not to a branch. A branch URL is a moving target and would make
#: "same source, same code, same output" unverifiable the moment the branch moved.
SOURCE_COMMIT = "d75aaa0d31596ad6487ae0db20138067d301e031"
SOURCE_URL = (
    "https://raw.githubusercontent.com/rfordatascience/tidytuesday/"
    f"{SOURCE_COMMIT}/data/2020/2020-02-11/hotels.csv"
)
SOURCE_REDISTRIBUTION = (
    "R4DS Online Learning Community, TidyTuesday 2020-02-11 (repository: CC0 1.0)"
)

#: SHA-256 of the file :data:`SOURCE_URL` serves. Checked on every run: a source that changed
#: is a different dataset, and it should stop the pipeline rather than flow into a manifest.
SOURCE_SHA256 = "7c2ae42a7353905ea136e5c2287f17c92c5435826598bfbb8491c6f0c7b1fc06"
SOURCE_BYTES = 16_855_599
SOURCE_ROWS = 119_390

#: The offline dataset's own name and version. The dataset and feature versions come from
#: Stage 6.1 -- this pipeline does not get to define its own contract.
OFFLINE_DATASET_NAME = "demand_daily_v1"

REQUIRED_COLUMNS: tuple[str, ...] = (
    "hotel",
    "lead_time",
    "arrival_date_year",
    "arrival_date_month",
    "arrival_date_day_of_month",
    "stays_in_weekend_nights",
    "stays_in_week_nights",
    "reservation_status",
    "reservation_status_date",
)

#: Month names are matched from this table, not with ``%B``: ``strptime`` resolves month names
#: against the process locale, so a machine running under a non-English locale would reject
#: every row. A dataset whose contents depend on an environment variable is not reproducible.
MONTH_NUMBERS: Mapping[str, int] = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}

#: Source label -> offline key. An explicit table, not a slugifier: an unexpected label is a
#: sign the source changed, and that must raise rather than invent a third hotel.
HOTEL_KEYS: Mapping[str, str] = {
    "Resort Hotel": "resort_hotel",
    "City Hotel": "city_hotel",
}

#: The source's occupancy status: the guest checked in and departed. ``Canceled`` and
#: ``No-Show`` held no room, which is the same line ``OCCUPANCY_STATUSES`` draws in production.
OCCUPIED_STATUS = "Check-Out"
CANCELLED_STATUSES: frozenset[str] = frozenset({"Canceled", "No-Show"})
KNOWN_STATUSES: frozenset[str] = frozenset({OCCUPIED_STATUS, *CANCELLED_STATUSES})

_DAY = dt.timedelta(days=1)


class OfflineSourceError(Exception):
    """The source file is not the file this pipeline was written against.

    Separate from :class:`app.ml.dataset.DatasetError` on purpose: that family describes rows
    that break the dataset contract, this one describes an input that cannot be read at all.
    """


# --- offline identity ------------------------------------------------------------------------
#
# Stage 6.1 rows are keyed by a hotel's public UUID. An offline hotel has no production row and
# must never look as though it does, so its id is derived -- deterministically, because
# reproducibility demands it -- inside a namespace belonging to this pipeline.
#
# The .invalid TLD is reserved by RFC 2606 and is guaranteed never to resolve: the namespace is
# a name, not a location, and should not look like one that could be fetched.
OFFLINE_HOTEL_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://ai-hotel-intelligence-platform.invalid/ml/offline-hotels",
)


def offline_hotel_id(hotel_key: str) -> uuid.UUID:
    """A stable offline identity for one source hotel.

    UUID version 5 rather than 4, and that is the point rather than a detail: production
    ``hotels.public_id`` values are random version-4 UUIDs, so the version field alone proves
    an offline id was never a production id. A test asserts it.
    """
    return uuid.uuid5(OFFLINE_HOTEL_NAMESPACE, f"{SOURCE_NAME}:{hotel_key}")


# --- source records --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceBooking:
    """One source row, reduced to the six facts this pipeline is entitled to use.

    Everything else the source carries -- rate, country, market segment, party size, room type
    code -- is deliberately dropped. None of it is in the Stage 6.1 feature contract, and
    carrying it would invite a later stage to use a column the production database cannot
    supply.
    """

    hotel_key: str
    arrival_date: dt.date
    nights: int
    #: ``arrival_date - lead_time``. The source records lead time in whole days, so the booking
    #: instant is only ever known to the day; §"Cutoff" in the docs explains what that costs.
    booked_date: dt.date
    cancelled: bool
    #: The date the last status was set: the cancellation date when cancelled, otherwise the
    #: departure date. Only the former is used.
    status_date: dt.date

    @property
    def occupied(self) -> bool:
        return not self.cancelled and self.nights > 0

    @property
    def departure_date(self) -> dt.date:
        """Exclusive. The check-out day is not a night -- the same half-open rule the
        ``booking_room_nights`` CHECK constraint enforces in production."""
        return self.arrival_date + self.nights * _DAY


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    """A source row this pipeline would not use, and exactly why.

    Kept rather than counted so that "1,234 rows dropped" can always be answered with which
    rows and on account of which field. A pipeline that discards silently is one nobody can
    audit.
    """

    row_number: int
    field: str
    reason: str


@dataclass(frozen=True, slots=True)
class ParsedSource:
    """Everything read from the source: what was kept, what was not, and how much there was."""

    bookings: tuple[SourceBooking, ...]
    rejected: tuple[RejectedRecord, ...]
    rows_read: int

    @property
    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.rejected:
            counts[record.reason] = counts.get(record.reason, 0) + 1
        return dict(sorted(counts.items()))


def validate_source_schema(fieldnames: Sequence[str] | None) -> None:
    """Refuse a file that is not the documented source.

    The columns are checked by name before a single row is parsed, because the failure this
    prevents is the quiet one: a renamed column read as an empty string turns into a rejected
    row, and a hundred thousand rejected rows look like a data-quality finding rather than the
    wrong file.
    """
    if not fieldnames:
        raise OfflineSourceError("the source file has no header row")
    missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
    if missing:
        raise OfflineSourceError(
            "the source file is missing required column(s): " + ", ".join(missing)
        )


def _parse_int(raw: str) -> int:
    """Strict: ``"3.0"`` and ``" 3"`` are not integers here.

    The source writes whole numbers as whole numbers. Accepting a float-looking string would
    mean accepting a file whose types have changed, which is precisely what
    :func:`validate_source_schema` and this function exist to catch.
    """
    return int(raw)


def parse_source_rows(rows: Iterable[Mapping[str, str]]) -> ParsedSource:
    """Read source rows into :class:`SourceBooking` values, recording every refusal.

    Row numbers are 1-based over the *file*, header included, so a reported number can be fed
    straight to ``sed -n 'Np'``.
    """
    bookings: list[SourceBooking] = []
    rejected: list[RejectedRecord] = []
    rows_read = 0

    for offset, row in enumerate(rows):
        rows_read += 1
        line = offset + 2  # +1 for the header, +1 for 1-based counting

        label = row.get("hotel", "")
        hotel_key = HOTEL_KEYS.get(label)
        if hotel_key is None:
            rejected.append(RejectedRecord(line, "hotel", f"unknown hotel label {label!r}"))
            continue

        month = MONTH_NUMBERS.get(row.get("arrival_date_month", ""))
        if month is None:
            rejected.append(
                RejectedRecord(
                    line,
                    "arrival_date_month",
                    f"unknown month name {row.get('arrival_date_month', '')!r}",
                )
            )
            continue

        try:
            arrival = dt.date(
                _parse_int(row["arrival_date_year"]),
                month,
                _parse_int(row["arrival_date_day_of_month"]),
            )
        except (KeyError, ValueError):
            rejected.append(
                RejectedRecord(line, "arrival_date_day_of_month", "arrival date is not a real date")
            )
            continue

        try:
            nights = _parse_int(row["stays_in_weekend_nights"]) + _parse_int(
                row["stays_in_week_nights"]
            )
        except (KeyError, ValueError):
            rejected.append(
                RejectedRecord(line, "stays_in_week_nights", "stay length is not an integer")
            )
            continue
        if nights < 0:
            rejected.append(
                RejectedRecord(line, "stays_in_week_nights", f"negative stay length ({nights})")
            )
            continue
        if nights == 0:
            # Real rows, and there are hundreds of them: a reservation that arrived and
            # departed the same day occupies no night. It cannot become an observation and it
            # cannot sit on the books either, so it is dropped -- but counted, because a
            # sudden change in how many there are would say something about the source.
            rejected.append(
                RejectedRecord(
                    line, "stays_in_week_nights", "zero-night booking occupies no room night"
                )
            )
            continue

        try:
            lead_time = _parse_int(row["lead_time"])
        except (KeyError, ValueError):
            rejected.append(RejectedRecord(line, "lead_time", "lead time is not an integer"))
            continue
        if lead_time < 0:
            rejected.append(RejectedRecord(line, "lead_time", f"negative lead time ({lead_time})"))
            continue

        status = row.get("reservation_status", "")
        if status not in KNOWN_STATUSES:
            rejected.append(
                RejectedRecord(line, "reservation_status", f"unknown status {status!r}")
            )
            continue

        try:
            status_date = dt.date.fromisoformat(row["reservation_status_date"])
        except (KeyError, ValueError):
            rejected.append(
                RejectedRecord(
                    line, "reservation_status_date", "status date is not an ISO-8601 date"
                )
            )
            continue

        bookings.append(
            SourceBooking(
                hotel_key=hotel_key,
                arrival_date=arrival,
                nights=nights,
                booked_date=arrival - lead_time * _DAY,
                cancelled=status in CANCELLED_STATUSES,
                status_date=status_date,
            )
        )

    return ParsedSource(tuple(bookings), tuple(rejected), rows_read)


def read_source(handle: TextIO) -> ParsedSource:
    """Validate the header, then parse. The only file-shaped entry point in this module."""
    reader = csv.DictReader(handle)
    validate_source_schema(reader.fieldnames)
    return parse_source_rows(reader)


# --- target derivation -----------------------------------------------------------------------


def demand_by_date(bookings: Iterable[SourceBooking]) -> dict[str, dict[dt.date, int]]:
    """Realised room nights per hotel per calendar date.

    One booking contributes one room night to each date in ``[arrival, departure)``. Cancelled
    and no-show bookings contribute nothing: they held no room.
    """
    series: dict[str, dict[dt.date, int]] = defaultdict(dict)
    for booking in bookings:
        if not booking.occupied:
            continue
        hotel = series[booking.hotel_key]
        for step in range(booking.nights):
            day = booking.arrival_date + step * _DAY
            hotel[day] = hotel.get(day, 0) + 1
    return dict(series)


def coverage_window(bookings: Iterable[SourceBooking]) -> dict[str, tuple[dt.date, dt.date]]:
    """The dates whose realised demand the source can account for in full, per hotel.

    See the module docstring. The left bound is the only interesting one: a night on date *D*
    can have been produced by an arrival as early as ``D - longest_stay + 1``, so *D* is only
    trustworthy once that earliest possible arrival is itself inside the source window.

    ``longest_stay`` is the longest stay this hotel actually realised. It cannot bound a stay
    that started before the window and ran longer than anything inside it -- nothing can, from
    this file -- and the documentation says so rather than the name implying otherwise.
    """
    arrivals: dict[str, list[dt.date]] = defaultdict(list)
    longest: dict[str, int] = defaultdict(int)
    for booking in bookings:
        if not booking.occupied:
            continue
        arrivals[booking.hotel_key].append(booking.arrival_date)
        longest[booking.hotel_key] = max(longest[booking.hotel_key], booking.nights)

    windows: dict[str, tuple[dt.date, dt.date]] = {}
    for hotel_key, dates in arrivals.items():
        first = min(dates) + (longest[hotel_key] - 1) * _DAY
        last = max(dates)
        if first > last:
            raise OfflineSourceError(
                f"{hotel_key}: the longest realised stay ({longest[hotel_key]} nights) is longer "
                f"than the whole arrival window, so no date is fully covered"
            )
        windows[hotel_key] = (first, last)
    return windows


def on_books_by_date(
    bookings: Iterable[SourceBooking],
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict[str, dict[dt.date, int]]:
    """Room nights on the books at each target date's prediction cutoff.

    A booking is on the books for target date *D* when it had been entered by the cutoff and
    had not been cancelled by then -- the same two conditions the production repository writes
    as ``booked_at < cutoff`` and ``cancelled_at IS NULL OR cancelled_at >= cutoff``.

    Both conditions are monotone in *D*, which turns a per-date scan into an interval. With
    ``c = D - horizon``:

        entered by the cutoff    booked_date <= c      <=>  D >= booked_date + horizon
        still live at the cutoff status_date  > c      <=>  D <  status_date + horizon

    so a booking contributes to a contiguous run of target dates and nothing else. That matters
    at this size: the source expands to roughly 400,000 room nights.

    Cancelled bookings are counted while they were live. That is not an oversight -- it is what
    "on the books at the cutoff" means, and dropping them would leak the knowledge that they
    were going to be cancelled.
    """
    series: dict[str, dict[dt.date, int]] = defaultdict(dict)
    horizon = horizon_days * _DAY
    for booking in bookings:
        if booking.nights <= 0:
            continue
        start = max(booking.arrival_date, booking.booked_date + horizon)
        end = booking.departure_date
        if booking.cancelled:
            end = min(end, booking.status_date + horizon)
        hotel = series[booking.hotel_key]
        day = start
        while day < end:
            hotel[day] = hotel.get(day, 0) + 1
            day += _DAY
    return dict(series)


def build_histories(
    parsed: ParsedSource,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> tuple[tuple[HotelHistory, ...], dict[str, tuple[dt.date, dt.date]], int]:
    """Assemble Stage 6.1 histories, restricted to the fully covered dates.

    Returns the histories, the window each hotel was restricted to, and how many realised
    hotel-days were dropped for falling outside one -- a number worth reporting rather than
    absorbing.

    ``rooms_existing_by_date`` is left **empty**, which makes
    ``rooms_existing_at_cutoff`` ``None`` on every row. The source publishes no room inventory,
    and there is no honest way to produce that feature from it. Leaving the mapping empty is
    the whole of the fix: Stage 6.1 already treats an absent value as missing rather than zero,
    so the feature reports as unavailable instead of quietly reading as "no rooms".
    """
    demand = demand_by_date(parsed.bookings)
    on_books = on_books_by_date(parsed.bookings, horizon_days=horizon_days)
    windows = coverage_window(parsed.bookings)

    histories: list[HotelHistory] = []
    dropped = 0
    for hotel_key in sorted(demand):
        first, last = windows[hotel_key]
        hotel_demand = demand[hotel_key]
        covered = {day: count for day, count in hotel_demand.items() if first <= day <= last}
        dropped += len(hotel_demand) - len(covered)
        hotel_on_books = on_books.get(hotel_key, {})
        histories.append(
            HotelHistory(
                hotel_public_id=offline_hotel_id(hotel_key),
                demand_by_date=covered,
                on_books_by_date={
                    day: count for day, count in hotel_on_books.items() if day in covered
                },
                rooms_existing_by_date={},
            )
        )
    return tuple(histories), windows, dropped


# --- the offline dataset ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OfflineDataset:
    """The processed dataset, its split, and everything needed to explain both."""

    rows: tuple[DemandRow, ...]
    split: TemporalSplit
    report: DatasetReport
    feature_names: tuple[str, ...]
    hotel_keys: Mapping[uuid.UUID, str]
    coverage: Mapping[str, tuple[dt.date, dt.date]]
    parsed: ParsedSource
    horizon_days: int
    dates_outside_coverage: int
    dataset_version: str = DATASET_VERSION
    feature_version: str = FEATURE_VERSION
    lag_days: tuple[int, ...] = tuple(DEFAULT_LAG_DAYS)
    rolling_windows: tuple[int, ...] = tuple(DEFAULT_ROLLING_WINDOWS)

    @property
    def partition_of(self) -> dict[tuple[uuid.UUID, dt.date], str]:
        """Which partition each row landed in, keyed the way a row identifies itself."""
        mapping: dict[tuple[uuid.UUID, dt.date], str] = {}
        for name, part in (
            ("train", self.split.train),
            ("validation", self.split.validation),
            ("test", self.split.test),
        ):
            for row in part:
                mapping[(row.hotel_public_id, row.target_date)] = name
        return mapping


def build_offline_dataset(
    parsed: ParsedSource,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    lag_days: Sequence[int] = DEFAULT_LAG_DAYS,
    rolling_windows: Sequence[int] = DEFAULT_ROLLING_WINDOWS,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> OfflineDataset:
    """Source records in, Stage 6.1 rows plus a chronological split out.

    Every rule applied here belongs to Stage 6.1 and is called, not reimplemented:
    :func:`build_rows` for the features, :func:`validate_rows` for the invariants,
    :func:`split_chronologically` for the partitions. That is the point of the stage -- an
    offline dataset that satisfied a *different* contract would be no use to a model that has
    to serve against the production one.
    """
    specs = build_feature_specs(lag_days, rolling_windows)
    assert_no_feature_is_known_only_after_prediction(specs)

    histories, coverage, dropped = build_histories(parsed, horizon_days=horizon_days)
    if not histories:
        raise OfflineSourceError("the source produced no occupancy history for any hotel")

    rows = build_rows(
        histories,
        horizon_days=horizon_days,
        lag_days=lag_days,
        rolling_windows=rolling_windows,
    )
    report = validate_rows(rows)
    split = split_chronologically(
        rows,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    assert_split_is_chronological(split)

    return OfflineDataset(
        rows=rows,
        split=split,
        report=report,
        feature_names=tuple(spec.name for spec in specs),
        hotel_keys={offline_hotel_id(key): key for key in coverage},
        coverage=coverage,
        parsed=parsed,
        horizon_days=horizon_days,
        dates_outside_coverage=dropped,
        lag_days=tuple(lag_days),
        rolling_windows=tuple(rolling_windows),
    )


# --- serialisation ---------------------------------------------------------------------------
#
# The processed dataset is bytes, and its checksum is the thing two runs are compared on, so
# every choice below is made for determinism rather than for looks.

IDENTITY_COLUMNS: tuple[str, ...] = (
    "hotel_key",
    "hotel_public_id",
    "target_date",
    "horizon_days",
    "prediction_cutoff",
    "partition",
    "target_room_nights",
)


def _format_value(value: float | int | None) -> str:
    """One value, one unambiguous spelling.

    ``None`` is the empty string, and that is deliberate: a missing lag must not be readable as
    a zero by anything that loads this file, in either direction. ``repr`` on a float is the
    shortest string that round-trips, which is stable across platforms and Python versions --
    ``str(value)`` for a float means the same thing today but has not always.
    """
    if value is None:
        return ""
    if isinstance(value, bool):  # pragma: no cover - defensive; no feature is boolean
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return repr(value)


def dataset_header(dataset: OfflineDataset) -> tuple[str, ...]:
    return (*IDENTITY_COLUMNS, *dataset.feature_names)


def dataset_records(dataset: OfflineDataset) -> list[tuple[str, ...]]:
    """Every row as strings, in the order :func:`build_rows` fixed.

    That order -- target date, then hotel public id -- is Stage 6.1's, and it is why the same
    source produces the same bytes regardless of the order hotels were read in.
    """
    partitions = dataset.partition_of
    records: list[tuple[str, ...]] = []
    for row in dataset.rows:
        records.append(
            (
                dataset.hotel_keys[row.hotel_public_id],
                str(row.hotel_public_id),
                row.target_date.isoformat(),
                str(row.horizon_days),
                row.prediction_cutoff.isoformat(),
                partitions[(row.hotel_public_id, row.target_date)],
                str(row.target_room_nights),
                *(_format_value(row.features[name]) for name in dataset.feature_names),
            )
        )
    return records


def serialise_dataset(dataset: OfflineDataset) -> bytes:
    """The processed dataset as the exact bytes that get checksummed and written.

    LF endings explicitly, UTF-8 explicitly, no BOM, no dialect guessing. The default
    ``csv.writer`` would emit CRLF, which is fine in a file and fatal in a checksum compared
    between a Windows developer and a Linux CI runner.
    """
    lines = [",".join(dataset_header(dataset))]
    lines.extend(",".join(record) for record in dataset_records(dataset))
    return ("\n".join(lines) + "\n").encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --- manifest --------------------------------------------------------------------------------


def _target_statistics(rows: Sequence[DemandRow]) -> dict[str, float | int]:
    values = sorted(row.target_room_nights for row in rows)
    count = len(values)
    middle = count // 2
    median = float(values[middle]) if count % 2 else (values[middle - 1] + values[middle]) / 2
    return {
        "count": count,
        "min": values[0],
        "median": median,
        "mean": round(sum(values) / count, 6),
        "max": values[-1],
        "zero_demand_days": sum(1 for value in values if value == 0),
    }


def _partition_summary(rows: Sequence[DemandRow]) -> dict[str, object]:
    dates = [row.target_date for row in rows]
    return {
        "rows": len(rows),
        "date_min": min(dates).isoformat(),
        "date_max": max(dates).isoformat(),
        "target": _target_statistics(rows),
    }


def build_manifest(
    dataset: OfflineDataset,
    *,
    source_checksum: str,
    processed_checksum: str,
    processed_bytes: int,
    generated_at: dt.datetime | None = None,
) -> dict[str, object]:
    """The dataset's identity card.

    Split in two on purpose. Everything under ``"dataset"``, ``"source"`` and ``"partitions"``
    is a deterministic function of the source bytes and this code -- rerun the pipeline and it
    comes back identical. ``"generation"`` holds the one thing that cannot be, the wall clock,
    and it is kept out of both checksums so that "the dataset changed" and "the dataset was
    rebuilt" stay different statements.
    """
    dates = [row.target_date for row in dataset.rows]
    manifest: dict[str, object] = {
        "dataset": {
            "name": OFFLINE_DATASET_NAME,
            "dataset_version": dataset.dataset_version,
            "feature_version": dataset.feature_version,
            "target": "daily hotel room-night demand",
            "grain": "one hotel group x one calendar date",
            "horizon_days": dataset.horizon_days,
            "lag_days": list(dataset.lag_days),
            "rolling_windows": list(dataset.rolling_windows),
            "rows": len(dataset.rows),
            "hotels": len(dataset.hotel_keys),
            "date_min": min(dates).isoformat(),
            "date_max": max(dates).isoformat(),
            "distinct_dates": len({row.target_date for row in dataset.rows}),
            "columns": list(dataset_header(dataset)),
            "processed_sha256": processed_checksum,
            "processed_bytes": processed_bytes,
            "target_statistics": _target_statistics(dataset.rows),
        },
        "source": {
            "name": SOURCE_NAME,
            "title": SOURCE_TITLE,
            "citation": SOURCE_CITATION,
            "doi": SOURCE_DOI,
            "license": SOURCE_LICENSE,
            "redistribution": SOURCE_REDISTRIBUTION,
            "url": SOURCE_URL,
            "commit": SOURCE_COMMIT,
            "sha256": source_checksum,
            "rows_read": dataset.parsed.rows_read,
            "bookings_parsed": len(dataset.parsed.bookings),
            "records_rejected": len(dataset.parsed.rejected),
            "rejection_counts": dataset.parsed.rejection_counts,
            "hotel_coverage": {
                key: {"first_covered": first.isoformat(), "last_covered": last.isoformat()}
                for key, (first, last) in sorted(dataset.coverage.items())
            },
            "hotel_ids": {key: str(offline_hotel_id(key)) for key in sorted(dataset.coverage)},
            "hotel_days_outside_coverage": dataset.dates_outside_coverage,
        },
        "partitions": {
            "train": _partition_summary(dataset.split.train),
            "validation": _partition_summary(dataset.split.validation),
            "test": _partition_summary(dataset.split.test),
            "train_end": dataset.split.train_end.isoformat(),
            "validation_end": dataset.split.validation_end.isoformat(),
            "policy": "chronological by date; no shuffle, no random seed",
        },
        "quality": {
            "rows_with_complete_features": dataset.report.rows_with_complete_features,
            "missing_feature_counts": dict(dataset.report.missing_feature_counts),
            "notes": list(dataset.report.notes),
        },
        "generation": {
            "note": (
                "Wall-clock metadata only. Deliberately excluded from every checksum above: "
                "rebuilding the dataset must not change its identity."
            ),
            "generated_at": (generated_at or dt.datetime.now(dt.UTC)).isoformat(),
            "pipeline": "ml/pipelines/offline_demand.py",
            "model_trained": False,
        },
    }
    return manifest


def serialise_manifest(manifest: Mapping[str, object]) -> bytes:
    """Sorted keys, two-space indent, trailing newline, LF. A diffable, stable file."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class BuildResult:
    """What one end-to-end run produced, before anything is written to disk."""

    dataset: OfflineDataset
    processed: bytes
    processed_checksum: str
    manifest: dict[str, object] = field(default_factory=dict)


def build_from_source(
    handle: TextIO,
    *,
    source_checksum: str,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    generated_at: dt.datetime | None = None,
) -> BuildResult:
    """Read, transform, serialise, checksum and describe -- in one deterministic pass."""
    parsed = read_source(handle)
    dataset = build_offline_dataset(parsed, horizon_days=horizon_days)
    processed = serialise_dataset(dataset)
    checksum = sha256_hex(processed)
    manifest = build_manifest(
        dataset,
        source_checksum=source_checksum,
        processed_checksum=checksum,
        processed_bytes=len(processed),
        generated_at=generated_at,
    )
    return BuildResult(dataset, processed, checksum, manifest)
