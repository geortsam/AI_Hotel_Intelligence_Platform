"""A guest's email address against real PostgreSQL (Issue H7).

Migration ``0020_guest_email_rules`` replaced 0001's ``uq_guests_hotel_id_email`` -- which
compared the text exactly, so ``Elena@x.test`` and ``elena@x.test`` could both be stored at one
hotel -- with ``uq_guests_hotel_id_lower_email`` on ``(hotel_id, lower(email))``, and added
``ck_guests_email_format``, the pattern ``users.email`` already had. The API checks and
lower-cases before anything is written; the database holds both rules on its own.

Everything here runs against real PostgreSQL: the API by real requests, the constraints by real
inserts that bypass the API, and 0020's own ``upgrade`` and ``downgrade`` inside a transaction
that is rolled back, so nothing they do survives the test.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.integration.conftest import REPO_ROOT, authenticated_client, requires_postgres
from tests.integration.test_guests_api import guest_payload, guests_url, hotel_payload

pytestmark = requires_postgres

SUITE_EMAIL = "guest-email-rules@example.test"
REVISION = "0020_guest_email_rules"
MIGRATION = REPO_ROOT / "database" / "migrations" / "versions" / f"20261009_{REVISION}.py"

NEW_INDEX = "uq_guests_hotel_id_lower_email"
OLD_INDEX = "uq_guests_hotel_id_email"
FORMAT_CHECK = "ck_guests_email_format"
#: Exactly as ``pg_get_indexdef`` / ``pg_get_constraintdef`` render each.
NEW_INDEX_DEFINITION = (
    f"CREATE UNIQUE INDEX {NEW_INDEX} ON public.guests USING btree "
    "(hotel_id, lower(email)) WHERE (email IS NOT NULL)"
)
OLD_INDEX_DEFINITION = (
    f"CREATE UNIQUE INDEX {OLD_INDEX} ON public.guests USING btree "
    "(hotel_id, email) WHERE (email IS NOT NULL)"
)
FORMAT_DEFINITION = r"CHECK (((email IS NULL) OR (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'::text)))"

ADDRESS = "elena.papadakis@example.test"


# --- setup ---------------------------------------------------------------------------------------


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.commit()


def new_hotel(api: TestClient, slug: str) -> str:
    response = api.post("/api/v1/hotels", json=hotel_payload(slug))
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def guest_count(api: TestClient, hotel: str) -> int:
    return int(api.get(guests_url(hotel)).json()["total"])


def raw_hotel(connection: sa.Connection, slug: str) -> int:
    return int(
        connection.execute(
            sa.text(
                "INSERT INTO hotels (name, slug, address_line1, city, country_code, timezone, "
                "currency) VALUES (:slug, :slug, '1 Test Street', 'Athens', 'GR', "
                "'Europe/Athens', 'EUR') RETURNING id"
            ),
            {"slug": slug},
        ).scalar_one()
    )


def raw_guest(connection: sa.Connection, hotel_id: int, email: str | None) -> str:
    """A guest written straight to the table -- the constraints, with no API between."""
    return str(
        connection.execute(
            sa.text(
                "INSERT INTO guests (hotel_id, first_name, last_name, email) "
                "VALUES (:hotel, 'Ada', 'Lovelace', :email) RETURNING public_id"
            ),
            {"hotel": hotel_id, "email": email},
        ).scalar_one()
    )


def violation(attempt: Any) -> tuple[str | None, str | None]:
    """(SQLSTATE, constraint) of the IntegrityError ``attempt()`` raises."""
    with pytest.raises(IntegrityError) as refused:
        attempt()
    diag = getattr(refused.value.orig, "diag", None)
    return (
        getattr(refused.value.orig, "sqlstate", None),
        getattr(diag, "constraint_name", None),
    )


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0020", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def migrating(engine: Engine) -> Iterator[sa.Connection]:
    """A connection inside a transaction that is always rolled back, with Alembic operations
    bound to it -- so 0020's own functions run against the real catalog and leave nothing."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
            with Operations.context(MigrationContext.configure(connection)):
                yield connection
        finally:
            transaction.rollback()


def guest_indexes(connection: sa.Connection) -> dict[str, str]:
    rows = connection.execute(
        sa.text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'guests'"
        )
    ).all()
    return {str(name): str(definition) for name, definition in rows}


def guest_checks(connection: sa.Connection) -> dict[str, str]:
    rows = connection.execute(
        sa.text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'guests'::regclass AND contype = 'c'"
        )
    ).all()
    return {str(name): str(definition) for name, definition in rows}


def schema_snapshot(connection: sa.Connection) -> dict[str, Any]:
    """Every table, column, constraint and index of the public schema, as PostgreSQL says."""
    return {
        "tables": connection.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        ).all(),
        "columns": connection.execute(
            sa.text(
                "SELECT table_name, column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema = 'public' "
                "ORDER BY table_name, ordinal_position"
            )
        ).all(),
        "constraints": {
            (str(table), str(name)): str(definition)
            for table, name, definition in connection.execute(
                sa.text(
                    "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(c.oid) "
                    "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
                    "WHERE n.nspname = 'public'"
                )
            ).all()
        },
        "indexes": {
            str(name): str(definition)
            for name, definition in connection.execute(
                sa.text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
            ).all()
        },
    }


ROWS = "SELECT public_id::text, hotel_id, email FROM guests ORDER BY id"


# --- the API -------------------------------------------------------------------------------------


def test_an_address_is_stored_lower_case(api: TestClient) -> None:
    hotel = new_hotel(api, "hotel-a")

    created = api.post(
        guests_url(hotel), json=guest_payload(email="  Elena.Papadakis@Example.TEST ")
    )

    assert created.status_code == 201, created.text
    assert created.json()["email"] == ADDRESS
    detail = f"{guests_url(hotel)}/{created.json()['public_id']}"
    assert api.get(detail).json()["email"] == ADDRESS


@pytest.mark.parametrize(
    "variant", ["Elena.Papadakis@example.test", "ELENA.PAPADAKIS@EXAMPLE.TEST"]
)
def test_one_hotel_cannot_hold_an_address_twice_in_any_case(api: TestClient, variant: str) -> None:
    hotel = new_hotel(api, "hotel-a")
    assert api.post(guests_url(hotel), json=guest_payload(email=ADDRESS)).status_code == 201

    refused = api.post(guests_url(hotel), json=guest_payload(last_name="Byron", email=variant))

    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == (
        "Another guest at this hotel already uses that email address."
    )
    assert ADDRESS not in refused.text.lower()
    assert guest_count(api, hotel) == 1


def test_an_update_onto_another_guests_address_in_another_case_is_refused(api: TestClient) -> None:
    hotel = new_hotel(api, "hotel-a")
    api.post(guests_url(hotel), json=guest_payload(email=ADDRESS))
    other = api.post(guests_url(hotel), json=guest_payload(last_name="Byron")).json()
    detail = f"{guests_url(hotel)}/{other['public_id']}"

    refused = api.patch(detail, json={"email": "Elena.Papadakis@EXAMPLE.test"})

    assert refused.status_code == 409
    assert api.get(detail).json()["email"] is None


def test_another_hotel_may_hold_the_same_address_in_any_case(api: TestClient) -> None:
    first = new_hotel(api, "hotel-a")
    second = new_hotel(api, "hotel-b")
    assert api.post(guests_url(first), json=guest_payload(email=ADDRESS)).status_code == 201

    response = api.post(
        guests_url(second), json=guest_payload(email="ELENA.Papadakis@example.TEST")
    )

    assert response.status_code == 201, response.text
    assert response.json()["email"] == ADDRESS


def test_any_number_of_guests_may_have_no_address(api: TestClient) -> None:
    hotel = new_hotel(api, "hotel-a")

    for name in ("Byron", "Babbage", "Somerville"):
        assert api.post(guests_url(hotel), json=guest_payload(last_name=name)).status_code == 201

    assert guest_count(api, hotel) == 3


def test_an_address_can_still_be_cleared_with_null(api: TestClient) -> None:
    hotel = new_hotel(api, "hotel-a")
    created = api.post(guests_url(hotel), json=guest_payload(email=ADDRESS)).json()
    detail = f"{guests_url(hotel)}/{created['public_id']}"

    response = api.patch(detail, json={"email": None})

    assert response.status_code == 200, response.text
    assert response.json()["email"] is None


@pytest.mark.parametrize(
    "address", ["not-an-email", "elena@example", "elena papadakis@example.test"]
)
def test_a_malformed_address_is_a_422_at_the_field_and_writes_nothing(
    api: TestClient, address: str
) -> None:
    hotel = new_hotel(api, "hotel-a")

    refused = api.post(guests_url(hotel), json=guest_payload(email=address))

    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert [detail["location"] for detail in error["details"]] == [["body", "email"]]
    assert address not in refused.text
    assert guest_count(api, hotel) == 0


# --- the database, with no API in between --------------------------------------------------------


def test_direct_sql_cannot_store_one_address_twice_in_another_case(engine: Engine) -> None:
    with engine.connect() as connection, connection.begin():
        hotel = raw_hotel(connection, "direct-a")
        raw_guest(connection, hotel, "Elena.Papadakis@Example.test")
        savepoint = connection.begin_nested()

        state, constraint = violation(lambda: raw_guest(connection, hotel, ADDRESS))
        savepoint.rollback()

        assert (state, constraint) == ("23505", NEW_INDEX)
        connection.rollback()


def test_direct_sql_cannot_store_a_malformed_address(engine: Engine) -> None:
    with engine.connect() as connection, connection.begin():
        hotel = raw_hotel(connection, "direct-a")

        state, constraint = violation(lambda: raw_guest(connection, hotel, "not-an-email"))

        assert (state, constraint) == ("23514", FORMAT_CHECK)
        connection.rollback()


def test_direct_sql_may_store_many_null_addresses_and_one_address_per_hotel(engine: Engine) -> None:
    with engine.connect() as connection, connection.begin():
        first = raw_hotel(connection, "direct-a")
        second = raw_hotel(connection, "direct-b")
        for _ in range(3):
            raw_guest(connection, first, None)
        raw_guest(connection, first, ADDRESS)
        raw_guest(connection, second, ADDRESS.upper())

        count = connection.execute(sa.text("SELECT count(*) FROM guests")).scalar_one()

        assert count == 5
        connection.rollback()


def test_the_index_and_the_check_are_exactly_the_approved_definitions(engine: Engine) -> None:
    with engine.connect() as connection:
        indexes = guest_indexes(connection)
        checks = guest_checks(connection)

    assert indexes[NEW_INDEX] == NEW_INDEX_DEFINITION
    assert OLD_INDEX not in indexes
    assert checks[FORMAT_CHECK] == FORMAT_DEFINITION


# --- migration 0020 itself -----------------------------------------------------------------------


def test_the_migration_is_the_one_on_disk() -> None:
    migration = load_migration()
    assert migration.revision == REVISION
    assert migration.down_revision == "0019_review_external_id_scope"


def test_0020_changes_only_the_guest_email_key_and_check(migrating: sa.Connection) -> None:
    """Down to 0019 and back up, the whole public schema compared each way: the only
    differences are the index swap and the CHECK."""
    migration = load_migration()
    at_head = schema_snapshot(migrating)

    migration.downgrade()
    at_0019 = schema_snapshot(migrating)
    migration.upgrade()
    again = schema_snapshot(migrating)

    assert again == at_head
    assert at_0019["tables"] == at_head["tables"]
    assert at_0019["columns"] == at_head["columns"]
    assert set(at_head["constraints"]) - set(at_0019["constraints"]) == {("guests", FORMAT_CHECK)}
    assert set(at_0019["constraints"]) <= set(at_head["constraints"])
    assert {k: v for k, v in at_head["constraints"].items() if k != ("guests", FORMAT_CHECK)} == (
        at_0019["constraints"]
    )
    assert set(at_0019["indexes"]) - set(at_head["indexes"]) == {OLD_INDEX}
    assert set(at_head["indexes"]) - set(at_0019["indexes"]) == {NEW_INDEX}
    assert at_0019["indexes"][OLD_INDEX] == OLD_INDEX_DEFINITION
    assert {k: v for k, v in at_0019["indexes"].items() if k != OLD_INDEX} == {
        k: v for k, v in at_head["indexes"].items() if k != NEW_INDEX
    }


def test_rows_written_under_0019_survive_the_upgrade_unchanged(migrating: sa.Connection) -> None:
    """No address is lower-cased or otherwise rewritten: a mixed-case address stored before
    0020 keeps its spelling, and the new key holds over it."""
    migration = load_migration()
    migration.downgrade()
    first = raw_hotel(migrating, "upgrade-a")
    second = raw_hotel(migrating, "upgrade-b")
    raw_guest(migrating, first, "Elena.Papadakis@Example.TEST")
    raw_guest(migrating, first, "ada@example.test")
    raw_guest(migrating, first, None)
    raw_guest(migrating, first, None)
    raw_guest(migrating, second, "elena.papadakis@example.test")
    before = migrating.execute(sa.text(ROWS)).all()

    migration.upgrade()

    assert migrating.execute(sa.text(ROWS)).all() == before
    assert guest_indexes(migrating)[NEW_INDEX] == NEW_INDEX_DEFINITION
    # And the key now refuses what the old one let through.
    savepoint = migrating.begin_nested()
    state, constraint = violation(lambda: raw_guest(migrating, first, ADDRESS))
    savepoint.rollback()
    assert (state, constraint) == ("23505", NEW_INDEX)


def test_the_downgrade_restores_0001s_index_and_keeps_every_row(migrating: sa.Connection) -> None:
    migration = load_migration()
    hotel = raw_hotel(migrating, "down-a")
    raw_guest(migrating, hotel, ADDRESS)
    raw_guest(migrating, hotel, None)
    before = migrating.execute(sa.text(ROWS)).all()

    migration.downgrade()

    assert migrating.execute(sa.text(ROWS)).all() == before
    indexes = guest_indexes(migrating)
    assert indexes[OLD_INDEX] == OLD_INDEX_DEFINITION
    assert NEW_INDEX not in indexes
    assert FORMAT_CHECK not in guest_checks(migrating)


def assert_nothing_applied(connection: sa.Connection) -> None:
    indexes = guest_indexes(connection)
    assert OLD_INDEX in indexes
    assert NEW_INDEX not in indexes
    assert FORMAT_CHECK not in guest_checks(connection)


def test_the_upgrade_refuses_case_duplicates_without_naming_the_address(
    migrating: sa.Connection,
) -> None:
    migration = load_migration()
    migration.downgrade()
    hotel = raw_hotel(migrating, "dup-hotel")
    first = raw_guest(migrating, hotel, "Elena.Papadakis@Example.test")
    second = raw_guest(migrating, hotel, ADDRESS)
    before = migrating.execute(sa.text(ROWS)).all()

    with pytest.raises(RuntimeError) as refused:
        migration.upgrade()

    message = str(refused.value)
    assert message.startswith("0020 refused: 1 group(s) of guests at one hotel share")
    assert "'dup-hotel'" in message and first in message and second in message
    assert "elena" not in message.lower()
    assert "nothing was changed" in message
    assert migrating.execute(sa.text(ROWS)).all() == before
    assert_nothing_applied(migrating)


def test_the_upgrade_refuses_malformed_addresses_without_naming_them(
    migrating: sa.Connection,
) -> None:
    migration = load_migration()
    migration.downgrade()
    hotel = raw_hotel(migrating, "bad-hotel")
    guest = raw_guest(migrating, hotel, "not-an-email")
    raw_guest(migrating, hotel, ADDRESS)
    before = migrating.execute(sa.text(ROWS)).all()

    with pytest.raises(RuntimeError) as refused:
        migration.upgrade()

    message = str(refused.value)
    assert message.startswith("0020 refused: 1 guest email address(es) do not match")
    assert "'bad-hotel'" in message and guest in message
    assert "not-an-email" not in message
    assert migrating.execute(sa.text(ROWS)).all() == before
    assert_nothing_applied(migrating)


def test_the_upgrade_reports_both_problems_at_once(migrating: sa.Connection) -> None:
    migration = load_migration()
    migration.downgrade()
    hotel = raw_hotel(migrating, "both-hotel")
    raw_guest(migrating, hotel, "Elena.Papadakis@Example.test")
    raw_guest(migrating, hotel, ADDRESS)
    raw_guest(migrating, hotel, "ada@example")

    with pytest.raises(RuntimeError) as refused:
        migration.upgrade()

    message = str(refused.value)
    assert "differs only by letter case" in message
    assert "do not match" in message
    assert_nothing_applied(migrating)
