"""Hotel domain against real PostgreSQL.

Persistence behaviour -- unique constraints, RESTRICT on delete, ordering, partial updates --
is only meaningful against the real database, so these run there. SQLite is not substituted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Guest, Hotel
from tests.integration.conftest import authenticated_client, requires_postgres

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "hotels@example.test"

pytestmark = requires_postgres

HOTELS = "/api/v1/hotels"


def payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "slug": "acropolis-view",
        "name": "Acropolis View Hotel",
        "address_line1": "1 Dionysiou Areopagitou",
        "city": "Athens",
        "country_code": "GR",
        "timezone": "Europe/Athens",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    # Stage 4.2: every hotel-scoped endpoint requires an authenticated MEMBER, so
    # the suite's client carries a token. `POST /hotels` grants its creator `owner`,
    # which is why suites that build their own hotels need nothing further.
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.commit()


# --- 1-2. create -------------------------------------------------------------------------


def test_create_hotel_returns_201_and_the_created_resource(api: TestClient) -> None:
    response = api.post(HOTELS, json=payload())
    body = response.json()

    assert response.status_code == 201
    assert body["slug"] == "acropolis-view"
    assert body["name"] == "Acropolis View Hotel"
    assert body["is_active"] is True
    assert uuid.UUID(body["public_id"])  # server-assigned


def test_created_hotel_is_actually_persisted(api: TestClient, session: Session) -> None:
    api.post(HOTELS, json=payload())

    stored = session.scalars(sa.select(Hotel)).one()
    assert stored.slug == "acropolis-view"
    assert stored.timezone == "Europe/Athens"


def test_create_normalises_lowercase_country_and_currency(api: TestClient) -> None:
    body = api.post(HOTELS, json=payload(country_code="gr", currency="eur")).json()

    assert body["country_code"] == "GR"
    assert body["currency"] == "EUR"


def test_create_accepts_optional_fields(api: TestClient) -> None:
    body = api.post(
        HOTELS,
        json=payload(
            star_rating=5,
            latitude="37.971230",
            longitude="23.725750",
            email="stay@example.test",
            region="Attica",
        ),
    ).json()

    assert body["star_rating"] == 5
    assert Decimal(body["latitude"]) == Decimal("37.971230")
    assert body["region"] == "Attica"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("slug", "Not A Slug"),
        ("country_code", "GRC"),
        ("currency", "EU"),
        ("star_rating", 6),
        ("star_rating", 0),
        ("latitude", "91.0"),
        ("longitude", "-181.0"),
    ],
)
def test_create_validation_failure_returns_422(api: TestClient, field: str, value: object) -> None:
    response = api.post(HOTELS, json=payload(**{field: value}))
    body = response.json()

    assert response.status_code == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(field in d["location"] for d in body["error"]["details"])


def test_create_missing_required_field_returns_422(api: TestClient) -> None:
    incomplete = payload()
    del incomplete["currency"]

    response = api.post(HOTELS, json=incomplete)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- 12. uniqueness ------------------------------------------------------------------------


def test_duplicate_slug_returns_409_not_500(api: TestClient) -> None:
    api.post(HOTELS, json=payload())

    response = api.post(HOTELS, json=payload(name="Another Hotel"))
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "acropolis-view" in body["error"]["message"]


def test_duplicate_slug_does_not_leak_sql_or_constraint_internals(api: TestClient) -> None:
    api.post(HOTELS, json=payload())
    text = api.post(HOTELS, json=payload()).text.lower()

    for leak in ["insert", "uq_hotels_slug", "psycopg", "traceback", "sqlalchemy"]:
        assert leak not in text, f"leaked {leak!r}"


def test_a_failed_create_leaves_no_partial_row(api: TestClient, session: Session) -> None:
    """The rollback must be real, not merely reported."""
    api.post(HOTELS, json=payload())
    api.post(HOTELS, json=payload(name="Another Hotel"))

    assert session.scalar(sa.select(sa.func.count()).select_from(Hotel)) == 1


# --- 3-5. list, pagination, ordering ---------------------------------------------------------


def test_list_returns_the_pagination_envelope(api: TestClient) -> None:
    api.post(HOTELS, json=payload())
    body = api.get(HOTELS).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["pages"] == 1
    assert len(body["items"]) == 1


def test_list_is_empty_when_there_are_no_hotels(api: TestClient) -> None:
    body = api.get(HOTELS).json()

    assert body["items"] == []
    assert body["total"] == 0
    assert body["pages"] == 0


def test_pagination_splits_results_and_reports_the_page_count(api: TestClient) -> None:
    for index in range(5):
        api.post(HOTELS, json=payload(slug=f"hotel-{index}", name=f"Hotel {index}"))

    first = api.get(HOTELS, params={"page": 1, "page_size": 2}).json()
    second = api.get(HOTELS, params={"page": 2, "page_size": 2}).json()
    third = api.get(HOTELS, params={"page": 3, "page_size": 2}).json()

    assert [first["total"], first["pages"]] == [5, 3]
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    assert len(third["items"]) == 1


def test_pages_do_not_overlap_or_skip_rows(api: TestClient) -> None:
    for index in range(6):
        api.post(HOTELS, json=payload(slug=f"hotel-{index}", name=f"Hotel {index}"))

    seen: list[str] = []
    for page in (1, 2, 3):
        seen += [
            item["slug"]
            for item in api.get(HOTELS, params={"page": page, "page_size": 2}).json()["items"]
        ]

    assert len(seen) == len(set(seen)) == 6


def test_ordering_is_deterministic_by_name(api: TestClient) -> None:
    for slug, name in [("c", "Charlie"), ("a", "Alpha"), ("b", "Bravo")]:
        api.post(HOTELS, json=payload(slug=slug, name=name))

    names = [item["name"] for item in api.get(HOTELS).json()["items"]]

    assert names == ["Alpha", "Bravo", "Charlie"]


def test_ordering_is_stable_when_names_are_identical(api: TestClient) -> None:
    """PostgreSQL gives no ordering guarantee among equal rows; the id tiebreaker does."""
    for index in range(4):
        api.post(HOTELS, json=payload(slug=f"same-{index}", name="Identical Name"))

    runs = [[item["slug"] for item in api.get(HOTELS).json()["items"]] for _ in range(3)]

    assert runs[0] == runs[1] == runs[2]


def test_page_size_is_capped(api: TestClient) -> None:
    """A client must not be able to request the entire table in one call."""
    response = api.get(HOTELS, params={"page_size": 1000})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(("param", "value"), [("page", 0), ("page", -1), ("page_size", 0)])
def test_invalid_pagination_parameters_return_422(api: TestClient, param: str, value: int) -> None:
    assert api.get(HOTELS, params={param: value}).status_code == 422


# --- 6-7. get one ----------------------------------------------------------------------------


def test_get_existing_hotel_returns_it(api: TestClient) -> None:
    created = api.post(HOTELS, json=payload()).json()

    response = api.get(f"{HOTELS}/{created['public_id']}")

    assert response.status_code == 200
    assert response.json()["public_id"] == created["public_id"]


def test_get_nonexistent_hotel_returns_404_in_the_error_envelope(api: TestClient) -> None:
    response = api.get(f"{HOTELS}/{uuid.uuid4()}")
    body = response.json()

    assert response.status_code == 404
    assert set(body) == {"error"}
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "Hotel not found."


def test_malformed_public_id_returns_422_not_500(api: TestClient) -> None:
    response = api.get(f"{HOTELS}/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- 13. response schema ----------------------------------------------------------------------


def test_response_exposes_public_id_and_never_the_internal_id(api: TestClient) -> None:
    body = api.post(HOTELS, json=payload()).json()

    assert "public_id" in body
    assert "id" not in body


def test_response_schema_is_exactly_the_declared_contract(api: TestClient) -> None:
    body = api.post(HOTELS, json=payload()).json()

    assert set(body) == {
        "public_id",
        "slug",
        "name",
        "address_line1",
        "address_line2",
        "city",
        "region",
        "postal_code",
        "country_code",
        "latitude",
        "longitude",
        "email",
        "phone",
        "website",
        "timezone",
        "currency",
        "star_rating",
        "is_active",
        "created_at",
        "updated_at",
    }


def test_timestamps_are_timezone_aware_in_the_response(api: TestClient) -> None:
    body = api.post(HOTELS, json=payload()).json()

    assert dt.datetime.fromisoformat(body["created_at"]).tzinfo is not None


# --- 8-9. update -------------------------------------------------------------------------------


def test_partial_update_changes_only_the_supplied_field(api: TestClient) -> None:
    created = api.post(HOTELS, json=payload(star_rating=3, region="Attica")).json()

    updated = api.patch(f"{HOTELS}/{created['public_id']}", json={"name": "Renamed Hotel"}).json()

    assert updated["name"] == "Renamed Hotel"
    # Everything else is preserved.
    assert updated["star_rating"] == 3
    assert updated["region"] == "Attica"
    assert updated["city"] == created["city"]
    assert updated["slug"] == created["slug"]


def test_partial_update_persists(api: TestClient, session: Session) -> None:
    created = api.post(HOTELS, json=payload()).json()
    api.patch(f"{HOTELS}/{created['public_id']}", json={"city": "Thessaloniki"})

    assert session.scalars(sa.select(Hotel)).one().city == "Thessaloniki"


def test_update_can_explicitly_null_a_nullable_field(api: TestClient) -> None:
    """An explicit null clears the column; an omitted field does not."""
    created = api.post(HOTELS, json=payload(region="Attica")).json()

    updated = api.patch(f"{HOTELS}/{created['public_id']}", json={"region": None}).json()

    assert updated["region"] is None


def test_empty_update_body_is_a_no_op_returning_current_state(api: TestClient) -> None:
    created = api.post(HOTELS, json=payload()).json()

    response = api.patch(f"{HOTELS}/{created['public_id']}", json={})

    assert response.status_code == 200
    assert response.json()["name"] == created["name"]


def test_update_validates_supplied_fields(api: TestClient) -> None:
    created = api.post(HOTELS, json=payload()).json()

    response = api.patch(f"{HOTELS}/{created['public_id']}", json={"star_rating": 9})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_update_rejects_unknown_fields(api: TestClient) -> None:
    """extra='forbid' stops a typo silently doing nothing."""
    created = api.post(HOTELS, json=payload()).json()

    response = api.patch(f"{HOTELS}/{created['public_id']}", json={"nmae": "typo"})

    assert response.status_code == 422


def test_slug_cannot_be_changed_through_patch(api: TestClient) -> None:
    """The slug is the stable URL identity; changing it would break existing links."""
    created = api.post(HOTELS, json=payload()).json()

    response = api.patch(f"{HOTELS}/{created['public_id']}", json={"slug": "new-slug"})

    assert response.status_code == 422


def test_update_nonexistent_hotel_returns_404(api: TestClient) -> None:
    response = api.patch(f"{HOTELS}/{uuid.uuid4()}", json={"name": "Ghost"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_deactivating_a_hotel_is_an_update_not_a_delete(api: TestClient) -> None:
    created = api.post(HOTELS, json=payload()).json()

    updated = api.patch(f"{HOTELS}/{created['public_id']}", json={"is_active": False}).json()

    assert updated["is_active"] is False


# --- 10-11. delete -------------------------------------------------------------------------------


def test_delete_hotel_without_dependencies_returns_204(api: TestClient) -> None:
    created = api.post(HOTELS, json=payload()).json()

    response = api.delete(f"{HOTELS}/{created['public_id']}")

    assert response.status_code == 204
    assert response.content == b""


def test_deleted_hotel_is_gone(api: TestClient, session: Session) -> None:
    created = api.post(HOTELS, json=payload()).json()
    api.delete(f"{HOTELS}/{created['public_id']}")

    assert session.scalar(sa.select(sa.func.count()).select_from(Hotel)) == 0
    assert api.get(f"{HOTELS}/{created['public_id']}").status_code == 404


def test_delete_nonexistent_hotel_returns_404(api: TestClient) -> None:
    response = api.delete(f"{HOTELS}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_delete_with_dependencies_returns_409_and_does_not_cascade(
    api: TestClient, session: Session
) -> None:
    """PostgreSQL's ON DELETE RESTRICT must be honoured, not worked around."""
    created = api.post(HOTELS, json=payload()).json()
    hotel = session.scalars(sa.select(Hotel)).one()
    session.add(Guest(hotel_id=hotel.id, first_name="Ada", last_name="Lovelace"))
    session.commit()

    response = api.delete(f"{HOTELS}/{created['public_id']}")
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "cannot be deleted" in body["error"]["message"]

    # Neither the hotel nor its dependent row was removed.
    assert session.scalar(sa.select(sa.func.count()).select_from(Hotel)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(Guest)) == 1


def test_restrict_violation_does_not_leak_constraint_or_sql(
    api: TestClient, session: Session
) -> None:
    api.post(HOTELS, json=payload())
    hotel = session.scalars(sa.select(Hotel)).one()
    session.add(Guest(hotel_id=hotel.id, first_name="Ada", last_name="Lovelace"))
    session.commit()

    text = api.delete(f"{HOTELS}/{hotel.public_id}").text.lower()

    # The constraint name, the statement and the driver are all internal.
    for leak in [
        "fk_guests_hotel_id_hotels",
        "fk_",
        "delete from",
        "psycopg",
        "sqlalchemy",
        "restrictviolation",
        "traceback",
        "detail:",
        "key (id)",
    ]:
        assert leak not in text, f"leaked {leak!r}"

    # The domain words in the guidance ("rooms", "guests", ...) are deliberate: they tell the
    # caller what to clear first. They are public vocabulary, already in the OpenAPI schema,
    # not a disclosure of database internals.
    assert "cannot be deleted" in text


def test_hotel_remains_usable_after_a_refused_delete(api: TestClient, session: Session) -> None:
    """The rollback must leave the session and the row in a working state."""
    created = api.post(HOTELS, json=payload()).json()
    hotel = session.scalars(sa.select(Hotel)).one()
    session.add(Guest(hotel_id=hotel.id, first_name="Ada", last_name="Lovelace"))
    session.commit()

    api.delete(f"{HOTELS}/{created['public_id']}")

    assert api.get(f"{HOTELS}/{created['public_id']}").status_code == 200
    assert (
        api.patch(f"{HOTELS}/{created['public_id']}", json={"name": "Still Works"}).status_code
        == 200
    )
