"""A review or a revenue line outlives the booking or guest it pointed to (Issue H3).

Migration 0001 declared three composite foreign keys ``ON DELETE SET NULL`` without a column
list. PostgreSQL then nulls EVERY referencing column, ``hotel_id`` is NOT NULL, and the parent's
delete failed with 23502 -- an undeclared RESTRICT. Migration 0018 recreated them as

* ``revenue(booking_id, hotel_id) -> bookings``   ``ON DELETE SET NULL (booking_id)``
* ``reviews(booking_id, hotel_id) -> bookings``   ``ON DELETE SET NULL (booking_id)``
* ``reviews(guest_id, hotel_id)   -> guests``     ``ON DELETE SET NULL (guest_id)``

so deleting the parent keeps the child, keeps its hotel and nulls only the optional reference.
Every test here runs the real PostgreSQL DELETE and reads the rows it left. ``payments`` still
RESTRICTs a booking's deletion and ``bookings`` still RESTRICTs a guest's, and both are pinned
below beside the catalog itself.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.main import create_app
from app.models.enums import AuditAction
from app.services.audit import AuditTrail
from tests.integration.conftest import REPO_ROOT, authenticated_client, requires_postgres
from tests.integration.test_finance_api import (
    LEDGER_DATE,
    hotel_payload,
    make_booking,
    make_categories,
    revenue_payload,
    revenue_url,
)

pytestmark = requires_postgres

SUITE_EMAIL = "set-null@example.test"
#: The revision this suite is about. No longer the head: Issues H4 and H7 added 0019 and
#: 0020 on top of it.
REVISION = "0018_composite_set_null_columns"
HEAD = "0020_guest_email_rules"
MIGRATION = REPO_ROOT / "database" / "migrations" / "versions" / f"20261006_{REVISION}.py"
RANGE = {"date_from": "2026-09-01", "date_to": "2026-09-30"}
REVIEW = {"rating": "4.50", "review_date": "2026-09-05", "title": "Kept", "body": "Quiet room."}

#: Every foreign key at head -- (columns, referenced, actions) -- exactly as PostgreSQL renders it
#: through ``pg_get_constraintdef``. 38; three changed by 0018.
FOREIGN_KEY_PARTS = {
    "fk_audit_events_actor_user_id_users": ("actor_user_id", "users(id)", "ON DELETE RESTRICT"),
    "fk_audit_events_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_booking_rooms_booking_stay_status_bookings": (
        "booking_id, check_in_date, check_out_date, booking_status",
        "bookings(id, check_in_date, check_out_date, status)",
        "ON UPDATE CASCADE ON DELETE CASCADE",
    ),
    "fk_booking_rooms_room_id_hotel_id_rooms": (
        "room_id, hotel_id",
        "rooms(id, hotel_id)",
        "ON DELETE RESTRICT",
    ),
    "fk_bookings_guest_id_hotel_id_guests": (
        "guest_id, hotel_id",
        "guests(id, hotel_id)",
        "ON DELETE RESTRICT",
    ),
    "fk_bookings_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_brn_booking_room_id_hotel_id_booking_rooms": (
        "booking_room_id, hotel_id",
        "booking_rooms(id, hotel_id)",
        "ON DELETE CASCADE",
    ),
    "fk_brn_booking_room_stay_booking_rooms": (
        "booking_room_id, check_in_date, check_out_date",
        "booking_rooms(id, check_in_date, check_out_date)",
        "ON UPDATE CASCADE ON DELETE CASCADE",
    ),
    "fk_copilot_conversations_actor_user_id_users": (
        "actor_user_id",
        "users(id)",
        "ON DELETE RESTRICT",
    ),
    "fk_copilot_conversations_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_copilot_messages_conversation_id_hotel_id": (
        "conversation_id, hotel_id",
        "copilot_conversations(id, hotel_id)",
        "ON DELETE CASCADE",
    ),
    "fk_daily_hotel_metrics_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE CASCADE"),
    "fk_demand_observation_periods_hotel_id_hotels": (
        "hotel_id",
        "hotels(id)",
        "ON DELETE CASCADE",
    ),
    "fk_demand_predictions_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_expenses_category_id_expense_categories": (
        "category_id",
        "expense_categories(id)",
        "ON DELETE RESTRICT",
    ),
    "fk_expenses_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_guests_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_hotel_document_chunks_document_id_hotel_documents": (
        "document_id",
        "hotel_documents(id)",
        "ON DELETE RESTRICT",
    ),
    "fk_hotel_documents_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_hotel_documents_supersedes_id_hotel_id_hotel_documents": (
        "supersedes_id, hotel_id",
        "hotel_documents(id, hotel_id)",
        "ON DELETE RESTRICT",
    ),
    "fk_llm_invocations_actor_user_id_users": ("actor_user_id", "users(id)", "ON DELETE RESTRICT"),
    "fk_llm_invocations_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_payments_booking_id_hotel_id_bookings": (
        "booking_id, hotel_id",
        "bookings(id, hotel_id)",
        "ON DELETE RESTRICT",
    ),
    "fk_payments_refunded_payment_id_payments": (
        "refunded_payment_id",
        "payments(id)",
        "ON DELETE RESTRICT",
    ),
    "fk_platform_admins_user_id_users": ("user_id", "users(id)", "ON DELETE CASCADE"),
    "fk_revenue_booking_id_hotel_id_bookings": (
        "booking_id, hotel_id",
        "bookings(id, hotel_id)",
        "ON DELETE SET NULL (booking_id)",
    ),
    "fk_revenue_category_id_revenue_categories": (
        "category_id",
        "revenue_categories(id)",
        "ON DELETE RESTRICT",
    ),
    "fk_revenue_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_reviews_booking_id_hotel_id_bookings": (
        "booking_id, hotel_id",
        "bookings(id, hotel_id)",
        "ON DELETE SET NULL (booking_id)",
    ),
    "fk_reviews_guest_id_hotel_id_guests": (
        "guest_id, hotel_id",
        "guests(id, hotel_id)",
        "ON DELETE SET NULL (guest_id)",
    ),
    "fk_reviews_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE CASCADE"),
    "fk_room_type_amenities_amenity_id_amenities": (
        "amenity_id",
        "amenities(id)",
        "ON DELETE RESTRICT",
    ),
    "fk_room_type_amenities_room_type_id_room_types": (
        "room_type_id",
        "room_types(id)",
        "ON DELETE CASCADE",
    ),
    "fk_room_types_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_rooms_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_rooms_room_type_id_hotel_id_room_types": (
        "room_type_id, hotel_id",
        "room_types(id, hotel_id)",
        "ON DELETE RESTRICT",
    ),
    "fk_user_hotels_hotel_id_hotels": ("hotel_id", "hotels(id)", "ON DELETE RESTRICT"),
    "fk_user_hotels_user_id_users": ("user_id", "users(id)", "ON DELETE CASCADE"),
}
FOREIGN_KEYS = {
    name: f"FOREIGN KEY ({columns}) REFERENCES {referenced} {actions}"
    for name, (columns, referenced, actions) in FOREIGN_KEY_PARTS.items()
}
CHANGED_BY_0018 = {
    "fk_revenue_booking_id_hotel_id_bookings",
    "fk_reviews_booking_id_hotel_id_bookings",
    "fk_reviews_guest_id_hotel_id_guests",
}
#: What 0001 declared for those three, and what 0018's downgrade must restore exactly.
BEFORE_0018 = {
    "fk_revenue_booking_id_hotel_id_bookings": "FOREIGN KEY (booking_id, hotel_id) REFERENCES "
    "bookings(id, hotel_id) ON DELETE SET NULL",
    "fk_reviews_booking_id_hotel_id_bookings": "FOREIGN KEY (booking_id, hotel_id) REFERENCES "
    "bookings(id, hotel_id) ON DELETE SET NULL",
    "fk_reviews_guest_id_hotel_id_guests": "FOREIGN KEY (guest_id, hotel_id) REFERENCES "
    "guests(id, hotel_id) ON DELETE SET NULL",
}


# --- setup -----------------------------------------------------------------------------------


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


@pytest.fixture
def hotel(api: TestClient, engine: Engine) -> str:
    make_categories(engine)
    return str(api.post("/api/v1/hotels", json=hotel_payload("set-null")).json()["public_id"])


@pytest.fixture
def booking(api: TestClient, hotel: str) -> str:
    return make_booking(api, hotel)


def booking_url(hotel: str, booking: str) -> str:
    return f"/api/v1/hotels/{hotel}/bookings/{booking}"


def guest_of(api: TestClient, hotel: str, booking: str) -> str:
    return str(api.get(booking_url(hotel, booking)).json()["guest_public_id"])


def review(api: TestClient, hotel: str, booking: str) -> None:
    response = api.post(f"{booking_url(hotel, booking)}/review", json=REVIEW)
    assert response.status_code == 201, response.text


def revenue(api: TestClient, hotel: str, booking: str) -> None:
    response = api.post(revenue_url(hotel), json=revenue_payload(booking_public_id=booking))
    assert response.status_code == 201, response.text


def charge(api: TestClient, hotel: str, booking: str) -> None:
    response = api.post(
        f"{booking_url(hotel, booking)}/payments",
        json={"amount": "100.00", "currency": "EUR", "method": "card"},
    )
    assert response.status_code == 201, response.text


def row(engine: Engine, table: str) -> dict[str, Any]:
    """The one row of *table*, read straight from PostgreSQL."""
    with engine.connect() as connection:
        found = connection.execute(sa.text(f"SELECT * FROM {table}")).mappings().all()
    assert len(found) == 1, found
    return dict(found[0])


def count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())


def without(record: dict[str, Any], *columns: str) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {*columns, "updated_at"}}


def reviews_listed(api: TestClient, hotel: str) -> list[dict[str, Any]]:
    response = api.get(f"/api/v1/hotels/{hotel}/reviews")
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def overview(api: TestClient, hotel: str) -> dict[str, Any]:
    response = api.get(f"/api/v1/hotels/{hotel}/analytics/overview", params=RANGE)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- booking -> review ------------------------------------------------------------------------


def test_deleting_a_booking_detaches_its_review(
    api: TestClient, hotel: str, booking: str, engine: Engine
) -> None:
    guest = guest_of(api, hotel, booking)
    review(api, hotel, booking)
    before = row(engine, "reviews")
    assert before["booking_id"] is not None

    response = api.delete(booking_url(hotel, booking))

    assert response.status_code == 204, response.text
    assert count(engine, "bookings") == 0
    after = row(engine, "reviews")
    assert after["booking_id"] is None
    assert after["hotel_id"] == before["hotel_id"]
    assert without(after, "booking_id") == without(before, "booking_id")
    [listed] = reviews_listed(api, hotel)
    assert listed["booking_public_id"] is None
    assert listed["guest_public_id"] == guest
    assert (listed["title"], listed["body"], listed["rating"]) == ("Kept", "Quiet room.", "4.50")


def test_a_detached_review_has_no_booking_route_left(
    api: TestClient, hotel: str, booking: str
) -> None:
    """Moderation goes through ``/bookings/{b}/review``; with the booking gone there is no URL,
    so the review is read-only -- as an external review always was. (The review card shows
    no moderation for it: ``reviews.test.tsx``, "offers no moderation for a review with no
    stay, and says why".)"""
    review(api, hotel, booking)
    assert api.delete(booking_url(hotel, booking)).status_code == 204

    assert api.get(f"{booking_url(hotel, booking)}/review").status_code == 404
    hidden = api.patch(f"{booking_url(hotel, booking)}/review", json={"is_published": False})
    assert hidden.status_code == 404
    assert reviews_listed(api, hotel)[0]["is_published"] is True


# --- booking -> revenue -----------------------------------------------------------------------


def test_deleting_a_booking_detaches_its_revenue(
    api: TestClient, hotel: str, booking: str, engine: Engine
) -> None:
    revenue(api, hotel, booking)
    before = row(engine, "revenue")
    totals = overview(api, hotel)["other_revenue"]

    response = api.delete(booking_url(hotel, booking))

    assert response.status_code == 204, response.text
    assert count(engine, "bookings") == 0
    after = row(engine, "revenue")
    assert after["booking_id"] is None
    assert after["hotel_id"] == before["hotel_id"]
    assert without(after, "booking_id") == without(before, "booking_id")
    assert after["amount"] == Decimal("120.00") and after["revenue_date"] == LEDGER_DATE
    [listed] = api.get(revenue_url(hotel)).json()["items"]
    assert listed["booking_public_id"] is None
    assert (listed["amount"], listed["category_code"]) == ("120.00", "FB")
    assert overview(api, hotel)["other_revenue"] == totals


# --- guest -> review --------------------------------------------------------------------------


def test_deleting_a_guest_detaches_their_review(
    api: TestClient, hotel: str, booking: str, engine: Engine
) -> None:
    """Through the API alone: the reservation is deleted first -- bookings RESTRICT the guest --
    and then the guest, leaving a review that remembers neither."""
    guest = guest_of(api, hotel, booking)
    review(api, hotel, booking)
    assert api.delete(booking_url(hotel, booking)).status_code == 204
    before = row(engine, "reviews")
    assert before["guest_id"] is not None

    response = api.delete(f"/api/v1/hotels/{hotel}/guests/{guest}")

    assert response.status_code == 204, response.text
    assert count(engine, "guests") == 0
    after = row(engine, "reviews")
    assert (after["guest_id"], after["booking_id"]) == (None, None)
    assert after["hotel_id"] == before["hotel_id"]
    assert without(after, "guest_id") == without(before, "guest_id")
    [listed] = reviews_listed(api, hotel)
    assert (listed["guest_public_id"], listed["booking_public_id"]) == (None, None)
    assert listed["title"] == "Kept"


def test_the_guest_key_detaches_on_its_own_in_the_database(hotel: str, engine: Engine) -> None:
    """The guest key alone, at the database: a review with an author and no stay."""
    with engine.begin() as connection:
        hotel_id = connection.execute(sa.text("SELECT id FROM hotels")).scalar_one()
        guest_id = connection.execute(
            sa.text(
                "INSERT INTO guests (hotel_id, first_name, last_name) "
                "VALUES (:h, 'Ada', 'Lovelace') RETURNING id"
            ),
            {"h": hotel_id},
        ).scalar_one()
        connection.execute(
            sa.text(
                "INSERT INTO reviews (hotel_id, guest_id, source, rating, rating_scale, "
                "review_date) VALUES (:h, :g, 'google', 4, 5, '2026-09-05')"
            ),
            {"h": hotel_id, "g": guest_id},
        )
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM guests WHERE id = :g"), {"g": guest_id})

    after = row(engine, "reviews")
    assert (after["guest_id"], after["hotel_id"]) == (None, hotel_id)


# --- the RESTRICTs 0018 must not weaken -------------------------------------------------------


def test_a_booking_with_a_payment_still_cannot_be_deleted(
    api: TestClient, hotel: str, booking: str, engine: Engine
) -> None:
    review(api, hotel, booking)
    revenue(api, hotel, booking)
    charge(api, hotel, booking)

    response = api.delete(booking_url(hotel, booking))

    assert response.status_code == 409
    message = response.json()["error"]["message"]
    assert "payments" in message
    assert "review" not in message and "revenue" not in message
    assert count(engine, "bookings") == 1
    assert row(engine, "reviews")["booking_id"] is not None
    assert row(engine, "revenue")["booking_id"] is not None


def test_a_guest_with_a_booking_still_cannot_be_deleted(
    api: TestClient, hotel: str, booking: str, engine: Engine
) -> None:
    guest = guest_of(api, hotel, booking)
    review(api, hotel, booking)

    response = api.delete(f"/api/v1/hotels/{hotel}/guests/{guest}")

    assert response.status_code == 409
    message = response.json()["error"]["message"]
    assert "reservations" in message
    assert "review" not in message
    assert count(engine, "guests") == 1
    assert row(engine, "reviews")["guest_id"] is not None


# --- the API contract and the figures ---------------------------------------------------------


def test_the_detached_references_are_nullable_in_the_api_contract() -> None:
    schemas = create_app(Settings(environment="test")).openapi()["components"]["schemas"]

    def nullable(schema: str, field: str) -> bool:
        spec = schemas[schema]["properties"][field]
        return {"type": "null"} in spec.get("anyOf", [])

    assert nullable("ReviewResponse", "booking_public_id")
    assert nullable("ReviewResponse", "guest_public_id")
    assert nullable("RevenueResponse", "booking_public_id")


def test_a_detached_review_is_still_counted_once(api: TestClient, hotel: str, booking: str) -> None:
    review(api, hotel, booking)
    counted = overview(api, hotel)["reviews"]

    assert api.delete(booking_url(hotel, booking)).status_code == 204

    assert overview(api, hotel)["reviews"] == counted
    assert counted["review_count"] == 1


# --- atomicity --------------------------------------------------------------------------------


def test_a_failed_deletion_detaches_nothing(
    api: TestClient,
    hotel: str,
    booking: str,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The booking's audit event fails: the booking, and both references to it, come back."""
    review(api, hotel, booking)
    revenue(api, hotel, booking)
    record = AuditTrail.record

    def failing(self: AuditTrail, action: AuditAction, *args: Any, **kwargs: Any) -> Any:
        if action is AuditAction.BOOKING_DELETED:
            raise IntegrityError("INSERT INTO audit_events", {}, Exception("injected"))
        return record(self, action, *args, **kwargs)

    monkeypatch.setattr(AuditTrail, "record", failing)

    response = api.delete(booking_url(hotel, booking))

    assert response.status_code >= 400, response.text
    assert count(engine, "bookings") == 1
    assert row(engine, "reviews")["booking_id"] is not None
    assert row(engine, "revenue")["booking_id"] is not None


# --- the schema itself ------------------------------------------------------------------------


def foreign_keys(connection: sa.Connection) -> dict[str, str]:
    rows = connection.execute(
        sa.text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE contype = 'f' ORDER BY conname"
        )
    ).all()
    return {str(name): str(definition) for name, definition in rows}


def test_the_database_is_migrated_past_0018(engine: Engine) -> None:
    """At the head, which 0018 leads to: 0019 replaced one index and touched no foreign key."""
    with engine.connect() as connection:
        revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    assert revision == HEAD


def test_the_catalog_holds_every_foreign_key_as_0018_left_it(engine: Engine) -> None:
    """All 38, compared whole: the three 0018 changed carry their column list, and the other
    35 -- every RESTRICT and CASCADE, every composite key -- are exactly what they were."""
    with engine.connect() as connection:
        found = foreign_keys(connection)
        columns: dict[str, int] = {
            str(name): int(length)
            for name, length in connection.execute(
                sa.text(
                    "SELECT conname, array_length(confdelsetcols, 1) FROM pg_constraint "
                    "WHERE contype = 'f' AND confdelsetcols IS NOT NULL"
                )
            ).all()
        }

    assert found == FOREIGN_KEYS
    assert len(found) == 38
    assert columns == dict.fromkeys(CHANGED_BY_0018, 1)
    assert sum(" SET NULL" in definition for definition in found.values()) == 3


def test_the_models_match_the_migrated_schema(engine: Engine) -> None:
    """``alembic check``: autogenerate finds nothing to do between the models and the database."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "database" / "migrations"))
    command.check(config)


def test_the_downgrade_restores_0001s_keys_and_the_upgrade_reapplies_0018(engine: Engine) -> None:
    """0018's own functions, run in a transaction that is rolled back: nothing is left behind."""
    spec = importlib.util.spec_from_file_location("migration_0018", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                downgraded = foreign_keys(connection)
                migration.upgrade()
                upgraded = foreign_keys(connection)
        finally:
            transaction.rollback()

    assert {name: downgraded[name] for name in CHANGED_BY_0018} == BEFORE_0018
    assert {k: v for k, v in downgraded.items() if k not in CHANGED_BY_0018} == {
        k: v for k, v in FOREIGN_KEYS.items() if k not in CHANGED_BY_0018
    }
    assert upgraded == FOREIGN_KEYS


def test_the_migration_is_the_one_on_disk() -> None:
    assert MIGRATION.is_file()
    assert Path(MIGRATION).name.endswith(f"{REVISION}.py")
