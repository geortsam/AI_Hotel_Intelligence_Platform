"""Independent audit of the three committed Stage 7.14 horizon datasets against the raw source.

    PYTHONPATH=backend python -m ml.pipelines.audit_horizon_on_books
    PYTHONPATH=backend python -m ml.pipelines.audit_horizon_on_books --source path/to/hotels.csv

Recomputes, for every committed row of ``demand_daily_h{7,14,28}_v1``, two columns straight from
the raw booking rows, and compares them with what is committed:

* ``on_books_room_nights_at_cutoff`` -- room nights of bookings **entered on or before the cutoff
  day** (``target - h``) and **not cancelled on or before it** (a stay that checked out never
  was);
* ``target_room_nights`` -- room nights of stays that checked out.

It also checks that each row's ``prediction_cutoff`` is the end of ``target - h`` and that
``rooms_existing_at_cutoff`` is empty.

## Why it shares no code with the pipeline

The pipeline builds these columns in ``ml.pipelines.offline_demand`` and ``ml.horizons`` on top of
the Stage 6.1 contract in ``app.ml.dataset``. An audit that called any of them would be checking
that code against itself. So this module imports **only the standard library** -- a test pins
that -- and computes each night directly from the definition: one booking, one night at a time,
no series, no calendar, no shared helper. It is the Stage 7.14 audit that recorded "0
mismatches in 4,386 rows", committed unchanged in what it computes (see the history note below).

## What it reads, and what it trusts

* the raw source, whose SHA-256 must equal the one every committed horizon manifest records;
* each committed horizon CSV, whose SHA-256 must equal its manifest's ``processed_sha256``;
* each manifest's ``horizon_days`` and ``name``, which must be the horizon being audited.

A committed value that is not a whole number is a mismatch, and an **empty** one is reported, not
read as zero. The pipeline writes an on-the-books value for every row it emits -- ``0`` where
nothing was on the books at the cutoff, because a row's date is an observed one and its
on-the-books is counted over the same records as its target -- so an empty value is a dataset
this code did not produce. On the committed 300-row excerpt the audit and the pipeline agree on
every row, zeros included, and the tests pin that.

It writes nothing, reaches no network, and is not part of the shipped image (the runtime stage
copies ``ml/`` modules by name; a test pins the import closure).

## What it does not establish

That the committed datasets are what the source says under this definition. Not that the
definition is right for production (the source has day-resolution lead times and no status
history -- see ``docs/ml-multi-horizon.md`` §7), and nothing about accuracy or business value.

## History

Stage 7.14 ran this as a one-off script (2026-09-26) and quoted its result in the documentation.
F18 commits it. The computation is the recorded script's; what changed is the plumbing: paths
are the repository's, the source and dataset digests are verified first, a failure exits
non-zero, and a raw row it cannot read or a hotel it does not know stops the audit instead of
being skipped or folded into the other hotel. On the pinned source both counts are zero.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPOSITORY_ROOT / "ml" / "data" / "raw" / "demand_daily_v1_source.csv"

#: The horizons Stage 7.14 committed, in its order. Each is audited; none may be missing.
HORIZONS = (7, 14, 28)

MONTHS = {
    name: number
    for number, name in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}
HOTEL_KEYS = {"City Hotel": "city_hotel", "Resort Hotel": "resort_hotel"}
CHECKED_OUT = "Check-Out"

#: How many individual mismatches are printed per horizon; the counts are always complete.
SHOWN = 20


class AuditError(RuntimeError):
    """The audit could not run as specified: a digest, a file or a source row is wrong."""


@dataclass(frozen=True)
class Booking:
    hotel: str
    arrival: dt.date
    nights: int
    booked: dt.date
    status: str
    status_date: dt.date


@dataclass(frozen=True)
class Mismatch:
    column: str
    hotel: str
    target: dt.date
    committed: str
    recomputed: str

    def __str__(self) -> str:
        return (
            f"{self.column} {self.hotel} {self.target}: "
            f"committed {self.committed or '(empty)'}, recomputed {self.recomputed or '(empty)'}"
        )


@dataclass
class HorizonResult:
    horizon_days: int
    rows: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)

    def count(self, column: str) -> int:
        return sum(m.column == column for m in self.mismatches)

    @property
    def clean(self) -> bool:
        return self.rows > 0 and not self.mismatches


# --- reading -------------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_bookings(handle: TextIO) -> list[Booking]:
    """Every source row as a booking. A row that cannot be read stops the audit, with its line."""
    bookings = []
    unreadable: list[str] = []
    for line, row in enumerate(csv.DictReader(handle), start=2):
        try:
            arrival = dt.date(
                int(row["arrival_date_year"]),
                MONTHS[row["arrival_date_month"]],
                int(row["arrival_date_day_of_month"]),
            )
            nights = int(row["stays_in_weekend_nights"]) + int(row["stays_in_week_nights"])
            booked = arrival - dt.timedelta(days=int(row["lead_time"]))
            status = row["reservation_status"]
            status_date = dt.date.fromisoformat(row["reservation_status_date"])
            hotel = HOTEL_KEYS[row["hotel"]]
        except (ValueError, KeyError) as problem:
            unreadable.append(f"line {line}: {type(problem).__name__}: {problem}")
            continue
        bookings.append(Booking(hotel, arrival, nights, booked, status, status_date))
    if unreadable:
        shown = "; ".join(unreadable[:SHOWN])
        raise AuditError(f"{len(unreadable)} source row(s) could not be read: {shown}")
    return bookings


# --- the independent recount ---------------------------------------------------------------------


def recount(
    bookings: Iterable[Booking], horizon_days: int
) -> tuple[dict[tuple[str, dt.date], int], dict[tuple[str, dt.date], int]]:
    """(on the books at ``night - h``, realised) room nights per (hotel, night)."""
    on_books: dict[tuple[str, dt.date], int] = defaultdict(int)
    realised: dict[tuple[str, dt.date], int] = defaultdict(int)
    for booking in bookings:
        for step in range(booking.nights):
            night = booking.arrival + dt.timedelta(days=step)
            cutoff = night - dt.timedelta(days=horizon_days)
            if booking.status == CHECKED_OUT:
                realised[(booking.hotel, night)] += 1
            entered = booking.booked <= cutoff
            still_live = booking.status == CHECKED_OUT or booking.status_date > cutoff
            if entered and still_live:
                on_books[(booking.hotel, night)] += 1
    return on_books, realised


def compare(
    rows: Iterable[Mapping[str, str]],
    horizon_days: int,
    on_books: Mapping[tuple[str, dt.date], int],
    realised: Mapping[tuple[str, dt.date], int],
) -> HorizonResult:
    """Check every committed row; none is skipped."""
    result = HorizonResult(horizon_days)
    for row in rows:
        result.rows += 1
        hotel, target = row["hotel_key"], dt.date.fromisoformat(row["target_date"])
        key = (hotel, target)
        checks = (
            ("on_books_room_nights_at_cutoff", str(on_books.get(key, 0))),
            ("target_room_nights", str(realised.get(key, 0))),
        )
        for column, expected in checks:
            # A committed value that is not a whole number -- including an empty one -- is a
            # mismatch against the recount. It is never read as zero: that would be deciding
            # what the pipeline meant rather than checking what it wrote.
            try:
                agrees = int(row[column]) == int(expected)
            except ValueError:
                agrees = False
            if not agrees:
                result.mismatches.append(Mismatch(column, hotel, target, row[column], expected))
        # The cutoff is midnight UTC at the END of target - h, so its date is the day after.
        cutoff_day = dt.date.fromisoformat(row["prediction_cutoff"][:10]) - dt.timedelta(days=1)
        if cutoff_day != target - dt.timedelta(days=horizon_days):
            expected_day = target - dt.timedelta(days=horizon_days - 1)
            result.mismatches.append(
                Mismatch(
                    "prediction_cutoff",
                    hotel,
                    target,
                    row["prediction_cutoff"],
                    f"{expected_day}T00:00:00+00:00",
                )
            )
        if row["rooms_existing_at_cutoff"] != "":
            result.mismatches.append(
                Mismatch(
                    "rooms_existing_at_cutoff", hotel, target, row["rooms_existing_at_cutoff"], ""
                )
            )
    return result


# --- the committed side --------------------------------------------------------------------------


def dataset_paths(root: Path, horizon_days: int) -> tuple[Path, Path]:
    name = f"demand_daily_h{horizon_days}_v1"
    return (
        root / "ml" / "data" / "processed" / f"{name}.csv",
        root / "ml" / "manifests" / f"{name}.json",
    )


def load_manifest(path: Path, horizon_days: int) -> dict[str, object]:
    if not path.is_file():
        raise AuditError(f"h={horizon_days}: manifest {path} is missing")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    dataset, source = manifest.get("dataset"), manifest.get("source")
    if not isinstance(dataset, dict) or not isinstance(source, dict):
        raise AuditError(f"h={horizon_days}: {path} has no dataset or source block")
    if (
        dataset.get("horizon_days") != horizon_days
        or dataset.get("name") != f"demand_daily_h{horizon_days}_v1"
    ):
        raise AuditError(
            f"h={horizon_days}: {path} describes {dataset.get('name')} "
            f"at h={dataset.get('horizon_days')}"
        )
    return manifest


def expected_source_digest(manifests: Mapping[int, dict[str, object]]) -> str:
    digests = {h: m["source"]["sha256"] for h, m in manifests.items()}  # type: ignore[index]
    if len(set(digests.values())) != 1:
        raise AuditError(f"the horizon manifests disagree on the source SHA-256: {digests}")
    digest = next(iter(digests.values()))
    if not isinstance(digest, str) or len(digest) != 64:
        raise AuditError(f"the manifests record no usable source SHA-256: {digest!r}")
    return digest


# --- the run -------------------------------------------------------------------------------------


def audit(
    source: Path, *, root: Path = REPOSITORY_ROOT, horizons: Sequence[int] = HORIZONS
) -> list[HorizonResult]:
    """Verify every digest, then recount and compare every row at every horizon."""
    if not horizons:
        raise AuditError("no horizon to audit")
    manifests = {h: load_manifest(dataset_paths(root, h)[1], h) for h in horizons}

    expected = expected_source_digest(manifests)
    if not source.is_file():
        raise AuditError(f"the raw source {source} does not exist")
    actual = sha256_file(source)
    if actual != expected:
        raise AuditError(
            f"source SHA-256 mismatch: {source} is {actual}, the committed manifests record "
            f"{expected}. This is not the file the datasets were built from."
        )

    for h in horizons:
        csv_path, _ = dataset_paths(root, h)
        if not csv_path.is_file():
            raise AuditError(f"h={h}: dataset {csv_path} is missing")
        committed = manifests[h]["dataset"]["processed_sha256"]  # type: ignore[index]
        if sha256_file(csv_path) != committed:
            raise AuditError(f"h={h}: {csv_path} does not match its manifest's processed_sha256")

    with source.open(newline="", encoding="utf-8") as handle:
        bookings = read_bookings(handle)

    results = []
    for h in horizons:
        on_books, realised = recount(bookings, h)
        with dataset_paths(root, h)[0].open(newline="", encoding="utf-8") as handle:
            result = compare(csv.DictReader(handle), h, on_books, realised)
        if result.rows == 0:
            raise AuditError(f"h={h}: the committed dataset has no rows to audit")
        results.append(result)
    return results


def main(argv: Sequence[str] | None = None, *, root: Path = REPOSITORY_ROOT) -> int:
    """Exit 0 when every row matches, 1 on any mismatch, 2 when the audit cannot run."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="the raw source CSV")
    args = parser.parse_args(argv)

    try:
        results = audit(args.source, root=root)
    except (AuditError, OSError) as failure:
        print(f"audit FAILED: {failure}", file=sys.stderr)
        return 2

    print(f"source {args.source}: SHA-256 matches the committed manifests")
    for result in results:
        print(
            f"h={result.horizon_days}: rows={result.rows} "
            f"on_books mismatches={result.count('on_books_room_nights_at_cutoff')} "
            f"target mismatches={result.count('target_room_nights')} "
            f"cutoff mismatches={result.count('prediction_cutoff')} "
            f"capacity mismatches={result.count('rooms_existing_at_cutoff')}"
        )
        for mismatch in result.mismatches[:SHOWN]:
            print(f"    {mismatch}")
        if len(result.mismatches) > SHOWN:
            print(f"    ... and {len(result.mismatches) - SHOWN} more")
    total = sum(r.rows for r in results)
    if all(result.clean for result in results):
        print(f"audit passed: {total} rows at {len(results)} horizons, 0 mismatches")
        return 0
    print("audit FAILED: the committed datasets disagree with the raw source", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
