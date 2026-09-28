"""F18 -- the independent on-the-books audit: its machinery, at fixture level.

**What these tests are not.** They are not the raw-source verification. CI has neither the 16.9 MB
raw source nor the right to download it (docs/ml-training-data.md §4), so the full audit of the
committed ``demand_daily_h{7,14,28}_v1`` datasets is an operator-run check:
``python -m ml.pipelines.audit_horizon_on_books`` (see docs/ml-multi-horizon.md, "Verifying
against the raw source"). Nothing here stands in for it.

What runs here is the audit's own code against throwaway repository roots built from the
committed 300-row verbatim excerpt of the source (``fixtures/hotel_booking_demand_sample.csv``):

* ``excerpt_root`` -- the three horizon datasets and manifests **the real pipeline** builds from
  the excerpt, as built. On these the audit and the pipeline agree on every target, cutoff and
  capacity value and on every on-the-books value the pipeline wrote; the only difference is that
  on this sparse sample the pipeline leaves some on-the-books values empty where the recount is
  0 (the Stage 6.1 contract writes no value for a date its extract has no entry for). The first
  test pins exactly that, and nothing looser. The committed full-source datasets have no such
  row -- the operator run reports 0 mismatches.
* ``agreeing_root`` -- the same datasets without those rows, so the success path and every
  deliberate break can be checked against a baseline of zero mismatches.
"""

from __future__ import annotations

import ast
import csv
import datetime as dt
import hashlib
import io
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from ml.horizons import HORIZONS as SPECS
from ml.horizons import build_horizon_dataset
from ml.pipelines.audit_horizon_on_books import (
    DEFAULT_SOURCE,
    HORIZONS,
    AuditError,
    Booking,
    audit,
    main,
    recount,
)
from ml.pipelines.build_demand_dataset import DEFAULT_RAW
from ml.pipelines.offline_demand import SOURCE_SHA256, read_source, serialise_manifest
from tests.backend.test_production_packaging import copied_ml_files, ml_import_closure
from tests.ml.test_offline_demand import FIXTURE

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = "ml/pipelines/audit_horizon_on_books.py"
ON_BOOKS = "on_books_room_nights_at_cutoff"

Root = tuple[Path, Path]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def paths(root: Path, h: int) -> tuple[Path, Path]:
    name = f"demand_daily_h{h}_v1"
    return root / "ml/data/processed" / f"{name}.csv", root / "ml/manifests" / f"{name}.json"


def read_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_rows(root: Path, h: int, fields: list[str], rows: list[dict[str, str]]) -> None:
    """Write a dataset and re-record its digest, so the digest check passes and only the values
    are under test."""
    csv_path, manifest_path = paths(root, h)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    csv_path.write_bytes(buffer.getvalue().encode("utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dataset"]["processed_sha256"] = sha256(csv_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def record_source_digest(root: Path, digest: str) -> None:
    for h in HORIZONS:
        manifest_path = paths(root, h)[1]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["sha256"] = digest
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def excerpt_root(tmp_path: Path) -> Root:
    """(root, source): the excerpt as the source, and the pipeline's three datasets built from it.

    The excerpt's checksum is taken here, from the bytes on this checkout, only to write the
    throwaway manifests; nothing compares it with a pinned value."""
    root = tmp_path / "repo"
    source = tmp_path / "source.csv"
    shutil.copyfile(FIXTURE, source)
    raw = source.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    for spec in SPECS:
        parsed = read_source(io.StringIO(raw.decode("utf-8"), newline=""))
        built = build_horizon_dataset(parsed, spec, source_checksum=checksum)
        csv_path, manifest_path = paths(root, spec.horizon_days)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_bytes(built.processed)
        manifest_path.write_bytes(serialise_manifest(built.manifest))
    return root, source


@pytest.fixture
def agreeing_root(excerpt_root: Root) -> Root:
    root, source = excerpt_root
    for h in HORIZONS:
        fields, rows = read_rows(paths(root, h)[0])
        write_rows(root, h, fields, [row for row in rows if row[ON_BOOKS] != ""])
    return root, source


# --- fixture-level agreement ---------------------------------------------------------------------


def test_on_the_excerpt_the_only_difference_is_an_empty_value_where_the_recount_is_zero(
    excerpt_root: Root,
) -> None:
    root, source = excerpt_root

    results = audit(source, root=root)

    assert [r.horizon_days for r in results] == [7, 14, 28]
    for result in results:
        _, rows = read_rows(paths(root, result.horizon_days)[0])
        empty = sum(row[ON_BOOKS] == "" for row in rows)
        assert result.rows == len(rows) > empty > 0, "every row audited; the sample has gaps"
        assert {(m.column, m.committed, m.recomputed) for m in result.mismatches} == {
            (ON_BOOKS, "", "0")
        }
        assert len(result.mismatches) == empty


def test_where_every_row_agrees_the_audit_passes_at_every_horizon(agreeing_root: Root) -> None:
    root, source = agreeing_root

    results = audit(source, root=root)

    assert [r.horizon_days for r in results] == [7, 14, 28]
    for result in results:
        assert result.rows > 0
        assert result.clean, result.mismatches[:3]


def test_the_command_reports_success_and_exits_0(
    agreeing_root: Root, capsys: pytest.CaptureFixture[str]
) -> None:
    root, source = agreeing_root

    assert main(["--source", str(source)], root=root) == 0
    out = capsys.readouterr().out
    assert "SHA-256 matches the committed manifests" in out
    for h in HORIZONS:
        assert f"h={h}: rows=" in out and "on_books mismatches=0 target mismatches=0" in out
    assert "audit passed:" in out and "0 mismatches" in out


def test_the_horizons_are_the_ones_stage_7_14_committed() -> None:
    assert HORIZONS == tuple(spec.horizon_days for spec in SPECS) == (7, 14, 28)


def test_the_audit_writes_nothing(excerpt_root: Root) -> None:
    root, source = excerpt_root
    before = {p: p.read_bytes() for p in [source, *root.rglob("*")] if p.is_file()}

    audit(source, root=root)

    after = {p: p.read_bytes() for p in [source, *root.rglob("*")] if p.is_file()}
    assert after == before


# --- digests -------------------------------------------------------------------------------------


def test_a_source_whose_digest_is_not_the_recorded_one_is_refused(agreeing_root: Root) -> None:
    root, source = agreeing_root
    record_source_digest(root, "0" * 64)

    with pytest.raises(AuditError, match="source SHA-256 mismatch"):
        audit(source, root=root)


def test_the_command_exits_2_on_a_wrong_source(
    agreeing_root: Root, capsys: pytest.CaptureFixture[str]
) -> None:
    root, source = agreeing_root
    record_source_digest(root, "0" * 64)

    assert main(["--source", str(source)], root=root) == 2
    assert "source SHA-256 mismatch" in capsys.readouterr().err


def test_a_missing_source_exits_2(agreeing_root: Root, tmp_path: Path) -> None:
    root, _ = agreeing_root
    assert main(["--source", str(tmp_path / "absent.csv")], root=root) == 2


def test_manifests_that_disagree_on_the_source_are_refused(agreeing_root: Root) -> None:
    root, source = agreeing_root
    manifest_path = paths(root, 14)[1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["sha256"] = "1" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AuditError, match="disagree on the source SHA-256"):
        audit(source, root=root)


def test_a_dataset_that_no_longer_matches_its_manifest_is_refused(agreeing_root: Root) -> None:
    root, source = agreeing_root
    csv_path = paths(root, 28)[0]
    csv_path.write_bytes(csv_path.read_bytes() + b"\n")

    with pytest.raises(AuditError, match=r"h=28: .* does not match its manifest"):
        audit(source, root=root)


def test_a_manifest_for_another_horizon_is_refused(agreeing_root: Root) -> None:
    root, source = agreeing_root
    manifest_path = paths(root, 7)[1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dataset"]["horizon_days"] = 14
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AuditError, match=r"h=7: .* describes"):
        audit(source, root=root)


def test_the_committed_manifests_name_the_pinned_raw_source() -> None:
    """The operator check verifies the real file against the digest Stage 6.2 pinned, and each
    committed dataset against its own manifest."""
    for h in HORIZONS:
        csv_path, manifest_path = paths(REPOSITORY_ROOT, h)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["source"]["sha256"] == SOURCE_SHA256
        assert manifest["dataset"]["processed_sha256"] == sha256(csv_path)


def test_the_default_source_is_the_pinned_raw_path() -> None:
    assert DEFAULT_SOURCE == DEFAULT_RAW
    assert DEFAULT_SOURCE.parent == REPOSITORY_ROOT / "ml" / "data" / "raw"


# --- mismatches are found and reported -----------------------------------------------------------

CHANGES: list[tuple[str, Callable[[str], str]]] = [
    (ON_BOOKS, lambda value: str(int(value) + 1)),
    (ON_BOOKS, lambda value: ""),
    ("target_room_nights", lambda value: str(int(value) + 1)),
    ("prediction_cutoff", lambda value: "2014-01-01T00:00:00+00:00"),
    ("rooms_existing_at_cutoff", lambda value: "120"),
]


@pytest.mark.parametrize("h", HORIZONS)
@pytest.mark.parametrize(
    ("column", "change"),
    CHANGES,
    ids=["on_books", "on_books_empty", "target", "cutoff", "capacity"],
)
def test_a_changed_value_is_reported_by_column_hotel_and_date(
    agreeing_root: Root, h: int, column: str, change: Callable[[str], str]
) -> None:
    root, source = agreeing_root
    fields, rows = read_rows(paths(root, h)[0])
    rows[3][column] = change(rows[3][column])
    write_rows(root, h, fields, rows)

    results = {r.horizon_days: r for r in audit(source, root=root)}

    [mismatch] = results[h].mismatches
    assert (mismatch.column, mismatch.hotel, str(mismatch.target), mismatch.committed) == (
        column,
        rows[3]["hotel_key"],
        rows[3]["target_date"],
        rows[3][column],
    )
    assert all(results[other].clean for other in HORIZONS if other != h)


def test_the_command_prints_each_mismatch_and_exits_1(
    agreeing_root: Root, capsys: pytest.CaptureFixture[str]
) -> None:
    root, source = agreeing_root
    fields, rows = read_rows(paths(root, 7)[0])
    before = rows[0]["target_room_nights"]
    rows[0]["target_room_nights"] = str(int(before) + 5)
    write_rows(root, 7, fields, rows)

    assert main(["--source", str(source)], root=root) == 1
    out = capsys.readouterr()
    assert "h=7:" in out.out and "target mismatches=1" in out.out
    assert (
        f"target_room_nights {rows[0]['hotel_key']} {rows[0]['target_date']}: "
        f"committed {int(before) + 5}, recomputed {before}"
    ) in out.out
    assert "audit FAILED" in out.err


def test_an_empty_committed_value_is_printed_as_empty(
    agreeing_root: Root, capsys: pytest.CaptureFixture[str]
) -> None:
    root, source = agreeing_root
    fields, rows = read_rows(paths(root, 14)[0])
    rows[0][ON_BOOKS] = ""
    write_rows(root, 14, fields, rows)

    assert main(["--source", str(source)], root=root) == 1
    assert "committed (empty), recomputed" in capsys.readouterr().out


# --- nothing is skipped --------------------------------------------------------------------------


@pytest.mark.parametrize("h", HORIZONS)
def test_a_missing_horizon_dataset_fails_rather_than_being_skipped(
    agreeing_root: Root, h: int
) -> None:
    root, source = agreeing_root
    paths(root, h)[0].unlink()

    with pytest.raises(AuditError, match=rf"h={h}: dataset .* is missing"):
        audit(source, root=root)


def test_a_missing_manifest_fails(agreeing_root: Root) -> None:
    root, source = agreeing_root
    paths(root, 14)[1].unlink()

    with pytest.raises(AuditError, match=r"h=14: manifest .* is missing"):
        audit(source, root=root)


def test_a_dataset_with_no_rows_fails(agreeing_root: Root) -> None:
    root, source = agreeing_root
    fields, _ = read_rows(paths(root, 7)[0])
    write_rows(root, 7, fields, [])

    with pytest.raises(AuditError, match="h=7: the committed dataset has no rows"):
        audit(source, root=root)


@pytest.mark.parametrize(
    ("bad_field", "value"),
    [("arrival_date_month", "Juli"), ("lead_time", "soon"), ("hotel", "Airport Hotel")],
)
def test_a_source_row_it_cannot_read_stops_the_audit_with_its_line(
    agreeing_root: Root, bad_field: str, value: str
) -> None:
    """The Stage 7.14 script skipped such a row, and folded an unknown hotel into the resort; on
    the pinned source there are none, and now one is an error rather than a silent change."""
    root, source = agreeing_root
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields, rows = list(reader.fieldnames or []), list(reader)
    rows[5][bad_field] = value
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    source.write_bytes(buffer.getvalue().encode("utf-8"))
    record_source_digest(root, sha256(source))

    with pytest.raises(AuditError, match=r"1 source row\(s\) could not be read: line 7"):
        audit(source, root=root)


def test_no_horizon_at_all_is_an_error(agreeing_root: Root) -> None:
    root, source = agreeing_root
    with pytest.raises(AuditError, match="no horizon"):
        audit(source, root=root, horizons=())


# --- the definition, one booking at a time -------------------------------------------------------

NIGHT = dt.date(2016, 6, 30)


def booking(
    *, booked_days_before: int, status: str = "Check-Out", status_day: dt.date = NIGHT
) -> list[Booking]:
    booked = NIGHT - dt.timedelta(days=booked_days_before)
    return [Booking("city_hotel", NIGHT, 1, booked, status, status_day)]


@pytest.mark.parametrize("h", HORIZONS)
def test_a_booking_entered_on_the_cutoff_day_counts_and_one_entered_after_does_not(h: int) -> None:
    on_books, _ = recount(booking(booked_days_before=h), h)
    assert on_books[("city_hotel", NIGHT)] == 1
    on_books, _ = recount(booking(booked_days_before=h - 1), h)
    assert on_books.get(("city_hotel", NIGHT), 0) == 0


@pytest.mark.parametrize("h", HORIZONS)
def test_a_cancellation_on_or_before_the_cutoff_is_off_the_books_and_one_after_is_on(
    h: int,
) -> None:
    cutoff = NIGHT - dt.timedelta(days=h)
    cancelled = booking(booked_days_before=h + 30, status="Canceled", status_day=cutoff)
    on_books, realised = recount(cancelled, h)
    assert on_books.get(("city_hotel", NIGHT), 0) == 0
    assert realised.get(("city_hotel", NIGHT), 0) == 0
    later = booking(
        booked_days_before=h + 30, status="Canceled", status_day=cutoff + dt.timedelta(1)
    )
    on_books, _ = recount(later, h)
    assert on_books[("city_hotel", NIGHT)] == 1


def test_only_stays_that_checked_out_are_realised_and_every_night_counts() -> None:
    booked = NIGHT - dt.timedelta(days=60)
    stay = [Booking("resort_hotel", NIGHT, 3, booked, "Check-Out", NIGHT)]
    no_show = [Booking("resort_hotel", NIGHT, 3, booked, "No-Show", NIGHT)]

    _, realised = recount(stay, 7)
    assert [realised[("resort_hotel", NIGHT + dt.timedelta(days=d))] for d in range(3)] == [1, 1, 1]
    _, realised = recount(no_show, 7)
    assert not realised


# --- independence and packaging ------------------------------------------------------------------


def test_the_audit_imports_only_the_standard_library() -> None:
    """Checking the pipeline with the pipeline proves nothing: no ml, no app, no third party."""
    tree = ast.parse((REPOSITORY_ROOT / AUDIT_PATH).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import"
            imported.add((node.module or "").split(".")[0])
    imported.discard("__future__")

    assert imported, "the scan found no import at all"
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    assert not {"ml", "app"} & imported


def test_the_audit_never_opens_the_network() -> None:
    source = (REPOSITORY_ROOT / AUDIT_PATH).read_text(encoding="utf-8")
    for banned in ("urllib", "http.", "socket", "requests", "urlopen"):
        assert banned not in source, banned


def test_the_audit_does_not_ship_in_the_image() -> None:
    assert AUDIT_PATH not in ml_import_closure()
    assert AUDIT_PATH not in copied_ml_files()
