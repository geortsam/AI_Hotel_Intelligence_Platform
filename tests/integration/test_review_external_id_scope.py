"""A review's platform reference is unique per hotel, not across the platform (Issue H4).

Migration 0001 declared ``uq_reviews_source_external_review_id`` on ``(source,
external_review_id)`` with no hotel column, so one property recording a platform's review
identifier refused every other property the same pair -- and told the second property that a
review it cannot see exists. Migration 0019 replaced it with

    uq_reviews_hotel_source_external_review_id
        ON reviews (hotel_id, source, external_review_id) WHERE external_review_id IS NOT NULL

and the service answers a refusal by it with its own code, ``DUPLICATE_EXTERNAL_REVIEW``.

Everything here runs against real PostgreSQL: the constraint is exercised by real inserts, the
API by real requests, and 0019's own ``upgrade`` and ``downgrade`` are run inside a transaction
that is rolled back, so nothing they do survives the test.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from collections.abc import Iterator
from decimal import Decimal
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Review
from app.services.review import (
    DUPLICATE_EXTERNAL_REVIEW,
    DUPLICATE_EXTERNAL_REVIEW_MESSAGE,
    EXTERNAL_ID_CONSTRAINT,
)
from tests.integration.conftest import (
    REPO_ROOT,
    authenticated_client,
    make_hotel,
    requires_postgres,
)
from tests.integration.test_reviews_api import (
    build_booking,
    review_payload,
    review_url,
    second_booking,
)

pytestmark = requires_postgres

SUITE_EMAIL = "external-id-scope@example.test"
HEAD = "0019_review_external_id_scope"
MIGRATION = REPO_ROOT / "database" / "migrations" / "versions" / f"20261007_{HEAD}.py"

TENANT_INDEX = "uq_reviews_hotel_source_external_review_id"
GLOBAL_INDEX = "uq_reviews_source_external_review_id"
#: Exactly as ``pg_get_indexdef`` renders each definition.
TENANT_DEFINITION = (
    f"CREATE UNIQUE INDEX {TENANT_INDEX} ON public.reviews USING btree "
    "(hotel_id, source, external_review_id) WHERE (external_review_id IS NOT NULL)"
)
#: What 0001 declared, and what 0019's downgrade must restore exactly.
GLOBAL_DEFINITION = (
    f"CREATE UNIQUE INDEX {GLOBAL_INDEX} ON public.reviews USING btree "
    "(source, external_review_id) WHERE (external_review_id IS NOT NULL)"
)

REFERENCE = "ta-123"
REVIEW_DATE = dt.date(2026, 9, 12)


# --- setup -----------------------------------------------------------------------------------


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.commit()


def add_review(session: Session, hotel_id: int, source: str, reference: str | None) -> None:
    """One review written straight to the table -- the constraint, with no service between."""
    session.add(
        Review(
            hotel_id=hotel_id,
            source=source,
            external_review_id=reference,
            rating=Decimal("4.00"),
            review_date=REVIEW_DATE,
        )
    )
    session.commit()


def count_reviews(session: Session) -> int:
    return int(session.scalar(sa.select(sa.func.count()).select_from(Review)) or 0)


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0019", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def review_indexes(connection: sa.Connection) -> dict[str, str]:
    rows = connection.execute(
        sa.text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'reviews' ORDER BY indexname"
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
                "SELECT table_name, column_name, data_type, is_nullable, column_default, "
                "is_generated, generation_expression FROM information_schema.columns "
                "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
            )
        ).all(),
        "constraints": connection.execute(
            sa.text(
                "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
                "WHERE n.nspname = 'public' ORDER BY 1, 2"
            )
        ).all(),
        "indexes": {
            str(name): str(definition)
            for name, definition in connection.execute(
                sa.text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                    "ORDER BY indexname"
                )
            ).all()
        },
    }


@pytest.fixture
def migrating(engine: Engine) -> Iterator[sa.Connection]:
    """A connection inside a transaction that is always rolled back, with Alembic operations
    bound to it -- so 0019's own functions run against the real catalog and leave nothing."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
            with Operations.context(MigrationContext.configure(connection)):
                yield connection
        finally:
            transaction.rollback()


def insert_raw(
    connection: sa.Connection, hotel_id: int, source: str, reference: str | None
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO reviews (hotel_id, source, external_review_id, rating, review_date) "
            "VALUES (:hotel, :source, :reference, 4, :day)"
        ),
        {"hotel": hotel_id, "source": source, "reference": reference, "day": REVIEW_DATE},
    )


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


# --- the constraint, enforced by PostgreSQL ------------------------------------------------------


def test_the_same_hotel_cannot_record_a_reference_twice_for_one_source(session: Session) -> None:
    hotel = make_hotel(session)
    add_review(session, hotel.id, "tripadvisor", REFERENCE)

    with pytest.raises(IntegrityError) as refused:
        add_review(session, hotel.id, "tripadvisor", REFERENCE)
    session.rollback()

    assert refused.value.orig is not None
    assert getattr(refused.value.orig, "sqlstate", None) == "23505"
    assert TENANT_INDEX in str(refused.value.orig)
    assert count_reviews(session) == 1


def test_different_hotels_may_record_the_same_reference(session: Session) -> None:
    first = make_hotel(session)
    second = make_hotel(session)
    add_review(session, first.id, "tripadvisor", REFERENCE)

    add_review(session, second.id, "tripadvisor", REFERENCE)

    assert count_reviews(session) == 2


def test_one_hotel_may_record_the_same_reference_from_another_source(session: Session) -> None:
    hotel = make_hotel(session)
    add_review(session, hotel.id, "tripadvisor", REFERENCE)

    add_review(session, hotel.id, "google", REFERENCE)

    assert count_reviews(session) == 2


def test_reviews_without_a_reference_are_unrestricted(session: Session) -> None:
    """WHERE external_review_id IS NOT NULL: any number of NULLs, at one hotel and one source,
    and across hotels."""
    hotel = make_hotel(session)
    other = make_hotel(session)
    for target in (hotel, hotel, hotel, other, other):
        add_review(session, target.id, "tripadvisor", None)

    assert count_reviews(session) == 5


@pytest.mark.parametrize("source", ["direct", "other"])
def test_direct_and_other_reviews_may_carry_a_reference(session: Session, source: str) -> None:
    """Issue H4 changes the key's scope, not which sources may carry an identifier."""
    hotel = make_hotel(session)
    add_review(session, hotel.id, source, REFERENCE)

    with pytest.raises(IntegrityError):
        add_review(session, hotel.id, source, REFERENCE)
    session.rollback()

    assert count_reviews(session) == 1


def test_the_index_is_exactly_the_approved_definition(engine: Engine) -> None:
    with engine.connect() as connection:
        indexes = review_indexes(connection)
        valid = connection.execute(
            sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = CAST(:i AS regclass)"),
            {"i": TENANT_INDEX},
        ).scalar_one()

    assert indexes[TENANT_INDEX] == TENANT_DEFINITION
    assert GLOBAL_INDEX not in indexes
    assert valid is True
    assert EXTERNAL_ID_CONSTRAINT == TENANT_INDEX


def test_the_database_is_at_0019(engine: Engine) -> None:
    with engine.connect() as connection:
        revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert revision == HEAD


# --- the API --------------------------------------------------------------------------------------


def test_the_api_refuses_a_hotels_own_duplicate_with_its_own_code(api: TestClient) -> None:
    hotel, first, guest = build_booking(api, "hotel-a")
    second = second_booking(api, hotel, guest)
    payload = review_payload(source="tripadvisor", external_review_id=REFERENCE)
    assert api.post(review_url(hotel, first), json=payload).status_code == 201

    response = api.post(review_url(hotel, second), json=payload)

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": DUPLICATE_EXTERNAL_REVIEW,
            "message": DUPLICATE_EXTERNAL_REVIEW_MESSAGE,
            "details": [],
        }
    }
    assert DUPLICATE_EXTERNAL_REVIEW == "DUPLICATE_EXTERNAL_REVIEW"


@pytest.mark.parametrize("source", ["direct", "other"])
def test_the_api_accepts_a_reference_on_direct_and_other_reviews(
    api: TestClient, source: str
) -> None:
    hotel, booking, _ = build_booking(api, "hotel-a")

    response = api.post(
        review_url(hotel, booking), json=review_payload(source=source, external_review_id=REFERENCE)
    )

    assert response.status_code == 201, response.text
    assert response.json()["source"] == source
    assert response.json()["external_review_id"] == REFERENCE


def test_tenants_are_isolated_both_ways(api: TestClient) -> None:
    """Hotel A cannot use a reference twice; Hotel B uses the same one independently, and is
    held to the same rule for its own reviews."""
    hotel_a, a_first, a_guest = build_booking(api, "hotel-a")
    a_second = second_booking(api, hotel_a, a_guest)
    hotel_b, b_first, b_guest = build_booking(api, "hotel-b")
    b_second = second_booking(api, hotel_b, b_guest)
    payload = review_payload(source="tripadvisor", external_review_id=REFERENCE)

    assert api.post(review_url(hotel_a, a_first), json=payload).status_code == 201
    refused_a = api.post(review_url(hotel_a, a_second), json=payload)
    assert api.post(review_url(hotel_b, b_first), json=payload).status_code == 201
    refused_b = api.post(review_url(hotel_b, b_second), json=payload)

    assert refused_a.status_code == refused_b.status_code == 409
    assert refused_a.json()["error"]["code"] == DUPLICATE_EXTERNAL_REVIEW
    assert refused_a.json() == refused_b.json()


def test_a_hotel_learns_nothing_about_another_from_a_reference(api: TestClient) -> None:
    """Hotel B holding the pair is invisible to Hotel A: A's first recording succeeds, and A's
    own duplicate is refused in words that name neither hotel, stay, guest nor reference."""
    hotel_b, booking_b, guest_b = build_booking(api, "hotel-b")
    payload = review_payload(
        source="tripadvisor", external_review_id=REFERENCE, reviewer_name="Hotel B's guest"
    )
    assert api.post(review_url(hotel_b, booking_b), json=payload).status_code == 201
    hotel_a, a_first, a_guest = build_booking(api, "hotel-a")
    a_second = second_booking(api, hotel_a, a_guest)

    first = api.post(review_url(hotel_a, a_first), json=payload)
    refused = api.post(review_url(hotel_a, a_second), json=payload)

    assert first.status_code == 201, first.text
    assert refused.status_code == 409
    for secret in [hotel_b, booking_b, guest_b, "hotel-b", "Hotel B", REFERENCE, a_first]:
        assert secret not in refused.text, f"leaked {secret!r}"


# --- migration 0019 itself ------------------------------------------------------------------------


def test_the_migration_is_the_one_on_disk() -> None:
    assert MIGRATION.is_file()
    migration = load_migration()
    assert migration.revision == HEAD
    assert migration.down_revision == "0018_composite_set_null_columns"


def test_0019_changes_only_the_external_id_index(migrating: sa.Connection) -> None:
    """Down to 0018 and back up, comparing the whole public schema each way. The one
    difference is the index swap; no table, column, constraint, key or other index moves."""
    migration = load_migration()
    at_head = schema_snapshot(migrating)

    migration.downgrade()
    at_0018 = schema_snapshot(migrating)
    migration.upgrade()
    again = schema_snapshot(migrating)

    assert again == at_head
    for part in ("tables", "columns", "constraints"):
        assert at_0018[part] == at_head[part], part
    removed = set(at_0018["indexes"]) - set(at_head["indexes"])
    added = set(at_head["indexes"]) - set(at_0018["indexes"])
    assert removed == {GLOBAL_INDEX}
    assert added == {TENANT_INDEX}
    assert {k: v for k, v in at_0018["indexes"].items() if k != GLOBAL_INDEX} == {
        k: v for k, v in at_head["indexes"].items() if k != TENANT_INDEX
    }


def test_the_downgrade_restores_0001s_index_and_the_upgrade_reapplies_0019(
    migrating: sa.Connection,
) -> None:
    migration = load_migration()

    migration.downgrade()
    downgraded = review_indexes(migrating)
    migration.upgrade()
    upgraded = review_indexes(migrating)

    assert downgraded[GLOBAL_INDEX] == GLOBAL_DEFINITION
    assert TENANT_INDEX not in downgraded
    assert upgraded[TENANT_INDEX] == TENANT_DEFINITION
    assert GLOBAL_INDEX not in upgraded


def test_rows_written_under_0018_survive_the_upgrade(migrating: sa.Connection) -> None:
    """The 0018 -> 0019 step over data: every row the global key allowed is kept, unchanged,
    and the new key is valid over it."""
    migration = load_migration()
    migration.downgrade()
    first = raw_hotel(migrating, "upgrade-a")
    second = raw_hotel(migrating, "upgrade-b")
    for hotel, source, reference in [
        (first, "tripadvisor", "ta-1"),
        (first, "google", "ta-1"),
        (first, "direct", "d-1"),
        (first, "other", "o-1"),
        (first, "tripadvisor", None),
        (first, "tripadvisor", None),
        (second, "tripadvisor", "ta-2"),
        (second, "google", None),
    ]:
        insert_raw(migrating, hotel, source, reference)
    rows = "SELECT id, hotel_id, source, external_review_id FROM reviews ORDER BY id"
    before = migrating.execute(sa.text(rows)).all()

    migration.upgrade()

    assert migrating.execute(sa.text(rows)).all() == before
    assert len(before) == 8
    assert review_indexes(migrating)[TENANT_INDEX] == TENANT_DEFINITION
    # And the upgraded key now admits what the old one refused.
    insert_raw(migrating, second, "tripadvisor", "ta-1")


def test_the_upgrade_refuses_rows_the_new_key_cannot_hold(migrating: sa.Connection) -> None:
    """Unreachable through 0018's own key, so the key is dropped here to build the case: the
    pre-check stops with a sentence, and nothing is half-applied."""
    migration = load_migration()
    migration.downgrade()
    migrating.execute(sa.text(f"DROP INDEX {GLOBAL_INDEX}"))
    hotel = raw_hotel(migrating, "upgrade-dup")
    insert_raw(migrating, hotel, "tripadvisor", "ta-1")
    insert_raw(migrating, hotel, "tripadvisor", "ta-1")

    with pytest.raises(RuntimeError, match="0019 refused: 1 "):
        migration.upgrade()
    assert TENANT_INDEX not in review_indexes(migrating)


def test_the_downgrade_refuses_a_pair_two_hotels_hold(migrating: sa.Connection) -> None:
    """0001's global key cannot hold what 0019 allows; the downgrade says so instead of
    failing half-way."""
    migration = load_migration()
    for slug in ("down-a", "down-b"):
        insert_raw(migrating, raw_hotel(migrating, slug), "tripadvisor", "ta-1")

    with pytest.raises(RuntimeError, match="0019 downgrade refused: 1 "):
        migration.downgrade()
    indexes = review_indexes(migrating)
    assert indexes[TENANT_INDEX] == TENANT_DEFINITION
    assert GLOBAL_INDEX not in indexes
