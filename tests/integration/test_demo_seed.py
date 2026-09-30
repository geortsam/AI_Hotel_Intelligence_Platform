"""The demo-data seed against real PostgreSQL: it initialises a fresh database, the schema accepts
every row, the rows read back exactly as planned, and the application serves them.

The database-free half is `tests/backend/test_demo_seed.py`. This half exists because only
PostgreSQL can prove the claims that matter most: the GiST exclusion constraint, the deferred
night-completeness trigger, the composite tenant keys and the CHECK constraints all accept the
seeded rows, and a second seed into the same database is refused with nothing changed.

Every test here starts from an EMPTY database, because the seed refuses anything else and other
suites leave accounts behind. The target is the suite's own disposable database, which the
session fixture has already cleared through both safety signals.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.db.base import Base
from scripts import seed_demo
from scripts.seed_demo import SeedConfig, SeedRefusedError, build_plan, fingerprint, logical_rows
from tests.integration.conftest import TEST_DATABASE_URL, create_test_app, requires_postgres

pytestmark = requires_postgres

#: Short enough to seed in a few seconds, long enough for the 90-day windows and the 90-day
#: forecast training window the intelligence page defaults to.
CONFIG = SeedConfig(reference_date=dt.date(2026, 9, 30), history_days=120, future_days=45)
PASSWORD = "demo-owner-password-for-the-suite"


def fast_hash(password: str) -> str:
    """Argon2 is deliberately slow; the tests that never log in use a stand-in digest."""
    return f"not-a-real-hash:{len(password)}"


def empty(engine: Engine) -> None:
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def fresh(engine: Engine) -> Iterator[str]:
    """The suite's disposable database, emptied before and after the test."""
    assert TEST_DATABASE_URL is not None
    empty(engine)
    yield TEST_DATABASE_URL
    empty(engine)


def current_rows(engine: Engine) -> dict[str, list[list[object]]]:
    with engine.connect() as connection:
        return seed_demo.database_rows(connection)


# --- it initialises a fresh database, exactly as planned -----------------------------------------


def test_the_seed_initialises_a_fresh_database_and_reads_back_as_planned(
    fresh: str, engine: Engine
) -> None:
    plan = build_plan(CONFIG)

    result = seed_demo.seed_database(fresh, plan, PASSWORD, hash_password=fast_hash)

    assert result.fingerprint == fingerprint(logical_rows(plan))
    assert current_rows(engine) == logical_rows(plan)
    assert result.counts["bookings"] == len(plan.bookings) > 500
    assert result.counts["booking_room_nights"] == sum(len(b.nights) for b in plan.bookings)


def test_the_database_holds_the_same_logical_dataset_every_time(fresh: str, engine: Engine) -> None:
    """Seed, empty, seed again: the same seed and reference date give identical rows."""
    first = seed_demo.seed_database(fresh, build_plan(CONFIG), PASSWORD, hash_password=fast_hash)
    empty(engine)
    second = seed_demo.seed_database(fresh, build_plan(CONFIG), PASSWORD, hash_password=fast_hash)
    empty(engine)
    other_seed = seed_demo.seed_database(
        fresh,
        build_plan(SeedConfig(**{**CONFIG.__dict__, "seed": CONFIG.seed + 1})),
        PASSWORD,
        hash_password=fast_hash,
    )

    assert first.fingerprint == second.fingerprint
    assert other_seed.fingerprint != first.fingerprint


def test_every_seeded_row_satisfies_the_constraints_the_orm_cannot_see(
    fresh: str, engine: Engine
) -> None:
    """The commit itself proves the deferred trigger and the exclusion constraint accepted the
    rows. These re-ask the database directly, so a weakened constraint would still be caught."""
    seed_demo.seed_database(fresh, build_plan(CONFIG), PASSWORD, hash_password=fast_hash)

    with engine.connect() as connection:
        incomplete = connection.execute(
            sa.text(
                "SELECT count(*) FROM booking_rooms x WHERE x.nights <> "
                "(SELECT count(*) FROM booking_room_nights n WHERE n.booking_room_id = x.id)"
            )
        ).scalar_one()
        overlapping = connection.execute(
            sa.text(
                "SELECT count(*) FROM booking_rooms a JOIN booking_rooms b "
                "ON a.room_id = b.room_id AND a.id < b.id "
                "AND daterange(a.check_in_date, a.check_out_date) "
                "&& daterange(b.check_in_date, b.check_out_date) "
                "WHERE a.booking_status IN ('confirmed', 'checked_in') "
                "AND b.booking_status IN ('confirmed', 'checked_in')"
            )
        ).scalar_one()
        cross_tenant = connection.execute(
            sa.text(
                "SELECT count(*) FROM bookings b JOIN guests g ON g.id = b.guest_id "
                "WHERE g.hotel_id <> b.hotel_id"
            )
        ).scalar_one()
        overlap_view = connection.execute(
            sa.text("SELECT count(*) FROM historical_room_overlaps")
        ).scalar_one()

    assert incomplete == 0
    assert overlapping == 0
    assert cross_tenant == 0
    assert overlap_view == 0


# --- it refuses anything but an empty database at the head ---------------------------------------


def test_a_second_seed_is_refused_and_changes_nothing(fresh: str, engine: Engine) -> None:
    seed_demo.seed_database(fresh, build_plan(CONFIG), PASSWORD, hash_password=fast_hash)
    before = current_rows(engine)

    with pytest.raises(SeedRefusedError, match=r"already holds data in: .*bookings"):
        seed_demo.seed_database(
            fresh,
            build_plan(SeedConfig(**{**CONFIG.__dict__, "seed": 99})),
            PASSWORD,
            hash_password=fast_hash,
        )

    assert current_rows(engine) == before


def test_one_leftover_account_is_enough_to_refuse(fresh: str, engine: Engine) -> None:
    """Not only business tables: any row in any application table means "not empty"."""
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users (email, password_hash, full_name) "
                "VALUES ('someone@example.test', 'x', 'Someone')"
            )
        )

    with pytest.raises(SeedRefusedError, match="already holds data in: users"):
        seed_demo.seed_database(fresh, build_plan(CONFIG), PASSWORD, hash_password=fast_hash)
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM hotels")).scalar_one() == 0


def test_a_database_behind_the_migration_head_is_refused(fresh: str, engine: Engine) -> None:
    """Checked on a rolled-back transaction: the revision is faked, never really changed."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("UPDATE alembic_version SET version_num = '0014_older'"))
            with pytest.raises(SeedRefusedError, match="not the head"):
                seed_demo.check_target(connection)
        finally:
            transaction.rollback()


def test_a_failure_part_way_leaves_the_database_empty(
    fresh: str, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One transaction: a row the database rejects rolls the whole seed back."""
    plan = build_plan(CONFIG)
    broken = seed_demo.DemoPlan(
        **{
            **plan.__dict__,
            "payments": (
                seed_demo.PaymentRow(**{**plan.payments[0].__dict__, "amount": Decimal("-1.00")}),
                *plan.payments[1:],
            ),
        }
    )

    with pytest.raises(sa.exc.IntegrityError):
        seed_demo.seed_database(fresh, broken, PASSWORD, hash_password=fast_hash)

    with engine.connect() as connection:
        for table in ("hotels", "bookings", "users", "amenities"):
            count = connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert count == 0, table


# --- the command line ----------------------------------------------------------------------------


def test_the_command_seeds_and_never_prints_the_password(
    fresh: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(seed_demo.PASSWORD_VARIABLE, PASSWORD)
    monkeypatch.setattr("app.core.security.hash_password", fast_hash)

    code = seed_demo.main(
        [
            "--database-url",
            fresh,
            "--reference-date",
            "2026-09-30",
            "--history-days",
            "60",
            "--future-days",
            "30",
        ]
    )

    out = capsys.readouterr()
    assert code == 0
    assert PASSWORD not in out.out + out.err
    target = sa.engine.make_url(fresh)
    if target.password:
        assert target.password not in out.out + out.err
    plan = build_plan(
        SeedConfig(reference_date=dt.date(2026, 9, 30), history_days=60, future_days=30)
    )
    assert f"database fingerprint : {fingerprint(logical_rows(plan))}" in out.out


def test_the_command_refuses_a_database_with_data(
    fresh: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(seed_demo.PASSWORD_VARIABLE, PASSWORD)
    seed_demo.seed_database(fresh, build_plan(CONFIG), PASSWORD, hash_password=fast_hash)

    code = seed_demo.main(["--database-url", fresh, "--reference-date", "2026-09-30"])

    assert code == 1
    assert "REFUSED" in capsys.readouterr().err


def test_the_command_refuses_without_a_password(
    fresh: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(seed_demo.PASSWORD_VARIABLE, raising=False)

    code = seed_demo.main(["--database-url", fresh, "--reference-date", "2026-09-30"])

    assert code == 1
    assert seed_demo.PASSWORD_VARIABLE in capsys.readouterr().err


# --- and the application serves it ---------------------------------------------------------------


def test_the_demo_owner_can_log_in_and_the_screens_have_something_to_show(
    fresh: str, engine: Engine
) -> None:
    """With the real Argon2 hash: the seeded account logs in, sees both hotels, and the
    dashboard's recent week, the default 90-day intelligence window and the 7-day forecast all
    have data to work with."""
    plan = build_plan(CONFIG)
    seed_demo.seed_database(fresh, plan, PASSWORD)
    client = TestClient(create_test_app(engine))
    login = client.post(
        "/api/v1/auth/login", json={"email": CONFIG.owner_email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

    hotels = client.get("/api/v1/hotels", params={"page": 1, "page_size": 50}).json()["items"]
    assert {h["slug"] for h in hotels} == {spec.slug for spec in seed_demo.HOTELS}

    reference = CONFIG.reference_date
    week = {"date_from": str(reference - dt.timedelta(days=6)), "date_to": str(reference)}
    quarter = {"date_from": str(reference - dt.timedelta(days=89)), "date_to": str(reference)}
    horizon = {
        "date_from": str(reference + dt.timedelta(days=1)),
        "date_to": str(reference + dt.timedelta(days=7)),
    }
    for hotel in hotels:
        base = f"/api/v1/hotels/{hotel['public_id']}"
        overview = client.get(f"{base}/analytics/overview", params=week).json()
        assert overview["occupancy"]["occupied_room_nights"] > 0
        assert Decimal(overview["occupancy"]["occupancy_rate"]) > 0
        assert overview["room_revenue"]

        trend = client.get(f"{base}/intelligence/demand-trend", params=quarter).json()
        assert trend["direction"] in {"increasing", "decreasing", "stable"}

        anomalies = client.get(f"{base}/intelligence/anomalies", params=quarter).json()
        assert anomalies["metrics_not_assessed"] == []

        forecast = client.get(f"{base}/intelligence/forecast/occupancy", params=horizon).json()
        assert all(point["predicted_room_nights"] is not None for point in forecast["points"])
        assert any(point["on_the_books_room_nights"] > 0 for point in forecast["points"])
