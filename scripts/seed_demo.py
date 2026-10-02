"""Deterministic, synthetic demo data for a fresh, fully migrated database.

    python scripts/seed_demo.py --database-url URL --reference-date 2026-09-30
    python scripts/seed_demo.py --database-url URL --reference-date today --seed 7151
    python scripts/seed_demo.py --reference-date 2026-09-30 --dry-run

## What it is for

A database to *look at*: the dashboard, the intelligence page and the copilot need recent,
current and future bookings to show anything. Before this script the only demo database had been
assembled by one-off scripts that were never committed; its bookings were all taken on a single
day and its stays sat in a narrow, now historical, window.

## What it is not for

**Nothing here is evidence.** The data is invented. It must never be used to train or evaluate a
model, to measure retrieval, or to support any claim about a real hotel -- the offline demand
model is trained on a public dataset (``ml/manifests``), and Stage 7.15's evidence must be real
documents. The hotels are fictional, every e-mail address is under ``example.com`` (RFC 2606),
and no phone number, card fragment or date of birth is generated.

## Determinism

The same ``--seed``, ``--reference-date`` and configuration produce the same logical dataset,
byte for byte, on any machine and any Python version:

* Randomness comes only from :meth:`random.Random.random`, seeded with an integer. Python
  guarantees that sequence across versions; ``randrange``, ``choice`` and ``shuffle`` carry no
  such guarantee, so they are not used. Integers, choices and money are derived from
  ``random()`` here, and money is exact :class:`~decimal.Decimal` built from whole cents.
* Each purpose (stays, guests, ledger, reviews) draws from its own stream, derived from the seed
  by SHA-256, so a change to one part cannot silently reshuffle another.
* Nothing reads the wall clock, the locale or the environment -- except ``--reference-date
  today``, which is the operator explicitly choosing the machine's date. Public identifiers
  are UUIDv5 values derived from the seed, not ``gen_random_uuid()``.
* Every date is an offset from the reference date, and the only calendar effect is the day of
  the week. So moving the reference date by a whole number of weeks moves the entire dataset
  by exactly that much, which the tests check. There is deliberately **no seasonality and no
  trend**: a calendar season would make the data's shape depend on the day it was seeded, and
  a trend would be manufactured. Demand varies by weekday and by chance, nothing else. No
  anomaly is injected.

Two things are outside the fingerprint, by necessity rather than oversight: surrogate keys (the
schema's identity columns refuse supplied values) and the demo owner's credentials. The password
hash carries a fresh salt, and the account's timestamps are the real ``now()`` -- a
``password_changed_at`` in the future would revoke every token issued before it.

## The reference date

The data describes the properties **as of noon, hotel-local time, on the reference date**. Stays
that ended by then are completed (or cancelled / no-show); stays that began before it are in
house; stays from the reference date on are future bookings, and only those whose booking
instant is not later than noon exist at all. A booking's ``booked_at`` is its check-in minus a
lead time drawn from a realistic mix (walk-ins, last-minute, weeks and months ahead), so the
future is on the books the way a real one is: fuller near the reference date, thinner further
out. ``created_at`` is set equal to ``booked_at``, as for a booking entered when it was taken.

## The declared observation period

Each hotel gets one ``demand_observation_periods`` row: the span whose complete booking record
the seed has written, which is what lets the demand model and the forecasts read a quiet day as
a zero. It is **not** the whole window. It starts ``longest stay - 1`` days after the window
opens, because a room's walk begins up to a week earlier and a stay begun before the window is
not written -- its nights inside the window are missing, so those first days are incomplete --
and it ends the day before the reference date, whose own night is still being booked at noon.

## Safety

* ``--database-url`` is required and is never read from the environment, so no ambient
  ``DATABASE_URL`` or ``TEST_DATABASE_URL`` can choose the target.
* The target must already be at the Alembic head. This script never migrates.
* The target must hold **no row in any application table**. A database with data -- a
  production database, the old demo database -- is refused before anything is written, with
  the tables that hold rows named. Nothing is ever deleted, truncated or overwritten; to reseed,
  create a new database (see ``database/README.md``).
* Everything is written in one transaction. The rows are read back and compared with the plan
  before the commit; any difference, or any constraint the database rejects, rolls the whole
  seed back and leaves the database empty.

The demo owner's password is read from ``DEMO_OWNER_PASSWORD`` and is never printed.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import decimal
import hashlib
import json
import os
import random
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.models import (  # noqa: E402
    Amenity,
    Booking,
    BookingRoom,
    BookingRoomNight,
    DemandObservationPeriod,
    Expense,
    ExpenseCategory,
    Guest,
    Hotel,
    Payment,
    Revenue,
    RevenueCategory,
    Review,
    Room,
    RoomType,
    RoomTypeAmenity,
    User,
    UserHotel,
)
from app.models.enums import (  # noqa: E402
    BookingSource,
    BookingStatus,
    HotelRole,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    ReviewSource,
    RoomStatus,
)

#: Documented default. Any integer works; this one is only a name for "the usual demo".
DEFAULT_SEED = 7151
DEFAULT_HISTORY_DAYS = 365
DEFAULT_FUTURE_DAYS = 120
#: The dataset describes each hotel as of this hour, hotel-local, on the reference date.
AS_OF_HOUR = 12
DEFAULT_OWNER_EMAIL = "demo.owner@example.com"
PASSWORD_VARIABLE = "DEMO_OWNER_PASSWORD"
#: `app.schemas.auth.PasswordField`: the rule registration applies, applied here too.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 256

#: Fixed namespace for the demo's UUIDv5 public identifiers.
DEMO_NAMESPACE = uuid.UUID("6f1d3c2a-8b4e-5a70-9c1d-2e3f4a5b6c7d")
CENT = decimal.Decimal("0.01")
CURRENCY = "EUR"
UTC = dt.UTC


# --- configuration -------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SeedConfig:
    """Everything that decides the dataset. Nothing else does."""

    reference_date: dt.date
    seed: int = DEFAULT_SEED
    history_days: int = DEFAULT_HISTORY_DAYS
    future_days: int = DEFAULT_FUTURE_DAYS
    owner_email: str = DEFAULT_OWNER_EMAIL

    def __post_init__(self) -> None:
        if not 28 <= self.history_days <= 1000:
            raise ValueError("history_days must be between 28 and 1000")
        if not 0 <= self.future_days <= 365:
            raise ValueError("future_days must be between 0 and 365")


@dataclasses.dataclass(frozen=True)
class RoomTypeSpec:
    code: str
    name: str
    rooms: int
    base_price: decimal.Decimal
    standard_occupancy: int
    max_occupancy: int
    bed_count: int
    bed_configuration: str
    size_sqm: decimal.Decimal
    amenities: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class HotelSpec:
    code: str
    name: str
    slug: str
    address_line1: str
    city: str
    postal_code: str
    timezone: str
    star_rating: int
    #: Probability that an empty room starts a stay on an average day.
    start_probability: decimal.Decimal
    room_types: tuple[RoomTypeSpec, ...]
    payroll: decimal.Decimal
    utilities: decimal.Decimal


HOTELS: tuple[HotelSpec, ...] = (
    HotelSpec(
        code="HRB",
        name="Demo Harbour Hotel",
        slug="demo-harbour-hotel",
        address_line1="1 Example Quay",
        city="Piraeus",
        postal_code="18531",
        timezone="Europe/Athens",
        star_rating=4,
        start_probability=decimal.Decimal("0.50"),
        room_types=(
            RoomTypeSpec(
                "DBL",
                "Standard Double",
                8,
                decimal.Decimal("95.00"),
                2,
                3,
                1,
                "1 double",
                decimal.Decimal("18.00"),
                ("wifi", "air_conditioning"),
            ),
            RoomTypeSpec(
                "SEA",
                "Superior Sea View",
                5,
                decimal.Decimal("140.00"),
                2,
                3,
                1,
                "1 king",
                decimal.Decimal("24.00"),
                ("wifi", "air_conditioning", "sea_view", "balcony", "minibar"),
            ),
            RoomTypeSpec(
                "STE",
                "Harbour Suite",
                2,
                decimal.Decimal("240.00"),
                2,
                4,
                2,
                "1 king, 1 sofa bed",
                decimal.Decimal("42.00"),
                ("wifi", "air_conditioning", "sea_view", "balcony", "minibar", "bathtub"),
            ),
        ),
        payroll=decimal.Decimal("9800.00"),
        utilities=decimal.Decimal("1850.00"),
    ),
    HotelSpec(
        code="HLS",
        name="Demo Hillside Inn",
        slug="demo-hillside-inn",
        address_line1="12 Example Hill Road",
        city="Nafplio",
        postal_code="21100",
        timezone="Europe/Athens",
        star_rating=3,
        start_probability=decimal.Decimal("0.38"),
        room_types=(
            RoomTypeSpec(
                "STD",
                "Standard Room",
                7,
                decimal.Decimal("80.00"),
                2,
                2,
                1,
                "1 double",
                decimal.Decimal("16.00"),
                ("wifi", "air_conditioning"),
            ),
            RoomTypeSpec(
                "FAM",
                "Family Room",
                3,
                decimal.Decimal("130.00"),
                4,
                5,
                3,
                "1 double, 2 singles",
                decimal.Decimal("30.00"),
                ("wifi", "air_conditioning", "kitchenette"),
            ),
        ),
        payroll=decimal.Decimal("5200.00"),
        utilities=decimal.Decimal("980.00"),
    ),
)

AMENITIES: tuple[tuple[str, str, str], ...] = (
    ("air_conditioning", "Air conditioning", "comfort"),
    ("balcony", "Balcony", "view"),
    ("bathtub", "Bathtub", "bathroom"),
    ("kitchenette", "Kitchenette", "comfort"),
    ("minibar", "Minibar", "comfort"),
    ("sea_view", "Sea view", "view"),
    ("wifi", "Wi-Fi", "connectivity"),
)
REVENUE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("FNB", "Food and beverage"),
    ("PARKING", "Parking"),
    ("SPA", "Spa"),
)
#: (code, name, is_fixed_cost)
EXPENSE_CATEGORIES: tuple[tuple[str, str, bool], ...] = (
    ("LAUNDRY", "Laundry", False),
    ("MAINTENANCE", "Maintenance", False),
    ("PAYROLL", "Payroll", True),
    ("SUPPLIES", "Supplies", False),
    ("UTILITIES", "Utilities", True),
)

#: Monday .. Sunday. Relative chance of a stay STARTING on that weekday.
ARRIVAL_WEEKDAY_PERCENT = (85, 85, 95, 105, 135, 130, 80)
#: Monday .. Sunday. Nightly rate relative to the rack rate.
NIGHT_WEEKDAY_PERCENT = (100, 100, 100, 100, 115, 115, 95)
LENGTH_OF_STAY: tuple[tuple[int, int], ...] = ((1, 22), (2, 30), (3, 20), (4, 12), (5, 8), (7, 8))
#: (smallest lead, largest lead, weight), in days before check-in.
LEAD_BANDS: tuple[tuple[int, int, int], ...] = (
    (0, 0, 5),
    (1, 7, 20),
    (8, 30, 30),
    (31, 90, 32),
    (91, 180, 13),
)
SOURCES: tuple[tuple[str, int], ...] = (
    (BookingSource.DIRECT.value, 15),
    (BookingSource.WEBSITE.value, 22),
    (BookingSource.PHONE.value, 8),
    (BookingSource.BOOKING_COM.value, 30),
    (BookingSource.EXPEDIA.value, 15),
    (BookingSource.AGODA.value, 5),
    (BookingSource.OTHER.value, 5),
)
OTA_PREFIX = {
    BookingSource.BOOKING_COM.value: "BDC",
    BookingSource.EXPEDIA.value: "EXP",
    BookingSource.AGODA.value: "AGD",
}
CANCELLATION_REASONS = (
    "Change of travel plans",
    "Booked elsewhere",
    "Trip postponed",
    "Illness",
)
FIRST_NAMES = (
    "Alex",
    "Anna",
    "Camille",
    "Daan",
    "Eleni",
    "Emma",
    "Giulia",
    "James",
    "Katerina",
    "Laura",
    "Liam",
    "Lukas",
    "Marco",
    "Maria",
    "Nikos",
    "Olivia",
    "Pierre",
    "Sophie",
    "Thomas",
    "Yannis",
)
LAST_NAMES = (
    "Bakker",
    "Bianchi",
    "Brown",
    "de Vries",
    "Dimitriou",
    "Dubois",
    "Ferrari",
    "Georgiou",
    "Jansen",
    "Johnson",
    "Laurent",
    "Martin",
    "Nikolaou",
    "Papadopoulos",
    "Rossi",
    "Schmidt",
    "Smith",
    "Walker",
    "Weber",
    "Wright",
)
#: (country, preferred language, weight)
COUNTRIES: tuple[tuple[str, str, int], ...] = (
    ("GR", "el", 30),
    ("GB", "en", 15),
    ("DE", "de", 14),
    ("FR", "fr", 10),
    ("IT", "it", 10),
    ("NL", "nl", 8),
    ("US", "en", 8),
    ("CY", "el", 5),
)
REVIEW_TEXT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "high": (
        ("Lovely stay", "Excellent location", "Would come back"),
        (
            "Friendly staff and a spotless room.",
            "Great breakfast and a comfortable bed.",
            "Easy check-in and a quiet room.",
        ),
    ),
    "mid": (
        ("Good value", "Pleasant enough"),
        (
            "Comfortable, though the room was a little small.",
            "A nice stay; breakfast could offer more choice.",
        ),
    ),
    "low": (
        ("Disappointing", "Not as expected"),
        ("The room was noisy at night.", "Check-in took much longer than it should have."),
    ),
}


# --- the plan: plain, immutable records ----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HotelRow:
    spec: HotelSpec
    public_id: uuid.UUID
    created_at: dt.datetime


@dataclasses.dataclass(frozen=True)
class RoomRow:
    hotel: str  # slug
    room_type: str  # code
    number: str
    floor: int
    status: str


@dataclasses.dataclass(frozen=True)
class GuestRow:
    hotel: str
    public_id: uuid.UUID
    first_name: str
    last_name: str
    email: str | None
    country_code: str
    preferred_language: str
    marketing_opt_in: bool
    created_at: dt.datetime


@dataclasses.dataclass(frozen=True)
class NightRow:
    stay_date: dt.date
    rate: decimal.Decimal
    rate_plan_code: str


@dataclasses.dataclass(frozen=True)
class BookingRow:
    hotel: str
    public_id: uuid.UUID
    reference: str
    guest: uuid.UUID
    room: str
    check_in: dt.date
    check_out: dt.date
    status: str
    adults: int
    children: int
    source: str
    channel_reference: str | None
    total_amount: decimal.Decimal
    booked_at: dt.datetime
    cancelled_at: dt.datetime | None
    cancellation_reason: str | None
    updated_at: dt.datetime
    guest_name: str
    nights: tuple[NightRow, ...]

    @property
    def lead_days(self) -> int:
        return (self.check_in - self.booked_at.date()).days


@dataclasses.dataclass(frozen=True)
class PaymentRow:
    public_id: uuid.UUID
    hotel: str
    booking: uuid.UUID
    amount: decimal.Decimal
    method: str
    paid_at: dt.datetime


@dataclasses.dataclass(frozen=True)
class ReviewRow:
    hotel: str
    booking: uuid.UUID
    guest: uuid.UUID
    source: str
    external_review_id: str | None
    rating: decimal.Decimal
    rating_scale: int
    title: str | None
    body: str | None
    reviewer_name: str
    review_date: dt.date
    responded_at: dt.datetime | None


@dataclasses.dataclass(frozen=True)
class RevenueRow:
    hotel: str
    category: str
    booking: uuid.UUID | None
    revenue_date: dt.date
    amount: decimal.Decimal
    tax_amount: decimal.Decimal
    description: str
    reference: str


@dataclasses.dataclass(frozen=True)
class ExpenseRow:
    hotel: str
    category: str
    expense_date: dt.date
    amount: decimal.Decimal
    tax_amount: decimal.Decimal
    description: str
    vendor: str
    invoice_reference: str


@dataclasses.dataclass(frozen=True)
class DemoPlan:
    config: SeedConfig
    hotels: tuple[HotelRow, ...]
    rooms: tuple[RoomRow, ...]
    guests: tuple[GuestRow, ...]
    bookings: tuple[BookingRow, ...]
    payments: tuple[PaymentRow, ...]
    reviews: tuple[ReviewRow, ...]
    revenue: tuple[RevenueRow, ...]
    expenses: tuple[ExpenseRow, ...]

    def as_of(self, hotel: HotelSpec) -> dt.datetime:
        return as_of_instant(self.config.reference_date, hotel)


# --- deterministic primitives --------------------------------------------------------------------


def stream(seed: int, purpose: str) -> random.Random:
    """An independent generator for one purpose, derived from the seed by SHA-256."""
    digest = hashlib.sha256(f"ahip-demo/{seed}/{purpose}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def public_id(seed: int, kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(DEMO_NAMESPACE, f"{seed}/{kind}/{key}")


def between(rng: random.Random, low: int, high: int) -> int:
    """An integer in [low, high], from ``random()`` alone."""
    return min(high, low + int(rng.random() * (high - low + 1)))


def chance(rng: random.Random, percent: int) -> bool:
    return rng.random() * 100 < percent


def pick[T](rng: random.Random, options: Sequence[T]) -> T:
    return options[between(rng, 0, len(options) - 1)]


def weighted[T](rng: random.Random, options: Sequence[tuple[T, int]]) -> T:
    total = sum(weight for _, weight in options)
    point = rng.random() * total
    for value, weight in options:
        point -= weight
        if point < 0:
            return value
    return options[-1][0]


def cents(rng: random.Random, low: decimal.Decimal, high: decimal.Decimal) -> decimal.Decimal:
    """Money in [low, high], whole cents."""
    value = between(rng, int(low * 100), int(high * 100))
    return (decimal.Decimal(value) / 100).quantize(CENT)


def percent_of(amount: decimal.Decimal, *percents: int) -> decimal.Decimal:
    value = amount
    for percent in percents:
        value = value * percent / 100
    return value.quantize(CENT, rounding=decimal.ROUND_HALF_UP)


def local(day: dt.date, hour: int, minute: int, zone: ZoneInfo) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)


def as_of_instant(reference: dt.date, hotel: HotelSpec) -> dt.datetime:
    return local(reference, AS_OF_HOUR, 0, ZoneInfo(hotel.timezone))


def moment_between(rng: random.Random, start: dt.datetime, end: dt.datetime) -> dt.datetime | None:
    """A whole-minute instant in (start, end], or None when the interval is empty."""
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 1:
        return None
    return start + dt.timedelta(minutes=between(rng, 1, minutes))


# --- building the plan ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Stay:
    hotel: HotelSpec
    room_type: RoomTypeSpec
    room: str
    check_in: dt.date
    check_out: dt.date
    lead: int
    booked_at: dt.datetime


def observation_period(config: SeedConfig) -> tuple[dt.date, dt.date]:
    """The span, both ends inclusive, whose complete booking record the seed writes.

    From ``longest stay - 1`` days into the window -- a stay begun before the window is not
    written, and the longest one reaches that far in -- to the day before the reference date,
    the last night that is over by the as-of moment.
    """
    start = config.reference_date - dt.timedelta(days=config.history_days)
    longest = max(nights for nights, _weight in LENGTH_OF_STAY)
    return start + dt.timedelta(days=longest - 1), config.reference_date - dt.timedelta(days=1)


def _simulate_stays(config: SeedConfig) -> list[_Stay]:
    """Walk every room forward through the window. A room either starts a stay or stays empty
    for a night; stays in one room therefore never overlap, whatever their status."""
    rng = stream(config.seed, "stays")
    start = config.reference_date - dt.timedelta(days=config.history_days)
    end = config.reference_date + dt.timedelta(days=config.future_days)
    stays: list[_Stay] = []
    for hotel in HOTELS:
        zone = ZoneInfo(hotel.timezone)
        as_of = as_of_instant(config.reference_date, hotel)
        for room_type, number, _floor in _rooms_of(hotel):
            day = start - dt.timedelta(days=between(rng, 0, 6))
            while day <= end:
                probability = hotel.start_probability * ARRIVAL_WEEKDAY_PERCENT[day.weekday()] / 100
                if rng.random() >= probability:
                    day += dt.timedelta(days=1)
                    continue
                nights = weighted(rng, LENGTH_OF_STAY)
                low, high = weighted(rng, [((lo, hi), w) for lo, hi, w in LEAD_BANDS])
                lead = between(rng, low, high)
                hour = between(rng, 13, 20) if lead == 0 else between(rng, 8, 22)
                booked_at = local(day - dt.timedelta(days=lead), hour, between(rng, 0, 59), zone)
                # A booking whose instant is after the as-of moment has not been made yet. Its
                # nights stay empty: that is what a real future looks like from today.
                if day >= start and booked_at <= as_of:
                    stays.append(
                        _Stay(
                            hotel,
                            room_type,
                            number,
                            day,
                            day + dt.timedelta(days=nights),
                            lead,
                            booked_at,
                        )
                    )
                day += dt.timedelta(days=nights)
    return stays


def _rooms_of(hotel: HotelSpec) -> Iterator[tuple[RoomTypeSpec, str, int]]:
    floor_of = {room_type.code: floor for floor, room_type in enumerate(hotel.room_types, start=1)}
    for room_type in hotel.room_types:
        floor = floor_of[room_type.code]
        for index in range(1, room_type.rooms + 1):
            yield room_type, f"{floor}{index:02d}", floor


def _status(rng: random.Random, stay: _Stay, reference: dt.date) -> str:
    if stay.check_out <= reference:
        roll = rng.random() * 100
        if roll < 7:
            return BookingStatus.CANCELLED.value
        if roll < 9:
            return BookingStatus.NO_SHOW.value
        return BookingStatus.CHECKED_OUT.value
    if stay.check_in < reference:
        return BookingStatus.CHECKED_IN.value
    roll = rng.random() * 100
    if roll < 7:
        return BookingStatus.CANCELLED.value
    if roll < 12:
        return BookingStatus.PENDING.value
    return BookingStatus.CONFIRMED.value


def build_plan(config: SeedConfig) -> DemoPlan:
    """The whole dataset, computed in memory. Pure: no database, no clock, no environment."""
    reference = config.reference_date
    seed = config.seed
    hotels = tuple(
        HotelRow(
            spec=spec,
            public_id=public_id(seed, "hotel", spec.slug),
            created_at=as_of_instant(reference, spec)
            - dt.timedelta(days=config.history_days + 240),
        )
        for spec in HOTELS
    )

    stays = sorted(
        _simulate_stays(config),
        key=lambda stay: (stay.hotel.slug, stay.booked_at, stay.room, stay.check_in),
    )

    booking_rng = stream(seed, "bookings")
    guest_rng = stream(seed, "guests")
    guests: list[GuestRow] = []
    pool: dict[str, list[GuestRow]] = {spec.slug: [] for spec in HOTELS}
    bookings: list[BookingRow] = []
    counters: dict[str, int] = {spec.slug: 0 for spec in HOTELS}

    for stay in stays:
        hotel = stay.hotel
        zone = ZoneInfo(hotel.timezone)
        as_of = as_of_instant(reference, hotel)
        counters[hotel.slug] += 1
        number = counters[hotel.slug]
        booking_id = public_id(seed, "booking", f"{hotel.slug}/{number}")

        # Guest: a returning one sometimes, otherwise someone new, created when they booked.
        returning = pool[hotel.slug] and chance(guest_rng, 12)
        if returning:
            guest = pick(guest_rng, pool[hotel.slug])
        else:
            first, last = pick(guest_rng, FIRST_NAMES), pick(guest_rng, LAST_NAMES)
            country, language = weighted(
                guest_rng, [((code, lang), w) for code, lang, w in COUNTRIES]
            )
            serial = len(pool[hotel.slug]) + 1
            local_part = f"{first}.{last}".lower().replace(" ", "")
            guest = GuestRow(
                hotel=hotel.slug,
                public_id=public_id(seed, "guest", f"{hotel.slug}/{serial}"),
                first_name=first,
                last_name=last,
                email=f"{local_part}.{serial}@example.com" if chance(guest_rng, 85) else None,
                country_code=country,
                preferred_language=language,
                marketing_opt_in=chance(guest_rng, 30),
                created_at=stay.booked_at,
            )
            pool[hotel.slug].append(guest)
            guests.append(guest)

        status = _status(booking_rng, stay, reference)
        cancelled_at: dt.datetime | None = None
        reason: str | None = None
        if status == BookingStatus.CANCELLED.value:
            limit = min(local(stay.check_in, 0, 0, zone), as_of)
            cancelled_at = moment_between(booking_rng, stay.booked_at, limit)
            if cancelled_at is None:
                # Booked on the day (a walk-in): too late to cancel. It happened instead.
                status = (
                    BookingStatus.CHECKED_OUT.value
                    if stay.check_out <= reference
                    else BookingStatus.CONFIRMED.value
                )
            else:
                reason = pick(booking_rng, CANCELLATION_REASONS)

        source = BookingSource.WALK_IN.value if stay.lead == 0 else weighted(booking_rng, SOURCES)
        prefix = OTA_PREFIX.get(source)
        channel = f"{prefix}-{between(booking_rng, 1_000_000, 9_999_999)}" if prefix else None
        nonrefundable = stay.lead >= 30 and prefix is None and chance(booking_rng, 35)
        plan_code = "OTA" if prefix else ("NONREF" if nonrefundable else "BAR")
        lead_percent = 105 if stay.lead <= 3 else (92 if stay.lead >= 60 else 100)
        plan_percent = 90 if nonrefundable else 100
        nights = tuple(
            NightRow(
                stay_date=night,
                rate=percent_of(
                    stay.room_type.base_price,
                    NIGHT_WEEKDAY_PERCENT[night.weekday()],
                    lead_percent,
                    plan_percent,
                    between(booking_rng, 95, 105),
                ),
                rate_plan_code=plan_code,
            )
            for night in (
                stay.check_in + dt.timedelta(days=offset)
                for offset in range((stay.check_out - stay.check_in).days)
            )
        )
        adults = min(
            stay.room_type.max_occupancy,
            weighted(booking_rng, ((1, 25), (2, 65), (3, 10))),
        )
        children = (
            between(booking_rng, 0, stay.room_type.max_occupancy - adults)
            if stay.room_type.max_occupancy - adults > 0 and chance(booking_rng, 25)
            else 0
        )
        updated_at = {
            BookingStatus.CHECKED_OUT.value: local(stay.check_out, 10, 30, zone),
            BookingStatus.CHECKED_IN.value: local(stay.check_in, 16, 0, zone),
            BookingStatus.NO_SHOW.value: local(stay.check_in + dt.timedelta(days=1), 10, 0, zone),
        }.get(status, cancelled_at or stay.booked_at)

        bookings.append(
            BookingRow(
                hotel=hotel.slug,
                public_id=booking_id,
                reference=f"DMO-{hotel.code}-{number:05d}",
                guest=guest.public_id,
                room=stay.room,
                check_in=stay.check_in,
                check_out=stay.check_out,
                status=status,
                adults=adults,
                children=children,
                source=source,
                channel_reference=channel,
                total_amount=sum((night.rate for night in nights), decimal.Decimal("0.00")),
                booked_at=stay.booked_at,
                cancelled_at=cancelled_at,
                cancellation_reason=reason,
                updated_at=updated_at,
                guest_name=f"{guest.first_name} {guest.last_name}",
                nights=nights,
            )
        )

    in_house = {(b.hotel, b.room) for b in bookings if b.status == BookingStatus.CHECKED_IN.value}
    rooms = tuple(
        RoomRow(
            hotel=spec.slug,
            room_type=room_type.code,
            number=number,
            floor=floor,
            status=(
                RoomStatus.OCCUPIED.value
                if (spec.slug, number) in in_house
                else RoomStatus.AVAILABLE.value
            ),
        )
        for spec in HOTELS
        for room_type, number, floor in _rooms_of(spec)
    )

    return DemoPlan(
        config=config,
        hotels=hotels,
        rooms=rooms,
        guests=tuple(guests),
        bookings=tuple(bookings),
        payments=_payments(config, bookings),
        reviews=_reviews(config, bookings, {g.public_id: g for g in guests}),
        revenue=_revenue(config, bookings),
        expenses=_expenses(config, bookings),
    )


def _hotel(slug: str) -> HotelSpec:
    return next(spec for spec in HOTELS if spec.slug == slug)


def _payments(config: SeedConfig, bookings: Sequence[BookingRow]) -> tuple[PaymentRow, ...]:
    """Captured charges only, each dated no later than the as-of moment."""
    rng = stream(config.seed, "payments")
    payments: list[PaymentRow] = []
    for booking in bookings:
        zone = ZoneInfo(_hotel(booking.hotel).timezone)
        first_night = booking.nights[0].rate
        prepaid = booking.nights[0].rate_plan_code == "NONREF"
        method_options = (
            ((PaymentMethod.OTA_COLLECT.value, 1),)
            if booking.channel_reference
            else (
                (PaymentMethod.CARD.value, 70),
                (PaymentMethod.CASH.value, 15),
                (PaymentMethod.BANK_TRANSFER.value, 15),
            )
        )
        method = weighted(rng, method_options)
        charge: tuple[decimal.Decimal, dt.datetime] | None = None
        if prepaid and booking.status != BookingStatus.PENDING.value:
            # Non-refundable: paid in full when booked, and kept if cancelled.
            charge = (booking.total_amount, booking.booked_at)
        elif booking.status == BookingStatus.CHECKED_OUT.value:
            charge = (booking.total_amount, local(booking.check_out, 10, 30, zone))
        elif booking.status == BookingStatus.CHECKED_IN.value:
            charge = (first_night, local(booking.check_in, 16, 0, zone))
        elif booking.status == BookingStatus.NO_SHOW.value:
            charge = (first_night, local(booking.check_in + dt.timedelta(days=1), 10, 0, zone))
        if charge is None:
            continue
        payments.append(
            PaymentRow(
                public_id=public_id(config.seed, "payment", str(booking.public_id)),
                hotel=booking.hotel,
                booking=booking.public_id,
                amount=charge[0],
                method=method,
                paid_at=charge[1],
            )
        )
    return tuple(payments)


REVIEW_SOURCE_FOR = {
    BookingSource.BOOKING_COM.value: (ReviewSource.BOOKING_COM.value, 10),
    BookingSource.EXPEDIA.value: (ReviewSource.EXPEDIA.value, 5),
}


def _reviews(
    config: SeedConfig, bookings: Sequence[BookingRow], guests: dict[uuid.UUID, GuestRow]
) -> tuple[ReviewRow, ...]:
    rng = stream(config.seed, "reviews")
    reviews: list[ReviewRow] = []
    for booking in bookings:
        if booking.status != BookingStatus.CHECKED_OUT.value:
            continue
        wanted = chance(rng, 35)
        delay = between(rng, 1, 7)
        stars = weighted(rng, ((5, 38), (4, 34), (3, 16), (2, 8), (1, 4)))
        half = chance(rng, 30)
        rating_only = chance(rng, 30)
        text_index = between(rng, 0, 2)
        responded = chance(rng, 40)
        review_date = booking.check_out + dt.timedelta(days=delay)
        if not wanted or review_date > config.reference_date:
            continue
        source, scale = REVIEW_SOURCE_FOR.get(
            booking.source,
            (
                (ReviewSource.GOOGLE.value, 5)
                if text_index == 0
                else (ReviewSource.TRIPADVISOR.value, 5)
                if text_index == 1
                else (ReviewSource.DIRECT.value, 5)
            ),
        )
        five = decimal.Decimal(stars) - (decimal.Decimal("0.5") if half and stars > 1 else 0)
        rating = (five * 2 if scale == 10 else five).quantize(CENT)
        band = "high" if stars >= 4 else ("mid" if stars == 3 else "low")
        titles, bodies = REVIEW_TEXT[band]
        guest = guests[booking.guest]
        zone = ZoneInfo(_hotel(booking.hotel).timezone)
        reply = local(review_date + dt.timedelta(days=1), 9, 30, zone)
        reviews.append(
            ReviewRow(
                hotel=booking.hotel,
                booking=booking.public_id,
                guest=guest.public_id,
                source=source,
                external_review_id=(
                    None
                    if source == ReviewSource.DIRECT.value
                    else f"demo-{booking.reference.lower()}"
                ),
                rating=rating,
                rating_scale=scale,
                title=None if rating_only else titles[text_index % len(titles)],
                body=None if rating_only else bodies[text_index % len(bodies)],
                reviewer_name=f"{guest.first_name} {guest.last_name[0]}.",
                review_date=review_date,
                responded_at=(
                    reply
                    if responded
                    and reply <= as_of_instant(config.reference_date, _hotel(booking.hotel))
                    else None
                ),
            )
        )
    return tuple(reviews)


def _stayed(booking: BookingRow) -> bool:
    return booking.status in (BookingStatus.CHECKED_OUT.value, BookingStatus.CHECKED_IN.value)


def _revenue(config: SeedConfig, bookings: Sequence[BookingRow]) -> tuple[RevenueRow, ...]:
    """Non-room income only (room revenue lives in booking_room_nights), on closed days."""
    rng = stream(config.seed, "revenue")
    rows: list[RevenueRow] = []
    reference = config.reference_date
    first_day = reference - dt.timedelta(days=config.history_days)
    for spec in HOTELS:
        serial = 0

        def add(
            category: str,
            booking: uuid.UUID | None,
            day: dt.date,
            amount: decimal.Decimal,
            tax_percent: int,
            description: str,
            _slug: str = spec.slug,
            _code: str = spec.code,
        ) -> None:
            nonlocal serial
            serial += 1
            rows.append(
                RevenueRow(
                    hotel=_slug,
                    category=category,
                    booking=booking,
                    revenue_date=day,
                    amount=amount,
                    tax_amount=percent_of(amount, tax_percent),
                    description=description,
                    reference=f"DMO-{_code}-REV-{serial:05d}",
                )
            )

        for booking in (b for b in bookings if b.hotel == spec.slug and _stayed(b)):
            parking = chance(rng, 15)
            for night in booking.nights:
                fnb, spa = chance(rng, 35), chance(rng, 8)
                fnb_amount = cents(rng, decimal.Decimal("12"), decimal.Decimal("65"))
                spa_amount = cents(rng, decimal.Decimal("45"), decimal.Decimal("140"))
                if night.stay_date >= reference:
                    continue
                if fnb:
                    add(
                        "FNB",
                        booking.public_id,
                        night.stay_date,
                        fnb_amount,
                        13,
                        "Restaurant and bar, charged to the room",
                    )
                if spa:
                    add("SPA", booking.public_id, night.stay_date, spa_amount, 24, "Spa treatment")
                if parking:
                    add(
                        "PARKING",
                        booking.public_id,
                        night.stay_date,
                        decimal.Decimal("12.00"),
                        24,
                        "Parking, per night",
                    )
        day = first_day
        while day < reference:
            walk_in = chance(rng, 60)
            amount = cents(rng, decimal.Decimal("40"), decimal.Decimal("260"))
            if walk_in:
                add("FNB", None, day, amount, 13, "Restaurant, non-resident covers")
            day += dt.timedelta(days=1)
    return tuple(rows)


def _expenses(config: SeedConfig, bookings: Sequence[BookingRow]) -> tuple[ExpenseRow, ...]:
    """Costs on closed days, scheduled by distance from the reference date so the schedule
    moves with it: payroll every 14 days, utilities every 28, supplies and laundry weekly."""
    rng = stream(config.seed, "expenses")
    rows: list[ExpenseRow] = []
    reference = config.reference_date
    first_day = reference - dt.timedelta(days=config.history_days)
    for spec in HOTELS:
        occupied: dict[dt.date, int] = {}
        for booking in (b for b in bookings if b.hotel == spec.slug and _stayed(b)):
            for night in booking.nights:
                occupied[night.stay_date] = occupied.get(night.stay_date, 0) + 1
        serial = 0
        day = first_day
        while day < reference:
            distance = (reference - day).days
            utilities_noise = between(rng, 90, 115)
            supplies = cents(rng, decimal.Decimal("90"), decimal.Decimal("420"))
            repair = chance(rng, 4)
            repair_amount = cents(rng, decimal.Decimal("120"), decimal.Decimal("900"))
            due: list[tuple[str, decimal.Decimal, int, str, str]] = []
            if distance % 14 == 0:
                due.append(("PAYROLL", spec.payroll, 0, "Staff payroll, fortnightly", "Payroll"))
            if distance % 28 == 0:
                due.append(
                    (
                        "UTILITIES",
                        percent_of(spec.utilities, utilities_noise),
                        24,
                        "Electricity and water",
                        "Example Utilities Co.",
                    )
                )
            if distance % 7 == 0:
                week = sum(occupied.get(day - dt.timedelta(days=n), 0) for n in range(7))
                due.append(
                    (
                        "SUPPLIES",
                        supplies,
                        24,
                        "Housekeeping and breakfast supplies",
                        "Example Wholesale",
                    )
                )
                if week:
                    due.append(
                        (
                            "LAUNDRY",
                            (decimal.Decimal("2.40") * week).quantize(CENT),
                            24,
                            f"Laundry for {week} occupied room nights",
                            "Example Laundry",
                        )
                    )
            if repair:
                due.append(
                    ("MAINTENANCE", repair_amount, 24, "Repair call-out", "Example Maintenance")
                )
            for category, amount, tax, description, vendor in due:
                serial += 1
                rows.append(
                    ExpenseRow(
                        hotel=spec.slug,
                        category=category,
                        expense_date=day,
                        amount=amount,
                        tax_amount=percent_of(amount, tax),
                        description=description,
                        vendor=vendor,
                        invoice_reference=f"DMO-{spec.code}-EXP-{serial:05d}",
                    )
                )
            day += dt.timedelta(days=1)
    return tuple(rows)


# --- the logical view, shared by the plan and the database ---------------------------------------


def _text(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal | uuid.UUID):
        return str(value)
    return value


def _rows(records: Iterator[tuple[Any, ...]] | list[tuple[Any, ...]]) -> list[list[Any]]:
    return sorted(([_text(v) for v in record] for record in records), key=json.dumps)


def logical_rows(plan: DemoPlan, owner_email: str | None = None) -> dict[str, list[list[Any]]]:
    """What the database must hold after seeding, by natural key, surrogate keys excluded."""
    email = owner_email or plan.config.owner_email
    room_type_rows = [
        (
            h.spec.slug,
            rt.code,
            rt.name,
            rt.standard_occupancy,
            rt.max_occupancy,
            rt.bed_count,
            rt.size_sqm,
            rt.base_price,
            CURRENCY,
        )
        for h in plan.hotels
        for rt in h.spec.room_types
    ]
    return {
        "amenities": _rows(list(AMENITIES)),
        "revenue_categories": _rows([(code, name, False) for code, name in REVENUE_CATEGORIES]),
        "expense_categories": _rows(list(EXPENSE_CATEGORIES)),
        "hotels": _rows(
            [
                (
                    h.public_id,
                    h.spec.name,
                    h.spec.slug,
                    h.spec.city,
                    "GR",
                    h.spec.timezone,
                    CURRENCY,
                    h.spec.star_rating,
                    h.created_at,
                )
                for h in plan.hotels
            ]
        ),
        "room_types": _rows(room_type_rows),
        "room_type_amenities": _rows(
            [
                (h.spec.slug, rt.code, code)
                for h in plan.hotels
                for rt in h.spec.room_types
                for code in rt.amenities
            ]
        ),
        "rooms": _rows([(r.hotel, r.number, r.room_type, r.floor, r.status) for r in plan.rooms]),
        "guests": _rows(
            [
                (
                    g.public_id,
                    g.hotel,
                    g.first_name,
                    g.last_name,
                    g.email,
                    g.country_code,
                    g.preferred_language,
                    g.marketing_opt_in,
                    g.created_at,
                )
                for g in plan.guests
            ]
        ),
        "bookings": _rows(
            [
                (
                    b.public_id,
                    b.hotel,
                    b.reference,
                    b.guest,
                    b.check_in,
                    b.check_out,
                    b.status,
                    b.adults,
                    b.children,
                    b.source,
                    b.channel_reference,
                    b.total_amount,
                    CURRENCY,
                    b.booked_at,
                    b.cancelled_at,
                    b.cancellation_reason,
                    b.booked_at,
                    b.updated_at,
                )
                for b in plan.bookings
            ]
        ),
        "booking_rooms": _rows(
            [
                (b.public_id, b.room, b.status, b.adults, b.children, b.guest_name)
                for b in plan.bookings
            ]
        ),
        "booking_room_nights": _rows(
            [
                (b.public_id, n.stay_date, n.rate, n.rate_plan_code)
                for b in plan.bookings
                for n in b.nights
            ]
        ),
        "payments": _rows(
            [
                (
                    p.public_id,
                    p.booking,
                    PaymentKind.CHARGE.value,
                    p.amount,
                    CURRENCY,
                    p.method,
                    PaymentStatus.CAPTURED.value,
                    p.paid_at,
                )
                for p in plan.payments
            ]
        ),
        "reviews": _rows(
            [
                (
                    r.booking,
                    r.guest,
                    r.source,
                    r.external_review_id,
                    r.rating,
                    r.rating_scale,
                    r.title,
                    r.body,
                    r.reviewer_name,
                    r.review_date,
                    r.responded_at,
                )
                for r in plan.reviews
            ]
        ),
        "revenue": _rows(
            [
                (
                    r.reference,
                    r.hotel,
                    r.category,
                    r.booking,
                    r.revenue_date,
                    r.amount,
                    r.tax_amount,
                    CURRENCY,
                    r.description,
                )
                for r in plan.revenue
            ]
        ),
        "expenses": _rows(
            [
                (
                    e.invoice_reference,
                    e.hotel,
                    e.category,
                    e.expense_date,
                    e.amount,
                    e.tax_amount,
                    CURRENCY,
                    e.description,
                    e.vendor,
                )
                for e in plan.expenses
            ]
        ),
        "users": _rows([(email, "Demo Owner", True)]),
        "user_hotels": _rows([(email, h.spec.slug, HotelRole.OWNER.value) for h in plan.hotels]),
        "demand_observation_periods": _rows(
            [(h.spec.slug, *observation_period(plan.config)) for h in plan.hotels]
        ),
    }


#: The same view, read back from the database. Each query returns the columns above, in order.
READBACK: dict[str, str] = {
    "amenities": "SELECT code, name, category FROM amenities",
    "revenue_categories": "SELECT code, name, is_room_revenue FROM revenue_categories",
    "expense_categories": "SELECT code, name, is_fixed_cost FROM expense_categories",
    "hotels": "SELECT public_id, name, slug, city, country_code, timezone, currency, star_rating,"
    " created_at FROM hotels",
    "room_types": "SELECT h.slug, t.code, t.name, t.standard_occupancy, t.max_occupancy,"
    " t.bed_count, t.size_sqm, t.base_price, t.currency FROM room_types t"
    " JOIN hotels h ON h.id = t.hotel_id",
    "room_type_amenities": "SELECT h.slug, t.code, a.code FROM room_type_amenities x"
    " JOIN room_types t ON t.id = x.room_type_id JOIN hotels h ON h.id = t.hotel_id"
    " JOIN amenities a ON a.id = x.amenity_id",
    "rooms": "SELECT h.slug, r.room_number, t.code, r.floor, r.status FROM rooms r"
    " JOIN hotels h ON h.id = r.hotel_id JOIN room_types t ON t.id = r.room_type_id",
    "guests": "SELECT g.public_id, h.slug, g.first_name, g.last_name, g.email, g.country_code,"
    " g.preferred_language, g.marketing_opt_in, g.created_at FROM guests g"
    " JOIN hotels h ON h.id = g.hotel_id",
    "bookings": "SELECT b.public_id, h.slug, b.reference, g.public_id, b.check_in_date,"
    " b.check_out_date, b.status, b.adults, b.children, b.source, b.channel_reference,"
    " b.total_amount, b.currency, b.booked_at, b.cancelled_at, b.cancellation_reason,"
    " b.created_at, b.updated_at FROM bookings b JOIN hotels h ON h.id = b.hotel_id"
    " JOIN guests g ON g.id = b.guest_id",
    "booking_rooms": "SELECT b.public_id, r.room_number, x.booking_status, x.adults, x.children,"
    " x.guest_name FROM booking_rooms x JOIN bookings b ON b.id = x.booking_id"
    " JOIN rooms r ON r.id = x.room_id",
    "booking_room_nights": "SELECT b.public_id, n.stay_date, n.rate, n.rate_plan_code"
    " FROM booking_room_nights n JOIN booking_rooms x ON x.id = n.booking_room_id"
    " JOIN bookings b ON b.id = x.booking_id",
    "payments": "SELECT p.public_id, b.public_id, p.kind, p.amount, p.currency, p.method,"
    " p.status, p.paid_at FROM payments p JOIN bookings b ON b.id = p.booking_id",
    "reviews": "SELECT b.public_id, g.public_id, r.source, r.external_review_id, r.rating,"
    " r.rating_scale, r.title, r.body, r.reviewer_name, r.review_date, r.responded_at"
    " FROM reviews r JOIN bookings b ON b.id = r.booking_id JOIN guests g ON g.id = r.guest_id",
    "revenue": "SELECT r.reference, h.slug, c.code, b.public_id, r.revenue_date, r.amount,"
    " r.tax_amount, r.currency, r.description FROM revenue r JOIN hotels h ON h.id = r.hotel_id"
    " JOIN revenue_categories c ON c.id = r.category_id"
    " LEFT JOIN bookings b ON b.id = r.booking_id",
    "expenses": "SELECT e.invoice_reference, h.slug, c.code, e.expense_date, e.amount,"
    " e.tax_amount, e.currency, e.description, e.vendor FROM expenses e"
    " JOIN hotels h ON h.id = e.hotel_id JOIN expense_categories c ON c.id = e.category_id",
    "users": "SELECT email, full_name, is_active FROM users",
    "user_hotels": "SELECT u.email, h.slug, m.role FROM user_hotels m"
    " JOIN users u ON u.id = m.user_id JOIN hotels h ON h.id = m.hotel_id",
    "demand_observation_periods": "SELECT h.slug, p.observed_from, p.observed_to"
    " FROM demand_observation_periods p JOIN hotels h ON h.id = p.hotel_id",
}


def database_rows(connection: sa.Connection) -> dict[str, list[list[Any]]]:
    return {
        table: _rows([tuple(row) for row in connection.execute(sa.text(query))])
        for table, query in READBACK.items()
    }


def fingerprint(rows: dict[str, list[list[Any]]]) -> str:
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# --- writing -------------------------------------------------------------------------------------


class SeedRefusedError(RuntimeError):
    """The target is not a database this script may write to. Nothing was written."""


def alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "database" / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:  # pragma: no cover - the chain always has a head
        raise SeedRefusedError("the migration chain has no head")
    return head


def check_target(connection: sa.Connection) -> None:
    """Refuse anything but an empty database at the migration head. Reads only."""
    inspector = sa.inspect(connection)
    if not inspector.has_table("alembic_version"):
        raise SeedRefusedError(
            "The target has no alembic_version table: it has not been migrated.\n"
            "Migrate it first:  alembic -x url=<URL> upgrade head"
        )
    current = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
    versions = list(current)
    head = alembic_head()
    if versions != [head]:
        raise SeedRefusedError(
            f"The target is at revision {versions or 'none'}, not the head {head!r}.\n"
            "This script never migrates. Migrate it first:  alembic -x url=<URL> upgrade head"
        )
    occupied = [
        table.name
        for table in Base.metadata.sorted_tables
        if connection.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table.name}")')).scalar()
    ]
    if occupied:
        raise SeedRefusedError(
            "The target already holds data in: " + ", ".join(sorted(occupied)) + ".\n"
            "Nothing was written. This script only seeds an EMPTY database and never deletes,\n"
            "truncates or overwrites anything. Create a new database for the demo instead\n"
            "(database/README.md, 'Demo data')."
        )


def read_password(environ: Mapping[str, str]) -> str:
    password = environ.get(PASSWORD_VARIABLE, "")
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise SeedRefusedError(
            f"Set {PASSWORD_VARIABLE} to the demo owner's password "
            f"({PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} characters). It is read from the "
            "environment so it never appears in a command line, and it is never printed."
        )
    return password


def write_plan(session: Session, plan: DemoPlan, password_hash: str) -> None:
    """Insert the plan through the application's own models. Flushes; never commits."""
    amenities = {
        code: Amenity(code=code, name=name, category=category) for code, name, category in AMENITIES
    }
    revenue_categories = {
        code: RevenueCategory(code=code, name=name, is_room_revenue=False)
        for code, name in REVENUE_CATEGORIES
    }
    expense_categories = {
        code: ExpenseCategory(code=code, name=name, is_fixed_cost=fixed)
        for code, name, fixed in EXPENSE_CATEGORIES
    }
    _flush(
        session, [*amenities.values(), *revenue_categories.values(), *expense_categories.values()]
    )

    hotels: dict[str, Hotel] = {}
    for row in plan.hotels:
        spec = row.spec
        hotels[spec.slug] = Hotel(
            public_id=row.public_id,
            name=spec.name,
            slug=spec.slug,
            address_line1=spec.address_line1,
            city=spec.city,
            postal_code=spec.postal_code,
            country_code="GR",
            email=f"reservations@{spec.slug}.example.com",
            website=f"https://{spec.slug}.example.com",
            timezone=spec.timezone,
            currency=CURRENCY,
            star_rating=spec.star_rating,
            is_active=True,
            created_at=row.created_at,
            updated_at=row.created_at,
        )
    _flush(session, list(hotels.values()))

    created = {row.spec.slug: row.created_at for row in plan.hotels}
    room_types: dict[tuple[str, str], RoomType] = {}
    for row in plan.hotels:
        for rt in row.spec.room_types:
            room_types[(row.spec.slug, rt.code)] = RoomType(
                hotel_id=hotels[row.spec.slug].id,
                name=rt.name,
                code=rt.code,
                max_occupancy=rt.max_occupancy,
                standard_occupancy=rt.standard_occupancy,
                bed_count=rt.bed_count,
                bed_configuration=rt.bed_configuration,
                size_sqm=rt.size_sqm,
                base_price=rt.base_price,
                currency=CURRENCY,
                is_active=True,
                created_at=created[row.spec.slug],
                updated_at=created[row.spec.slug],
            )
    _flush(session, list(room_types.values()))
    _flush(
        session,
        [
            RoomTypeAmenity(
                room_type_id=room_types[(row.spec.slug, rt.code)].id, amenity_id=amenities[code].id
            )
            for row in plan.hotels
            for rt in row.spec.room_types
            for code in rt.amenities
        ],
    )

    rooms = {
        (r.hotel, r.number): Room(
            hotel_id=hotels[r.hotel].id,
            room_type_id=room_types[(r.hotel, r.room_type)].id,
            room_number=r.number,
            floor=r.floor,
            status=r.status,
            is_active=True,
            created_at=created[r.hotel],
            updated_at=created[r.hotel],
        )
        for r in plan.rooms
    }
    _flush(session, list(rooms.values()))

    guests = {
        g.public_id: Guest(
            public_id=g.public_id,
            hotel_id=hotels[g.hotel].id,
            first_name=g.first_name,
            last_name=g.last_name,
            email=g.email,
            country_code=g.country_code,
            preferred_language=g.preferred_language,
            marketing_opt_in=g.marketing_opt_in,
            created_at=g.created_at,
            updated_at=g.created_at,
        )
        for g in plan.guests
    }
    _flush(session, list(guests.values()))

    bookings = {
        b.public_id: Booking(
            public_id=b.public_id,
            hotel_id=hotels[b.hotel].id,
            guest_id=guests[b.guest].id,
            reference=b.reference,
            check_in_date=b.check_in,
            check_out_date=b.check_out,
            status=b.status,
            adults=b.adults,
            children=b.children,
            source=b.source,
            channel_reference=b.channel_reference,
            total_amount=b.total_amount,
            currency=CURRENCY,
            cancelled_at=b.cancelled_at,
            cancellation_reason=b.cancellation_reason,
            booked_at=b.booked_at,
            created_at=b.booked_at,
            updated_at=b.updated_at,
        )
        for b in plan.bookings
    }
    _flush(session, list(bookings.values()))

    allocations = {
        b.public_id: BookingRoom(
            booking_id=bookings[b.public_id].id,
            room_id=rooms[(b.hotel, b.room)].id,
            hotel_id=hotels[b.hotel].id,
            check_in_date=b.check_in,
            check_out_date=b.check_out,
            booking_status=b.status,
            adults=b.adults,
            children=b.children,
            guest_name=b.guest_name,
            created_at=b.booked_at,
            updated_at=b.updated_at,
        )
        for b in plan.bookings
    }
    _flush(session, list(allocations.values()))
    _flush(
        session,
        [
            BookingRoomNight(
                booking_room_id=allocations[b.public_id].id,
                hotel_id=hotels[b.hotel].id,
                check_in_date=b.check_in,
                check_out_date=b.check_out,
                stay_date=n.stay_date,
                rate=n.rate,
                rate_plan_code=n.rate_plan_code,
                is_complimentary=False,
                created_at=b.booked_at,
                updated_at=b.booked_at,
            )
            for b in plan.bookings
            for n in b.nights
        ],
    )

    _flush(
        session,
        [
            Payment(
                public_id=p.public_id,
                booking_id=bookings[p.booking].id,
                hotel_id=hotels[p.hotel].id,
                kind=PaymentKind.CHARGE.value,
                amount=p.amount,
                currency=CURRENCY,
                method=p.method,
                status=PaymentStatus.CAPTURED.value,
                paid_at=p.paid_at,
                created_at=p.paid_at,
                updated_at=p.paid_at,
            )
            for p in plan.payments
        ],
    )
    _flush(
        session,
        [
            Review(
                hotel_id=hotels[r.hotel].id,
                guest_id=guests[r.guest].id,
                booking_id=bookings[r.booking].id,
                source=r.source,
                external_review_id=r.external_review_id,
                rating=r.rating,
                rating_scale=r.rating_scale,
                title=r.title,
                body=r.body,
                language="en",
                reviewer_name=r.reviewer_name,
                review_date=r.review_date,
                is_published=True,
                responded_at=r.responded_at,
                created_at=_noon(r.review_date, r.hotel),
                updated_at=_noon(r.review_date, r.hotel),
            )
            for r in plan.reviews
        ],
    )
    _flush(
        session,
        [
            Revenue(
                hotel_id=hotels[r.hotel].id,
                category_id=revenue_categories[r.category].id,
                booking_id=None if r.booking is None else bookings[r.booking].id,
                revenue_date=r.revenue_date,
                amount=r.amount,
                tax_amount=r.tax_amount,
                currency=CURRENCY,
                description=r.description,
                reference=r.reference,
                created_at=_noon(r.revenue_date, r.hotel),
                updated_at=_noon(r.revenue_date, r.hotel),
            )
            for r in plan.revenue
        ],
    )
    _flush(
        session,
        [
            Expense(
                hotel_id=hotels[e.hotel].id,
                category_id=expense_categories[e.category].id,
                expense_date=e.expense_date,
                amount=e.amount,
                tax_amount=e.tax_amount,
                currency=CURRENCY,
                description=e.description,
                vendor=e.vendor,
                invoice_reference=e.invoice_reference,
                is_recurring=False,
                recurrence_interval=None,
                created_at=_noon(e.expense_date, e.hotel),
                updated_at=_noon(e.expense_date, e.hotel),
            )
            for e in plan.expenses
        ],
    )

    # Credentials are not demo history: the account's timestamps are the database's now().
    owner = User(
        email=plan.config.owner_email,
        password_hash=password_hash,
        full_name="Demo Owner",
        is_active=True,
    )
    _flush(session, [owner])
    _flush(
        session,
        [
            UserHotel(user_id=owner.id, hotel_id=hotel.id, role=HotelRole.OWNER.value)
            for hotel in hotels.values()
        ],
    )

    # Last, and only because everything it vouches for is now written.
    observed_from, observed_to = observation_period(plan.config)
    _flush(
        session,
        [
            DemandObservationPeriod(
                hotel_id=hotel.id, observed_from=observed_from, observed_to=observed_to
            )
            for hotel in hotels.values()
        ],
    )


def _noon(day: dt.date, slug: str) -> dt.datetime:
    return local(day, 12, 0, ZoneInfo(_hotel(slug).timezone))


def _flush(session: Session, rows: Sequence[Any]) -> None:
    session.add_all(rows)
    session.flush()


@dataclasses.dataclass(frozen=True)
class SeedResult:
    fingerprint: str
    counts: dict[str, int]


def seed_database(
    database_url: str,
    plan: DemoPlan,
    password: str,
    *,
    hash_password: Callable[[str], str] | None = None,
) -> SeedResult:
    """Check the target, write the plan, verify it, and commit -- or change nothing at all."""
    if hash_password is None:
        from app.core.security import hash_password as argon2_hash

        hash_password = argon2_hash

    engine = sa.create_engine(database_url, future=True, poolclass=sa.pool.NullPool)
    try:
        # ONE transaction, owned by the connection: the check, every insert and the readback
        # happen inside it, and it commits only when the block exits cleanly -- which is also
        # when the deferred constraint triggers fire. Any exception rolls all of it back. The
        # Session joins this transaction and is never asked to commit it.
        with engine.connect() as connection, connection.begin():
            check_target(connection)
            session = Session(bind=connection, expire_on_commit=False)
            try:
                write_plan(session, plan, hash_password(password))
                expected = logical_rows(plan)
                actual = database_rows(connection)
            finally:
                session.close()
            if actual != expected:
                different = sorted(t for t in expected if expected[t] != actual.get(t))
                raise SeedRefusedError(
                    "The rows read back differ from the plan in: "
                    + ", ".join(different)
                    + ". Rolled back; nothing was kept."
                )
        # A NEW connection, after the commit: what it reads is what was kept.
        with engine.connect() as connection:
            final = database_rows(connection)
    finally:
        engine.dispose()
    return SeedResult(
        fingerprint=fingerprint(final),
        counts={table: len(rows) for table, rows in final.items()},
    )


# --- command line --------------------------------------------------------------------------------


def parse_reference_date(value: str) -> dt.date:
    """An ISO date, or the literal ``today`` -- the only way the wall clock gets in."""
    if value == "today":
        return dt.date.today()
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"not an ISO date (YYYY-MM-DD) or 'today': {value!r}"
        ) from exc


def summary(plan: DemoPlan) -> list[str]:
    reference = plan.config.reference_date
    lines = [
        f"reference date : {reference.isoformat()} (as of {AS_OF_HOUR:02d}:00 hotel-local)",
        f"seed           : {plan.config.seed}",
        f"stays from     : {min(b.check_in for b in plan.bookings).isoformat()}"
        f"  to {max(b.check_out for b in plan.bookings).isoformat()}",
        "observed       : {} to {} (declared, both inclusive)".format(
            *(day.isoformat() for day in observation_period(plan.config))
        ),
    ]
    for status in BookingStatus:
        count = sum(1 for b in plan.bookings if b.status == status.value)
        lines.append(f"  {status.value:<12} {count}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed_demo",
        description="Write deterministic, synthetic demo data into an EMPTY database that is "
        "already at the migration head. Never migrates, deletes or overwrites.",
    )
    parser.add_argument(
        "--reference-date",
        required=True,
        type=parse_reference_date,
        help="the 'today' the data describes: YYYY-MM-DD, or 'today' for the machine's date",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help=f"random seed (default {DEFAULT_SEED})"
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=DEFAULT_HISTORY_DAYS,
        help=f"days of stays before the reference date (default {DEFAULT_HISTORY_DAYS})",
    )
    parser.add_argument(
        "--future-days",
        type=int,
        default=DEFAULT_FUTURE_DAYS,
        help=f"days of stays after it (default {DEFAULT_FUTURE_DAYS})",
    )
    parser.add_argument(
        "--owner-email",
        default=DEFAULT_OWNER_EMAIL,
        help=f"the demo owner's login (default {DEFAULT_OWNER_EMAIL}); its "
        f"password is read from {PASSWORD_VARIABLE}",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database-url", help="the empty, migrated database to write to")
    target.add_argument(
        "--dry-run",
        action="store_true",
        help="build the plan and print its summary and fingerprint only",
    )
    args = parser.parse_args(argv)

    try:
        config = SeedConfig(
            reference_date=args.reference_date,
            seed=args.seed,
            history_days=args.history_days,
            future_days=args.future_days,
            owner_email=args.owner_email,
        )
    except ValueError as exc:
        parser.error(str(exc))
    plan = build_plan(config)
    for line in summary(plan):
        print(line)
    planned = logical_rows(plan)
    print(f"plan fingerprint     : {fingerprint(planned)}")
    if args.dry_run:
        for table, rows in planned.items():
            print(f"  {table:<20} {len(rows)}")
        return 0

    redacted = sa.engine.make_url(args.database_url).render_as_string(hide_password=True)
    print(f"target               : {redacted}")
    try:
        result = seed_database(args.database_url, plan, read_password(os.environ))
    except SeedRefusedError as exc:
        print(f"\nREFUSED. {exc}", file=sys.stderr)
        return 1
    for table, count in result.counts.items():
        print(f"  {table:<20} {count}")
    print(f"database fingerprint : {result.fingerprint}")
    print(f"log in as {config.owner_email} with the password from {PASSWORD_VARIABLE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
