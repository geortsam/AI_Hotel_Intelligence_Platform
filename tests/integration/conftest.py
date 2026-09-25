"""Fixtures for the PostgreSQL integration suite.

These tests exercise behaviour that ONLY PostgreSQL provides -- GiST exclusion constraints,
deferred constraint triggers, generated columns, partial indexes, composite-FK cascades.
They are never silently redirected to SQLite: SQLite has none of those features, so a green
run against it would prove nothing while looking like proof.

If ``TEST_DATABASE_URL`` is not set, every test here skips with an explicit reason and is
reported as PENDING -- not as passing.

    $env:TEST_DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/hotel_test"

The schema is created by running the real Alembic migration, so these tests verify the
migration as well as the models.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Booking,
    BookingRoom,
    BookingRoomNight,
    Guest,
    Hotel,
    Room,
    RoomType,
)
from tests.db_safety import (
    DANGEROUS_HOST_TOKENS,
    REQUIRED_DB_SUFFIX,
    UnsafeTestDatabaseError,
    assert_safe_destructive_target,
    assert_safe_test_database_url,
    database_name_from_url,
    redact_url,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


# ======================================================================================
# Safety guard
#
# This suite is destructive by design: it runs `alembic downgrade base` (DROP TABLE on
# every table) once per session, and TRUNCATE ... RESTART IDENTITY CASCADE after every
# single test.
#
# The guard itself lives in `tests/db_safety.py` -- one authoritative implementation, shared
# by this conftest and by the guard tests, rather than a copy per caller. It applies two
# independent signals: the URL's shape and name, and then the target's actual contents.
# Stage 5.17 added the second because the first cannot be sufficient here -- CI's ephemeral
# container database and the seeded demo database have the same name.
#
# The names below are re-exported because `tests/backend/test_integration_safety_guard.py`
# has always imported them from this module.
# ======================================================================================

#: Re-exported for `tests/backend/test_integration_safety_guard.py`, which has imported
#: these from this module since Stage 2C. Listing them here says they are deliberate public
#: names rather than leftovers.
__all__ = [
    "DANGEROUS_HOST_TOKENS",
    "REQUIRED_DB_SUFFIX",
    "UnsafeTestDatabaseError",
    "assert_safe_destructive_target",
    "assert_safe_test_database_url",
    "database_name_from_url",
    "redact_url",
    "requires_postgres",
]


#: Applied as ``pytestmark`` by each test module. A marker defined in a conftest does NOT
#: propagate to test modules, so it is exported and applied explicitly -- otherwise the
#: fixture raises and the suite reports ERROR instead of the honest PENDING/SKIPPED.
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason=(
        "PostgreSQL integration tests are PENDING: TEST_DATABASE_URL is not set. "
        "These verify GiST exclusion constraints, deferred constraint triggers and "
        "generated columns, none of which SQLite can emulate. Set TEST_DATABASE_URL to a "
        "throwaway PostgreSQL database to run them."
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """An engine against the throwaway test database, with the migration applied."""
    if not TEST_DATABASE_URL:  # pragma: no cover - belt and braces alongside the marker
        pytest.skip("TEST_DATABASE_URL is not set")

    # FIRST thing that happens, and nothing destructive is in it: the URL is analysed as a
    # string, and then the target is READ -- three SELECTs -- to confirm it holds nothing
    # worth keeping. Both must pass. Nothing below this line may run against a database that
    # has not cleared both signals.
    assert_safe_destructive_target(TEST_DATABASE_URL)

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "database" / "migrations"))
    cfg.cmd_opts = None
    os.environ["TEST_DATABASE_URL"] = TEST_DATABASE_URL

    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    eng = sa.create_engine(TEST_DATABASE_URL, future=True, poolclass=sa.pool.NullPool)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back after each test.

    The outer transaction is a real one, so DEFERRED constraint triggers fire on the
    SAVEPOINT/commit boundary the tests drive explicitly.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        yield s
        s.rollback()
        # Truncate rather than rely on rollback alone: several tests commit deliberately,
        # because a deferred constraint trigger can only be observed at COMMIT.
        s.execute(
            sa.text(
                # Stage 6.8. demand_predictions references hotels, so CASCADE would reach
                # it -- it is named anyway, like audit_events and the identity tables
                # below, so the teardown says what it clears rather than implying it.
                # Stage 7.7. llm_invocations is append-only by trigger, and like audit_events
                # the trigger is row-level, so TRUNCATE clears it. Named for the same reason.
                "TRUNCATE llm_invocations, demand_predictions, "
                "booking_room_nights, booking_rooms, payments, reviews, revenue, "
                "expenses, daily_hotel_metrics, bookings, guests, rooms, room_types, "
                "room_type_amenities, amenities, revenue_categories, expense_categories, "
                # Stage 4.5.12. audit_events is append-only -- migration 0007 installs a
                # trigger that refuses UPDATE and DELETE on it. TRUNCATE is neither: it fires
                # only statement-level TRUNCATE triggers, of which the table has none. So the
                # suite can still start each test from empty without the immutability
                # guarantee having a hole in it. It is named explicitly rather than left to
                # CASCADE, like the three identity tables below and for the same reason.
                # Stage 4.5.14. The archive is append-only too, and TRUNCATE is likewise
                # neither an UPDATE nor a DELETE. Listed before audit_events for readability
                # only -- TRUNCATE takes them in one statement and has no FK between them to
                # order around, deliberately (see migration 0008).
                "audit_events_archive, audit_events, "
                # users, user_hotels and platform_admins (Stages 4.1/4.2/4.3) are listed
                # explicitly: users has no foreign key to hotels, so the CASCADE from
                # `hotels` does not reach it, and naming all three keeps the order
                # independent of cascade behaviour.
                "platform_admins, user_hotels, users, hotels RESTART IDENTITY CASCADE"
            )
        )
        s.commit()


# --------------------------------------------------------------------------------------
# Builders. Each returns a persisted row so tests read as scenarios, not as setup noise.
# --------------------------------------------------------------------------------------


def make_hotel(session: Session, *, slug: str | None = None, currency: str = "EUR") -> Hotel:
    slug = slug or f"hotel-{uuid.uuid4().hex[:8]}"
    hotel = Hotel(
        name=f"Test Hotel {slug}",
        slug=slug,
        address_line1="1 Test Street",
        city="Athens",
        country_code="GR",
        timezone="Europe/Athens",
        currency=currency,
    )
    session.add(hotel)
    session.flush()
    return hotel


def make_room_type(session: Session, hotel: Hotel, *, code: str = "DBL") -> RoomType:
    room_type = RoomType(
        hotel_id=hotel.id,
        name="Double",
        code=code,
        max_occupancy=3,
        standard_occupancy=2,
        bed_count=1,
        base_price=Decimal("120.00"),
        currency=hotel.currency,
    )
    session.add(room_type)
    session.flush()
    return room_type


def make_room(session: Session, hotel: Hotel, room_type: RoomType, *, number: str) -> Room:
    room = Room(
        hotel_id=hotel.id,
        room_type_id=room_type.id,
        room_number=number,
    )
    session.add(room)
    session.flush()
    return room


def make_guest(session: Session, hotel: Hotel, *, email: str | None = None) -> Guest:
    guest = Guest(
        hotel_id=hotel.id,
        first_name="Ada",
        last_name="Lovelace",
        email=email,
        country_code="GB",
    )
    session.add(guest)
    session.flush()
    return guest


def make_booking(
    session: Session,
    hotel: Hotel,
    guest: Guest,
    *,
    check_in: dt.date,
    check_out: dt.date,
    status: str = "confirmed",
    reference: str | None = None,
    total: str = "480.00",
) -> Booking:
    booking = Booking(
        hotel_id=hotel.id,
        guest_id=guest.id,
        reference=reference or f"BK-{uuid.uuid4().hex[:10]}",
        check_in_date=check_in,
        check_out_date=check_out,
        status=status,
        total_amount=Decimal(total),
        currency=hotel.currency,
        cancelled_at=dt.datetime.now(dt.UTC) if status == "cancelled" else None,
    )
    session.add(booking)
    session.flush()
    return booking


def allocate_room(session: Session, booking: Booking, room: Room) -> BookingRoom:
    """Attach a physical room to a booking, mirroring the parent's stay window."""
    booking_room = BookingRoom(
        booking_id=booking.id,
        room_id=room.id,
        hotel_id=booking.hotel_id,
        check_in_date=booking.check_in_date,
        check_out_date=booking.check_out_date,
        booking_status=booking.status,
    )
    session.add(booking_room)
    session.flush()
    return booking_room


def price_nights(
    session: Session,
    booking_room: BookingRoom,
    rates: list[str],
    *,
    skip: set[int] | None = None,
) -> list[BookingRoomNight]:
    """Create one night row per rate, starting at check-in.

    ``skip`` omits the given zero-based offsets, which is how the completeness tests
    construct a deliberately incomplete night set.
    """
    skip = skip or set()
    rows: list[BookingRoomNight] = []
    for offset, rate in enumerate(rates):
        if offset in skip:
            continue
        night = BookingRoomNight(
            booking_room_id=booking_room.id,
            hotel_id=booking_room.hotel_id,
            check_in_date=booking_room.check_in_date,
            check_out_date=booking_room.check_out_date,
            stay_date=booking_room.check_in_date + dt.timedelta(days=offset),
            rate=Decimal(rate),
        )
        session.add(night)
        rows.append(night)
    session.flush()
    return rows


# ======================================================================================
# Stage 4.2 authentication and membership helpers.
#
# Every hotel-scoped endpoint now requires an authenticated caller who is a MEMBER of the
# hotel. Rather than teach each of the twelve integration suites how to sign in, they share
# the helpers below: `authenticated_client` returns a TestClient that already carries a
# bearer token, and `POST /hotels` makes its creator the owner, so a suite that builds its
# own hotels is a full owner of them with no further setup.
# ======================================================================================

#: A signing secret for the test process only. Production refuses to start without its own.
TEST_SECRET = "integration-suite-signing-secret-not-for-production"

#: Long enough to satisfy the registration floor, and obviously not a real credential.
TEST_PASSWORD = "integration-suite-password"


def register_and_login(client: TestClient, email: str, password: str = TEST_PASSWORD) -> str:
    """Create an account and return its bearer token.

    Registration is idempotent from the caller's point of view: a 409 means the account
    already exists in this test, which is fine -- the login below is what matters.
    """
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Integration Suite"},
    )
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def create_test_app(engine: Engine, **settings: Any) -> Any:
    """An application wired to the throwaway test database.

    Extracted so a suite that needs the APP -- to reach `app.state`, or to build clients with
    different source addresses -- does not have to duplicate the session override. Keyword
    arguments are passed through to Settings, which is how a suite tightens a limit rather
    than waiting for the real one.

    Returns a FastAPI instance; typed loosely so this module does not import FastAPI at
    collection time for suites that never build an app.
    """
    from app.api.deps import get_db
    from app.core.config import Settings
    from app.main import create_app

    app = create_app(Settings(environment="test", secret_key=TEST_SECRET, **settings))
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    return app


def authenticated_client(engine: Engine, *, email: str = "suite@example.test") -> TestClient:
    """A TestClient carrying a valid token for a freshly registered user.

    The token goes on the client's default headers, so existing suites keep calling
    `api.get(...)` unchanged. The user starts with no memberships; they gain `owner` on every
    hotel they create through the API.

    Each call builds its own app, so each carries its own rate-limiter state (Stage 4.5.3) --
    which is why one suite's logins cannot exhaust another's budget.
    """
    app = create_test_app(engine)
    bootstrap = TestClient(app)
    token = register_and_login(bootstrap, email)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def grant_membership(engine: Engine, email: str, hotel_public_id: str, role: str) -> None:
    """Give an existing user a role at an existing hotel, directly.

    Deliberately a database write rather than an API call: Stage 4.2 exposes no membership
    endpoint, and inventing one just to make tests convenient would be building product from
    a test's needs.
    """
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text(
                "INSERT INTO user_hotels (user_id, hotel_id, role) "
                "SELECT u.id, h.id, :role FROM users u, hotels h "
                "WHERE u.email = :email AND h.public_id = CAST(:hotel AS uuid) "
                "ON CONFLICT (user_id, hotel_id) DO UPDATE SET role = EXCLUDED.role"
            ),
            {"role": role, "email": email.lower(), "hotel": hotel_public_id},
        )
        session.commit()


def revoke_membership(engine: Engine, email: str, hotel_public_id: str) -> None:
    """Remove a user's membership of a hotel."""
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text(
                "DELETE FROM user_hotels uh USING users u, hotels h "
                "WHERE uh.user_id = u.id AND uh.hotel_id = h.id "
                "AND u.email = :email AND h.public_id = CAST(:hotel AS uuid)"
            ),
            {"email": email.lower(), "hotel": hotel_public_id},
        )
        session.commit()


def grant_platform_admin(engine: Engine, email: str) -> None:
    """Make an existing user a platform administrator, directly.

    A database write rather than an API call, because Stage 4.3 deliberately exposes no
    endpoint for granting platform administration -- the privilege that can edit every
    hotel's shared vocabulary is not one an API should hand out. This helper is the test
    suite's instance of the same out-of-band grant a real installation performs.
    """
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text(
                "INSERT INTO platform_admins (user_id, role) "
                "SELECT u.id, 'platform_admin' FROM users u WHERE u.email = :email "
                "ON CONFLICT (user_id) DO NOTHING"
            ),
            {"email": email.lower()},
        )
        session.commit()


def revoke_platform_admin(engine: Engine, email: str) -> None:
    """Remove a user's platform grant."""
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text(
                "DELETE FROM platform_admins pa USING users u "
                "WHERE pa.user_id = u.id AND u.email = :email"
            ),
            {"email": email.lower()},
        )
        session.commit()


def seed_amenity(engine: Engine, code: str, name: str, category: str | None = None) -> None:
    """Insert a global amenity directly.

    A row shared by every hotel cannot be governed by a per-hotel grant, so from Stage 4.2 the
    catalogue write endpoints are closed to every hotel role; Stage 4.3 reopened them to the
    platform administrator alone. Seeding stays the right tool for a suite that is not testing
    that privilege -- see `grant_platform_admin` for the suite that is.
    """
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text(
                "INSERT INTO amenities (code, name, category) VALUES (:c, :n, :cat) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"c": code.upper(), "n": name, "cat": category},
        )
        session.commit()


def seed_revenue_category(
    engine: Engine, code: str, name: str, *, is_room_revenue: bool = False, is_active: bool = True
) -> None:
    """Insert a global revenue category directly. See seed_amenity for why."""
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text(
                "INSERT INTO revenue_categories (code, name, is_room_revenue, is_active) "
                "VALUES (:c, :n, :room, :active) ON CONFLICT (code) DO NOTHING"
            ),
            {"c": code.upper(), "n": name, "room": is_room_revenue, "active": is_active},
        )
        session.commit()


def seed_expense_category(
    engine: Engine, code: str, name: str, *, is_fixed_cost: bool = False, is_active: bool = True
) -> None:
    """Insert a global expense category directly. See seed_amenity for why."""
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text(
                "INSERT INTO expense_categories (code, name, is_fixed_cost, is_active) "
                "VALUES (:c, :n, :fixed, :active) ON CONFLICT (code) DO NOTHING"
            ),
            {"c": code.upper(), "n": name, "fixed": is_fixed_cost, "active": is_active},
        )
        session.commit()
