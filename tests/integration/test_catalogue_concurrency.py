"""R3.3: ``If-Match`` on the three global catalogues, and migration 0022, on PostgreSQL.

Issue H6 made every form-backed hotel PATCH conditional on the ``updated_at`` the client loaded;
the catalogues had no such column, so their PATCHes stayed last-write-wins. Migration 0022 gave
``amenities``, ``revenue_categories`` and ``expense_categories`` an ``updated_at`` maintained by
the shared ``set_updated_at`` trigger, and their PATCHes now take the same optional precondition:
a stale version is refused with 412 ``STALE_UPDATE``, nothing written and nothing audited;
without the header an update behaves exactly as before.

Administrator A and administrator B are two platform administrators, each with their own
session. The last section runs migration 0022's own upgrade and downgrade inside a rolled-back
transaction.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.preconditions import entity_tag
from tests.integration.conftest import (
    authenticated_client,
    grant_platform_admin,
    requires_postgres,
    seed_amenity,
    seed_expense_category,
    seed_revenue_category,
)
from tests.integration.test_optimistic_concurrency import (
    MALFORMED,
    assert_stale,
    tag,
    wait_for_a_lock_wait,
)

pytestmark = requires_postgres

A_EMAIL = "catalogue-a@example.test"
B_EMAIL = "catalogue-b@example.test"

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "database"
    / "migrations"
    / "versions"
    / "20261120_0022_catalogue_versions.py"
)
TABLES = ("amenities", "revenue_categories", "expense_categories")


def _seed_amenity(engine: Engine) -> None:
    seed_amenity(engine, "WIFI", "Wi-Fi", "Technology")


def _seed_revenue(engine: Engine) -> None:
    seed_revenue_category(engine, "SPA", "Spa")


def _seed_expense(engine: Engine) -> None:
    seed_expense_category(engine, "UTIL", "Utilities")


#: name -> (url, table, seed, B's edit, A's edit). The two touch different fields: the case a
#: diff-only form already protected, and which the precondition now protects too.
RESOURCES: dict[str, tuple[str, str, Callable[[Engine], None], dict[str, Any], dict[str, Any]]] = {
    "amenity": (
        "/api/v1/amenities/WIFI",
        "amenities",
        _seed_amenity,
        {"category": "Written by B"},
        {"name": "Renamed by A"},
    ),
    "revenue-category": (
        "/api/v1/revenue-categories/SPA",
        "revenue_categories",
        _seed_revenue,
        {"is_room_revenue": True},
        {"name": "Renamed by A"},
    ),
    "expense-category": (
        "/api/v1/expense-categories/UTIL",
        "expense_categories",
        _seed_expense,
        {"is_fixed_cost": True},
        {"name": "Renamed by A"},
    ),
}

ALL = pytest.mark.parametrize("name", sorted(RESOURCES))


# --- setup ----------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean(session: Session) -> Iterator[None]:
    """The suite's own teardown after every test -- catalogues, users, grants and audit events
    truncated -- so every entry and event a test sees is its own."""
    yield


def administrator(engine: Engine, email: str) -> TestClient:
    client = authenticated_client(engine, email=email)
    grant_platform_admin(engine, email)
    return client


@pytest.fixture
def api(engine: Engine) -> TestClient:
    """Administrator A."""
    return administrator(engine, A_EMAIL)


@pytest.fixture
def b(engine: Engine) -> TestClient:
    """Administrator B, with their own session."""
    return administrator(engine, B_EMAIL)


def entry(engine: Engine, name: str) -> str:
    """Seed the entry this resource edits; return its URL."""
    url, _, seed, _, _ = RESOURCES[name]
    seed(engine)
    return url


def updates_recorded(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                sa.text("SELECT count(*) FROM audit_events WHERE action LIKE '%.updated'")
            )
            or 0
        )


# --- a current version applies; a stale one writes nothing ----------------------------------------


@ALL
def test_a_current_version_applies_the_update(api: TestClient, engine: Engine, name: str) -> None:
    url = entry(engine, name)
    loaded = api.get(url).json()
    edit = RESOURCES[name][4]

    response = api.patch(url, json=edit, headers={"If-Match": tag(loaded)})

    assert response.status_code == 200, response.text
    for field, value in edit.items():
        assert response.json()[field] == value
    assert response.json()["updated_at"] != loaded["updated_at"]
    assert api.get(url).json() == response.json()


@ALL
def test_a_stale_version_is_refused_and_nothing_is_written_or_audited(
    api: TestClient, b: TestClient, engine: Engine, name: str
) -> None:
    """A loads; B saves; A saves from what it loaded. Refused, the entry is exactly what B left
    -- ``updated_at`` included -- and no second update event is recorded."""
    url = entry(engine, name)
    _, _, _, b_edit, a_edit = RESOURCES[name]

    loaded_by_a = api.get(url).json()
    after_b = b.patch(url, json=b_edit)
    assert after_b.status_code == 200, after_b.text
    assert updates_recorded(engine) == 1

    refused = api.patch(url, json=a_edit, headers={"If-Match": tag(loaded_by_a)})

    assert_stale(refused)
    assert api.get(url).json() == after_b.json()
    assert updates_recorded(engine) == 1


@ALL
def test_after_a_refusal_a_reload_and_resubmit_applies(
    api: TestClient, b: TestClient, engine: Engine, name: str
) -> None:
    """Nothing is left locked or half-done by a 412: the client reloads and tries again."""
    url = entry(engine, name)
    _, _, _, b_edit, a_edit = RESOURCES[name]
    stale = api.get(url).json()
    assert b.patch(url, json=b_edit).status_code == 200
    assert_stale(api.patch(url, json=a_edit, headers={"If-Match": tag(stale)}))

    reloaded = api.get(url).json()
    response = api.patch(url, json=a_edit, headers={"If-Match": tag(reloaded)})

    assert response.status_code == 200, response.text
    for field, value in {**b_edit, **a_edit}.items():
        assert response.json()[field] == value


# --- without the header, nothing changed ----------------------------------------------------------


@ALL
def test_without_the_header_an_update_behaves_as_before(
    api: TestClient, b: TestClient, engine: Engine, name: str
) -> None:
    """Backward compatible: no precondition, no refusal, and B's field survives A's write."""
    url = entry(engine, name)
    _, _, _, b_edit, a_edit = RESOURCES[name]
    api.get(url)
    assert b.patch(url, json=b_edit).status_code == 200

    response = api.patch(url, json=a_edit)

    assert response.status_code == 200, response.text
    for field, value in {**b_edit, **a_edit}.items():
        assert response.json()[field] == value


@ALL
def test_a_wildcard_constrains_nothing(
    api: TestClient, b: TestClient, engine: Engine, name: str
) -> None:
    url = entry(engine, name)
    assert b.patch(url, json=RESOURCES[name][3]).status_code == 200

    response = api.patch(url, json=RESOURCES[name][4], headers={"If-Match": "*"})

    assert response.status_code == 200, response.text


@ALL
@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_precondition_is_a_422_and_writes_nothing(
    api: TestClient, engine: Engine, name: str, value: str
) -> None:
    url = entry(engine, name)
    before = api.get(url).json()

    response = api.patch(url, json=RESOURCES[name][4], headers={"If-Match": value})

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert [detail["location"] for detail in error["details"]] == [["header", "If-Match"]]
    assert value not in response.text
    assert api.get(url).json() == before


# --- the token itself -----------------------------------------------------------------------------


@ALL
def test_any_rendering_of_the_same_instant_matches(
    api: TestClient, engine: Engine, name: str
) -> None:
    url = entry(engine, name)
    loaded = api.get(url).json()
    canonical = entity_tag(dt.datetime.fromisoformat(loaded["updated_at"]))

    response = api.patch(url, json=RESOURCES[name][4], headers={"If-Match": canonical})

    assert response.status_code == 200, response.text


@ALL
def test_one_microsecond_off_is_stale(api: TestClient, engine: Engine, name: str) -> None:
    url = entry(engine, name)
    loaded = api.get(url).json()
    instant = dt.datetime.fromisoformat(loaded["updated_at"])

    for nudged in (instant + dt.timedelta(microseconds=1), instant - dt.timedelta(microseconds=1)):
        assert_stale(
            api.patch(url, json=RESOURCES[name][4], headers={"If-Match": entity_tag(nudged)})
        )
    assert api.get(url).json() == loaded


@ALL
def test_an_empty_update_is_still_checked(
    api: TestClient, b: TestClient, engine: Engine, name: str
) -> None:
    """The precondition is judged before the empty-update shortcut."""
    url = entry(engine, name)
    stale = api.get(url).json()
    assert b.patch(url, json=RESOURCES[name][3]).status_code == 200
    current = api.get(url).json()

    assert_stale(api.patch(url, json={}, headers={"If-Match": tag(stale)}))
    response = api.patch(url, json={}, headers={"If-Match": tag(current)})

    assert response.status_code == 200, response.text
    assert response.json() == current
    assert api.get(url).json() == current


# --- the comparison runs under the row lock -------------------------------------------------------


@ALL
def test_a_writer_that_commits_while_the_update_waits_is_seen_not_overwritten(
    api: TestClient, engine: Engine, name: str
) -> None:
    """Another transaction holds the entry's row. A's conditional update -- carrying the version
    current *at this moment* -- must wait for it, then compare against what it committed."""
    url, table, seed, _, a_edit = RESOURCES[name]
    seed(engine)
    code = url.rsplit("/", 1)[1]
    token = tag(api.get(url).json())
    outcome: dict[str, Any] = {}

    def submit() -> None:
        try:
            outcome["response"] = api.patch(url, json=a_edit, headers={"If-Match": token})
        except Exception as failure:
            outcome["failure"] = repr(failure)

    with engine.connect() as other:
        transaction = other.begin()
        other.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
        other.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE code = :c FOR UPDATE"),
            {"c": code},
        )
        worker = threading.Thread(target=submit)
        worker.start()
        wait_for_a_lock_wait(engine, worker, outcome)
        other.execute(
            sa.text(f"UPDATE {table} SET name = 'Written meanwhile' WHERE code = :c"),
            {"c": code},
        )
        transaction.commit()

    worker.join(timeout=30)
    assert not worker.is_alive(), "the conditional update never finished"
    assert_stale(outcome["response"])
    assert api.get(url).json()["name"] == "Written meanwhile"


# --- migration 0022 -------------------------------------------------------------------------------


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0022", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def migrating(engine: Engine) -> Iterator[sa.Connection]:
    """A connection inside a transaction that is always rolled back, with Alembic operations
    bound to it -- so 0022's own functions run against the real catalog and leave nothing."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
            with Operations.context(MigrationContext.configure(connection)):
                yield connection
        finally:
            transaction.rollback()


def versioned(connection: sa.Connection, table: str) -> tuple[bool, bool]:
    """(the column exists, its trigger exists) for one catalogue."""
    column = connection.scalar(
        sa.text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'updated_at'"
        ),
        {"t": table},
    )
    trigger = connection.scalar(
        sa.text("SELECT count(*) FROM pg_trigger WHERE tgname = :name AND NOT tgisinternal"),
        {"name": f"trg_{table}_set_updated_at"},
    )
    return bool(column), bool(trigger)


def test_the_migration_is_the_one_on_disk() -> None:
    migration = load_migration()
    assert migration.revision == "0022_catalogue_versions"
    assert migration.down_revision == "0021_audit_platform_grants"
    assert migration._TABLES == TABLES


@pytest.mark.parametrize("table", TABLES)
def test_the_column_is_required_and_defaults_to_now(engine: Engine, table: str) -> None:
    with engine.connect() as connection:
        nullable, default, kind = connection.execute(
            sa.text(
                "SELECT is_nullable, column_default, data_type FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'updated_at'"
            ),
            {"t": table},
        ).one()
    assert nullable == "NO"
    assert default == "now()"
    assert kind == "timestamp with time zone"


@ALL
def test_the_trigger_moves_the_version_on_any_write(engine: Engine, name: str) -> None:
    """The database, not the application, maintains the version: a write by plain SQL moves it."""
    url, table, seed, _, _ = RESOURCES[name]
    seed(engine)
    code = url.rsplit("/", 1)[1]
    select = sa.text(f"SELECT updated_at FROM {table} WHERE code = :c")
    with engine.begin() as connection:
        before = connection.scalar(select, {"c": code})
    with engine.begin() as connection:
        connection.execute(
            sa.text(f"UPDATE {table} SET name = name || '.' WHERE code = :c"),
            {"c": code},
        )
    with engine.begin() as connection:
        after = connection.scalar(select, {"c": code})

    assert after > before


def test_the_downgrade_removes_only_the_versions_and_the_upgrade_fills_existing_rows(
    engine: Engine, migrating: sa.Connection
) -> None:
    """Down: each column and trigger is gone, the rows stay. Up again: each comes back, and a
    row that existed before the upgrade receives a version."""
    for name in RESOURCES:
        RESOURCES[name][2](engine)
    migration = load_migration()
    assert all(versioned(migrating, table) == (True, True) for table in TABLES)

    migration.downgrade()
    assert all(versioned(migrating, table) == (False, False) for table in TABLES)
    migrating.execute(sa.text("INSERT INTO amenities (code, name) VALUES ('BEFORE', 'Before')"))
    counts = {table: migrating.scalar(sa.text(f"SELECT count(*) FROM {table}")) for table in TABLES}

    migration.upgrade()

    assert all(versioned(migrating, table) == (True, True) for table in TABLES)
    for table in TABLES:
        assert migrating.scalar(sa.text(f"SELECT count(*) FROM {table}")) == counts[table]
        assert (
            migrating.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE updated_at IS NULL")) == 0
        )
    assert migrating.scalar(sa.text("SELECT updated_at FROM amenities WHERE code = 'BEFORE'"))
