"""The demo-data seed, without a database: determinism, dates, lead times and constraints.

`scripts/seed_demo.py` builds its whole dataset in memory before touching a database, so almost
everything about it can be asserted here. What only PostgreSQL can prove -- that the rows really
satisfy the schema's constraints and triggers, and read back exactly as planned -- is in
`tests/integration/test_demo_seed.py`.
"""

from __future__ import annotations

import ast
import collections
import dataclasses
import datetime as dt
import decimal
import itertools
import re
from pathlib import Path
from typing import Any

import pytest

from app.ml.timeseries import Observation, anomaly_assessability, measure_trend
from app.models.enums import (
    INVENTORY_HOLDING_STATUSES,
    BookingSource,
    BookingStatus,
    PaymentMethod,
    ReviewSource,
)
from scripts import seed_demo
from scripts.seed_demo import (
    HOTELS,
    DemoPlan,
    SeedConfig,
    SeedRefusedError,
    build_plan,
    fingerprint,
    logical_rows,
)

REFERENCE = dt.date(2026, 9, 30)
SOURCE = Path(seed_demo.__file__).read_text(encoding="utf-8")

#: The default demo, pinned. Built on Python 3.14 (Windows) and checked again by CI on Python
#: 3.12 (Linux): the same number on both is the cross-version, cross-platform determinism claim.
#: A deliberate change to the generator changes this, and the change should be reviewed as one.
DEFAULT_FINGERPRINT = "9f4c439a0eded7e4b5e4bc742b61388d5aaa6a08a909d6743c9306af2539c56d"


@pytest.fixture(scope="module")
def plan() -> DemoPlan:
    return build_plan(SeedConfig(reference_date=REFERENCE))


def as_of(plan: DemoPlan, slug: str) -> dt.datetime:
    return plan.as_of(next(spec for spec in HOTELS if spec.slug == slug))


# --- determinism --------------------------------------------------------------------------------


def test_the_same_seed_and_reference_date_build_the_same_dataset() -> None:
    config = SeedConfig(reference_date=REFERENCE)

    first, second = build_plan(config), build_plan(config)

    assert first == second
    assert fingerprint(logical_rows(first)) == fingerprint(logical_rows(second))


def test_the_default_dataset_is_pinned(plan: DemoPlan) -> None:
    assert fingerprint(logical_rows(plan)) == DEFAULT_FINGERPRINT


def test_a_different_seed_builds_a_different_dataset(plan: DemoPlan) -> None:
    other = build_plan(SeedConfig(reference_date=REFERENCE, seed=seed_demo.DEFAULT_SEED + 1))

    assert fingerprint(logical_rows(other)) != fingerprint(logical_rows(plan))


def test_the_configuration_is_part_of_the_identity(plan: DemoPlan) -> None:
    shorter = build_plan(SeedConfig(reference_date=REFERENCE, history_days=200))

    assert fingerprint(logical_rows(shorter)) != fingerprint(logical_rows(plan))


def test_randomness_comes_only_from_random_random() -> None:
    """Only `random()` is guaranteed to reproduce across Python versions."""
    calls = set(re.findall(r"\brng\.(\w+)\(", SOURCE)) | set(re.findall(r"_rng\.(\w+)\(", SOURCE))
    assert calls == {"random"}
    for unstable in (
        "randrange",
        "randint",
        "choice(",
        "choices(",
        "shuffle(",
        "sample(",
        "uniform(",
        "gauss(",
    ):
        assert f".{unstable}" not in SOURCE, unstable


def test_nothing_reads_the_clock_or_the_environment_except_where_the_operator_asks() -> None:
    """`today` is read only for `--reference-date today`; the environment only for the
    owner's password. Neither DATABASE_URL nor TEST_DATABASE_URL can pick the target."""
    assert SOURCE.count("date.today()") == 1
    assert 'if value == "today":' in SOURCE
    for clock in ("datetime.now", "time.time", "utcnow", "gen_random_uuid()"):
        assert clock not in SOURCE.replace("``gen_random_uuid()``", ""), clock
    assert SOURCE.count("os.environ") == 1
    assert "read_password(os.environ)" in SOURCE
    tree = ast.parse(SOURCE)
    docstring = tree.body[0]
    code_strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and not (isinstance(docstring, ast.Expr) and node is docstring.value)
    ]
    assert not any("DATABASE_URL" in text for text in code_strings)
    assert "getenv" not in SOURCE


# --- dates relative to the reference date -------------------------------------------------------


def _relative(value: Any, reference: dt.date) -> Any:
    """Every date as an offset from the reference date, every instant as (offset, wall time)."""
    if isinstance(value, dt.datetime):
        return ((value.date() - reference).days, value.time())
    if isinstance(value, dt.date):
        return (value - reference).days
    if isinstance(value, SeedConfig):
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return tuple(
            _relative(getattr(value, field.name), reference) for field in dataclasses.fields(value)
        )
    if isinstance(value, tuple | list):
        return tuple(_relative(item, reference) for item in value)
    return value


@pytest.mark.parametrize("weeks", [1, 52, -30])
def test_moving_the_reference_date_by_whole_weeks_moves_everything_with_it(
    plan: DemoPlan, weeks: int
) -> None:
    """No date is hard-coded: the dataset is a function of offsets from the reference date and
    of the weekday. Shift by whole weeks and every record keeps its offset -- across month
    ends, year ends and daylight-saving changes alike."""
    shifted_reference = REFERENCE + dt.timedelta(weeks=weeks)
    shifted = build_plan(SeedConfig(reference_date=shifted_reference))

    assert _relative(shifted, shifted_reference) == _relative(plan, REFERENCE)


def test_moving_it_by_a_single_day_changes_the_weekday_pattern(plan: DemoPlan) -> None:
    next_day = REFERENCE + dt.timedelta(days=1)

    assert _relative(build_plan(SeedConfig(reference_date=next_day)), next_day) != _relative(
        plan, REFERENCE
    )


@pytest.mark.parametrize(
    "reference",
    [dt.date(2026, 9, 30), dt.date(2028, 2, 29), dt.date(2030, 12, 31), dt.date(2027, 3, 28)],
)
def test_stays_span_history_the_present_and_the_future_for_every_hotel(
    reference: dt.date,
) -> None:
    config = SeedConfig(reference_date=reference)
    plan = build_plan(config)
    earliest = reference - dt.timedelta(days=config.history_days)
    latest = reference + dt.timedelta(days=config.future_days)

    for spec in HOTELS:
        bookings = [b for b in plan.bookings if b.hotel == spec.slug]
        past = [b for b in bookings if b.check_out <= reference]
        current = [b for b in bookings if b.check_in < reference < b.check_out]
        future = [b for b in bookings if b.check_in >= reference]

        assert len(past) > 500, spec.slug
        assert current, spec.slug
        assert len(future) > 30, spec.slug
        assert all(earliest <= b.check_in <= latest for b in bookings)
        # The recent week the dashboard opens on is populated.
        recent = [
            b for b in bookings if reference - dt.timedelta(days=6) <= b.check_in <= reference
        ]
        assert len(recent) >= 5, spec.slug


def test_every_status_matches_where_the_stay_sits_relative_to_the_reference_date(
    plan: DemoPlan,
) -> None:
    for b in plan.bookings:
        if b.status in (BookingStatus.CHECKED_OUT.value, BookingStatus.NO_SHOW.value):
            assert b.check_out <= REFERENCE, b.reference
        elif b.status == BookingStatus.CHECKED_IN.value:
            assert b.check_in < REFERENCE < b.check_out, b.reference
        elif b.status in (BookingStatus.CONFIRMED.value, BookingStatus.PENDING.value):
            assert b.check_in >= REFERENCE, b.reference
        else:
            assert b.status == BookingStatus.CANCELLED.value


def test_every_status_occurs(plan: DemoPlan) -> None:
    assert {b.status for b in plan.bookings} == {status.value for status in BookingStatus}


def test_nothing_happens_after_the_as_of_moment(plan: DemoPlan) -> None:
    """Noon, hotel-local, on the reference date. No booking, cancellation, payment, review or
    reply is dated later -- the future holds stays, never events."""

    def instants(value: Any) -> list[dt.datetime]:
        if isinstance(value, dt.datetime):
            return [value]
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return [i for f in dataclasses.fields(value) for i in instants(getattr(value, f.name))]
        if isinstance(value, tuple):
            return [i for item in value for i in instants(item)]
        return []

    for hotel in plan.hotels:
        assert hotel.created_at < plan.as_of(hotel.spec)
    records: list[
        seed_demo.BookingRow | seed_demo.PaymentRow | seed_demo.ReviewRow | seed_demo.GuestRow
    ] = [*plan.bookings, *plan.payments, *plan.reviews, *plan.guests]
    for record in records:
        for instant in instants(record):
            assert instant <= as_of(plan, record.hotel), record
    assert all(row.revenue_date < REFERENCE for row in plan.revenue)
    assert all(row.expense_date < REFERENCE for row in plan.expenses)


# --- booking creation: lead times ---------------------------------------------------------------


def test_bookings_are_taken_before_they_are_stayed_by_realistic_lead_times(
    plan: DemoPlan,
) -> None:
    leads = [b.lead_days for b in plan.bookings]

    assert min(leads) == 0  # walk-ins exist
    assert max(leads) <= 180
    assert all(b.booked_at.date() <= b.check_in for b in plan.bookings)
    share_over_a_month = sum(1 for lead in leads if lead > 30) / len(leads)
    share_last_week = sum(1 for lead in leads if lead <= 7) / len(leads)
    assert 0.25 < share_over_a_month < 0.6
    assert 0.15 < share_last_week < 0.45
    walk_ins = [b for b in plan.bookings if b.lead_days == 0]
    assert walk_ins
    assert all(b.source == BookingSource.WALK_IN.value for b in walk_ins)


def test_booking_creation_is_spread_across_days_not_concentrated_on_one(
    plan: DemoPlan,
) -> None:
    """The old demo database took all 166 of its bookings on one day. Here, every hotel takes
    bookings on most days of the last ninety, and no day holds more than a few percent."""
    for spec in HOTELS:
        bookings = [b for b in plan.bookings if b.hotel == spec.slug]
        per_day = collections.Counter(b.booked_at.date() for b in bookings)
        window = {REFERENCE - dt.timedelta(days=n) for n in range(90)}

        assert len(window & set(per_day)) >= 70, spec.slug
        assert max(per_day.values()) / len(bookings) < 0.02, spec.slug


def test_created_at_is_the_booking_instant(plan: DemoPlan) -> None:
    """`created_at` is written equal to `booked_at`: a booking entered when it was taken."""
    rows = logical_rows(plan)["bookings"]
    booked_at, created_at = 13, 16
    assert all(row[created_at] == row[booked_at] for row in rows)


def test_the_future_is_on_the_books_the_way_a_real_one_is(plan: DemoPlan) -> None:
    """Fuller near the reference date than months out, because later stays have had less time
    to be booked -- not because anything was tuned to look that way."""
    for spec in HOTELS:
        nights = collections.Counter(
            n.stay_date
            for b in plan.bookings
            if b.hotel == spec.slug and b.status in INVENTORY_HOLDING_STATUSES
            for n in b.nights
        )
        soon = sum(nights[REFERENCE + dt.timedelta(days=d)] for d in range(0, 14))
        later = sum(nights[REFERENCE + dt.timedelta(days=d)] for d in range(90, 104))
        assert soon > later, spec.slug


# --- the schema's constraints, mirrored ---------------------------------------------------------


def test_no_room_is_held_twice_on_any_night(plan: DemoPlan) -> None:
    """The GiST exclusion constraint covers holding statuses; the plan never overlaps a room at
    all, whatever the status."""
    for (_hotel, _room), stays in _by_room(plan).items():
        ordered = sorted(stays, key=lambda b: b.check_in)
        for earlier, later in itertools.pairwise(ordered):
            assert earlier.check_out <= later.check_in, (earlier.reference, later.reference)


def _by_room(plan: DemoPlan) -> dict[tuple[str, str], list[seed_demo.BookingRow]]:
    rooms: dict[tuple[str, str], list[seed_demo.BookingRow]] = collections.defaultdict(list)
    for b in plan.bookings:
        rooms[(b.hotel, b.room)].append(b)
    return rooms


def test_every_booking_is_internally_consistent(plan: DemoPlan) -> None:
    capacity = {(spec.slug, rt.code): rt.max_occupancy for spec in HOTELS for rt in spec.room_types}
    room_type = {(r.hotel, r.number): r.room_type for r in plan.rooms}
    references: set[tuple[str, str]] = set()
    for b in plan.bookings:
        assert b.check_out > b.check_in
        # Deferred trigger: one night row per night, each inside the stay.
        assert [n.stay_date for n in b.nights] == [
            b.check_in + dt.timedelta(days=i) for i in range((b.check_out - b.check_in).days)
        ]
        assert all(n.rate > 0 and n.rate == n.rate.quantize(seed_demo.CENT) for n in b.nights)
        assert b.total_amount == sum(n.rate for n in b.nights)
        assert (b.status == BookingStatus.CANCELLED.value) == (b.cancelled_at is not None)
        assert (b.cancelled_at is None) == (b.cancellation_reason is None)
        if b.cancelled_at is not None:
            assert b.booked_at < b.cancelled_at
        assert b.updated_at >= b.booked_at
        assert b.adults >= 1 and b.children >= 0
        assert b.adults + b.children <= capacity[(b.hotel, room_type[(b.hotel, b.room)])]
        assert b.source in {s.value for s in BookingSource}
        assert (b.reference, b.hotel) not in references
        references.add((b.reference, b.hotel))


def test_guests_are_synthetic_and_unique_per_hotel(plan: DemoPlan) -> None:
    emails = [(g.hotel, g.email) for g in plan.guests if g.email is not None]
    assert len(emails) == len(set(emails))
    assert all(re.fullmatch(r"[a-z.]+\.\d+@example\.com", email) for _, email in emails)
    assert all(re.fullmatch(r"[A-Z]{2}", g.country_code) for g in plan.guests)
    guests = {g.public_id: g for g in plan.guests}
    for b in plan.bookings:
        assert guests[b.guest].hotel == b.hotel
        assert guests[b.guest].created_at <= b.booked_at


def test_payments_satisfy_the_payment_constraints(plan: DemoPlan) -> None:
    bookings = {b.public_id: b for b in plan.bookings}
    for p in plan.payments:
        assert p.amount > 0
        assert p.method in {m.value for m in PaymentMethod}
        assert p.paid_at <= as_of(plan, p.hotel)
        assert bookings[p.booking].hotel == p.hotel
    assert len({p.public_id for p in plan.payments}) == len(plan.payments)


def test_reviews_satisfy_the_review_constraints(plan: DemoPlan) -> None:
    bookings = {b.public_id: b for b in plan.bookings}
    assert len({r.booking for r in plan.reviews}) == len(plan.reviews)  # one per stay
    external = [(r.source, r.external_review_id) for r in plan.reviews if r.external_review_id]
    assert len(external) == len(set(external))
    for r in plan.reviews:
        assert r.rating_scale in (5, 10)
        assert 0 <= r.rating <= r.rating_scale
        assert r.source in {s.value for s in ReviewSource}
        assert bookings[r.booking].status == BookingStatus.CHECKED_OUT.value
        assert bookings[r.booking].check_out < r.review_date <= REFERENCE


def test_ledger_rows_are_non_room_and_on_closed_days(plan: DemoPlan) -> None:
    assert {r.category for r in plan.revenue} <= {c for c, _ in seed_demo.REVENUE_CATEGORIES}
    assert len({r.reference for r in plan.revenue}) == len(plan.revenue)
    assert len({e.invoice_reference for e in plan.expenses}) == len(plan.expenses)
    assert all(r.tax_amount >= 0 and r.revenue_date < REFERENCE for r in plan.revenue)
    assert all(e.tax_amount >= 0 and e.expense_date < REFERENCE for e in plan.expenses)
    assert {e.category for e in plan.expenses} == {c for c, _, _ in seed_demo.EXPENSE_CATEGORIES}


# --- the analytics it exists to feed ------------------------------------------------------------


def _daily(counts: collections.Counter[dt.date], start: dt.date, days: int) -> list[Observation]:
    return [
        Observation(
            date=start + dt.timedelta(days=n),
            value=decimal.Decimal(counts[start + dt.timedelta(days=n)]),
        )
        for n in range(days)
    ]


def test_the_default_ninety_day_window_is_genuinely_assessable(plan: DemoPlan) -> None:
    """Enough activity and variation for the intelligence page's default 90-day window: the
    anomaly scan can judge both series, and the demand trend is a real direction rather than
    no_activity, sparse_activity or insufficient_data. Which direction is not asserted -- the
    data has no manufactured trend."""
    start = REFERENCE - dt.timedelta(days=89)
    for spec in HOTELS:
        mine = [b for b in plan.bookings if b.hotel == spec.slug]
        created = collections.Counter(b.booked_at.date() for b in mine)
        occupied = collections.Counter(
            n.stay_date
            for b in mine
            if b.status
            in (
                BookingStatus.CHECKED_OUT.value,
                BookingStatus.CHECKED_IN.value,
                BookingStatus.CONFIRMED.value,
            )
            for n in b.nights
        )

        assert anomaly_assessability(_daily(created, start, 90)) is None, spec.slug
        assert anomaly_assessability(_daily(occupied, start, 90)) is None, spec.slug
        assert measure_trend(_daily(created, start, 90)).direction in {
            "increasing",
            "decreasing",
            "stable",
        }, spec.slug


# --- the operator surface -----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "short", "x" * 11, "x" * 257])
def test_an_unusable_password_is_refused(value: str) -> None:
    with pytest.raises(SeedRefusedError) as refused:
        seed_demo.read_password({seed_demo.PASSWORD_VARIABLE: value})
    assert value not in str(refused.value) or value == ""


def test_a_usable_password_is_returned_unchanged() -> None:
    assert seed_demo.read_password({seed_demo.PASSWORD_VARIABLE: "a-long-enough-pw"}) == (
        "a-long-enough-pw"
    )


def test_the_reference_date_is_an_iso_date_or_the_word_today() -> None:
    assert seed_demo.parse_reference_date("2026-09-30") == REFERENCE
    assert seed_demo.parse_reference_date("today") == dt.date.today()
    with pytest.raises(Exception, match="ISO date"):
        seed_demo.parse_reference_date("30/09/2026")


def test_the_reference_date_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        seed_demo.main(["--dry-run"])
    assert "--reference-date" in capsys.readouterr().err


def test_a_target_is_required_and_never_taken_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/should_not_be_used")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@localhost/nor_this_test")
    with pytest.raises(SystemExit):
        seed_demo.main(["--reference-date", "2026-09-30"])
    assert "--database-url" in capsys.readouterr().err


def test_a_dry_run_prints_the_plan_and_touches_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert seed_demo.main(["--reference-date", "2026-09-30", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert f"plan fingerprint     : {DEFAULT_FINGERPRINT}" in out
    assert "reference date : 2026-09-30" in out


def test_an_implausible_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="history_days"):
        SeedConfig(reference_date=REFERENCE, history_days=3)
    with pytest.raises(ValueError, match="future_days"):
        SeedConfig(reference_date=REFERENCE, future_days=-1)
