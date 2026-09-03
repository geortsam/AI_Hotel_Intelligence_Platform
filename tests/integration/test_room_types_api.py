"""Room type domain against real PostgreSQL.

The hotel-scoped identity is the point of this suite: a code is unique only within a hotel,
so most of what needs proving -- that hotel A's `DBL` is unreachable through hotel B, that
the unique constraint is per-hotel, that RESTRICT refuses a delete -- is database behaviour.
SQLite is not substituted.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Hotel, Room, RoomType
from tests.integration.conftest import authenticated_client, requires_postgres

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "room_types@example.test"

pytestmark = requires_postgres


def hotel_payload(slug: str = "acropolis-view", **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "slug": slug,
        "name": f"Hotel {slug}",
        "address_line1": "1 Dionysiou Areopagitou",
        "city": "Athens",
        "country_code": "GR",
        "timezone": "Europe/Athens",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


def type_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "code": "DLXDBL",
        "name": "Deluxe Double",
        "max_occupancy": 3,
        "standard_occupancy": 2,
        "bed_count": 1,
        "base_price": "120.00",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


def types_url(hotel_public_id: str) -> str:
    return f"/api/v1/hotels/{hotel_public_id}/room-types"


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


@pytest.fixture
def hotel_id(api: TestClient) -> str:
    """A persisted hotel; returns its public_id."""
    return str(api.post("/api/v1/hotels", json=hotel_payload()).json()["public_id"])


# --- 1-2. create ---------------------------------------------------------------------------


def test_create_room_type_returns_201(api: TestClient, hotel_id: str) -> None:
    response = api.post(types_url(hotel_id), json=type_payload())
    body = response.json()

    assert response.status_code == 201
    assert body["code"] == "DLXDBL"
    assert body["name"] == "Deluxe Double"
    assert body["is_active"] is True
    assert Decimal(body["base_price"]) == Decimal("120.00")


def test_created_room_type_is_persisted_against_the_right_hotel(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(types_url(hotel_id), json=type_payload())

    stored = session.scalars(sa.select(RoomType)).one()
    hotel = session.scalars(sa.select(Hotel)).one()
    assert stored.hotel_id == hotel.id
    assert stored.code == "DLXDBL"


def test_create_normalises_lowercase_code_and_currency(api: TestClient, hotel_id: str) -> None:
    body = api.post(types_url(hotel_id), json=type_payload(code="std", currency="eur")).json()

    assert body["code"] == "STD"
    assert body["currency"] == "EUR"


def test_create_accepts_optional_fields(api: TestClient, hotel_id: str) -> None:
    body = api.post(
        types_url(hotel_id),
        json=type_payload(description="Sea view", bed_configuration="1 king", size_sqm="28.50"),
    ).json()

    assert body["description"] == "Sea view"
    assert body["bed_configuration"] == "1 king"
    assert Decimal(body["size_sqm"]) == Decimal("28.50")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", ""),
        ("code", "has space"),
        ("name", ""),
        ("max_occupancy", 0),
        ("bed_count", 0),
        ("base_price", "-1.00"),
        ("currency", "EU"),
        ("size_sqm", "0"),
    ],
)
def test_create_validation_failure_returns_422(
    api: TestClient, hotel_id: str, field: str, value: object
) -> None:
    response = api.post(types_url(hotel_id), json=type_payload(**{field: value}))
    body = response.json()

    assert response.status_code == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(field in d["location"] for d in body["error"]["details"])


def test_standard_occupancy_above_max_is_rejected_at_the_edge(
    api: TestClient, hotel_id: str
) -> None:
    """Mirrors the database CHECK, but with a message naming both values."""
    response = api.post(
        types_url(hotel_id), json=type_payload(max_occupancy=2, standard_occupancy=4)
    )

    assert response.status_code == 422
    assert "standard_occupancy" in response.text


def test_create_rejects_unknown_fields(api: TestClient, hotel_id: str) -> None:
    assert api.post(types_url(hotel_id), json=type_payload(hotel_id=1)).status_code == 422


# --- 3. nonexistent hotel ---------------------------------------------------------------------


def test_create_under_nonexistent_hotel_returns_404(api: TestClient) -> None:
    response = api.post(types_url(str(uuid.uuid4())), json=type_payload())
    body = response.json()

    assert response.status_code == 404
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "Hotel not found."


def test_create_under_nonexistent_hotel_creates_nothing(api: TestClient, session: Session) -> None:
    """A typo in the path must not conjure a hotel."""
    api.post(types_url(str(uuid.uuid4())), json=type_payload())

    assert session.scalar(sa.select(sa.func.count()).select_from(Hotel)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(RoomType)) == 0


def test_listing_under_nonexistent_hotel_returns_404_not_an_empty_page(
    api: TestClient,
) -> None:
    """ "No room types" and "no such hotel" are different answers."""
    response = api.get(types_url(str(uuid.uuid4())))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_malformed_hotel_public_id_returns_422(api: TestClient) -> None:
    assert api.get(types_url("not-a-uuid")).status_code == 422


# --- 4-6. list, pagination, ordering ------------------------------------------------------------


def test_list_returns_the_shared_pagination_envelope(api: TestClient, hotel_id: str) -> None:
    api.post(types_url(hotel_id), json=type_payload())
    body = api.get(types_url(hotel_id)).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] == 1
    assert body["pages"] == 1


def test_list_is_empty_for_a_hotel_with_no_room_types(api: TestClient, hotel_id: str) -> None:
    body = api.get(types_url(hotel_id)).json()

    assert body["items"] == []
    assert body["total"] == 0
    assert body["pages"] == 0


def test_pagination_splits_results(api: TestClient, hotel_id: str) -> None:
    for index in range(5):
        api.post(types_url(hotel_id), json=type_payload(code=f"RT{index}"))

    first = api.get(types_url(hotel_id), params={"page": 1, "page_size": 2}).json()
    third = api.get(types_url(hotel_id), params={"page": 3, "page_size": 2}).json()

    assert [first["total"], first["pages"]] == [5, 3]
    assert len(first["items"]) == 2
    assert len(third["items"]) == 1


def test_pages_do_not_overlap_or_skip(api: TestClient, hotel_id: str) -> None:
    for index in range(6):
        api.post(types_url(hotel_id), json=type_payload(code=f"RT{index}"))

    seen: list[str] = []
    for page in (1, 2, 3):
        seen += [
            item["code"]
            for item in api.get(types_url(hotel_id), params={"page": page, "page_size": 2}).json()[
                "items"
            ]
        ]

    assert len(seen) == len(set(seen)) == 6


def test_ordering_is_deterministic_by_code(api: TestClient, hotel_id: str) -> None:
    for code in ["STD", "DLX", "SUITE"]:
        api.post(types_url(hotel_id), json=type_payload(code=code))

    codes = [item["code"] for item in api.get(types_url(hotel_id)).json()["items"]]

    assert codes == ["DLX", "STD", "SUITE"]
    # Repeat runs agree: code is unique per hotel, so it is a total order on its own.
    assert codes == [item["code"] for item in api.get(types_url(hotel_id)).json()["items"]]


def test_page_size_is_capped(api: TestClient, hotel_id: str) -> None:
    assert api.get(types_url(hotel_id), params={"page_size": 1000}).status_code == 422


# --- 7 + 17. hotel scoping ------------------------------------------------------------------------


def test_listing_is_filtered_to_the_hotel_in_the_path(api: TestClient) -> None:
    first = api.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"]
    second = api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"]

    api.post(types_url(first), json=type_payload(code="AAA"))
    api.post(types_url(first), json=type_payload(code="BBB"))
    api.post(types_url(second), json=type_payload(code="CCC"))

    assert [i["code"] for i in api.get(types_url(first)).json()["items"]] == ["AAA", "BBB"]
    assert [i["code"] for i in api.get(types_url(second)).json()["items"]] == ["CCC"]


def test_the_same_code_may_exist_at_two_different_hotels(api: TestClient) -> None:
    """UNIQUE(hotel_id, code) is per-hotel, not global."""
    first = api.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"]
    second = api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"]

    assert api.post(types_url(first), json=type_payload(code="DBL")).status_code == 201
    assert api.post(types_url(second), json=type_payload(code="DBL")).status_code == 201


def test_a_room_type_is_not_addressable_through_another_hotels_url(api: TestClient) -> None:
    """The core guarantee of the nested design."""
    owner = api.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"]
    other = api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"]
    api.post(types_url(owner), json=type_payload(code="DBL"))

    assert api.get(f"{types_url(owner)}/DBL").status_code == 200
    assert api.get(f"{types_url(other)}/DBL").status_code == 404
    assert api.patch(f"{types_url(other)}/DBL", json={"name": "Hijacked"}).status_code == 404
    assert api.delete(f"{types_url(other)}/DBL").status_code == 404


def test_a_failed_cross_hotel_update_changes_nothing(api: TestClient, session: Session) -> None:
    owner = api.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"]
    other = api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"]
    api.post(types_url(owner), json=type_payload(code="DBL", name="Original"))

    api.patch(f"{types_url(other)}/DBL", json={"name": "Hijacked"})

    assert session.scalars(sa.select(RoomType)).one().name == "Original"


def test_response_carries_the_owning_hotel_public_id(api: TestClient, hotel_id: str) -> None:
    body = api.post(types_url(hotel_id), json=type_payload()).json()

    assert body["hotel_public_id"] == hotel_id


# --- 8-9. get one --------------------------------------------------------------------------------


def test_get_existing_room_type(api: TestClient, hotel_id: str) -> None:
    api.post(types_url(hotel_id), json=type_payload())

    response = api.get(f"{types_url(hotel_id)}/DLXDBL")

    assert response.status_code == 200
    assert response.json()["code"] == "DLXDBL"


def test_get_is_case_insensitive_on_the_code(api: TestClient, hotel_id: str) -> None:
    """Codes are stored canonically upper-case, so a lower-case URL still resolves."""
    api.post(types_url(hotel_id), json=type_payload(code="DLX"))

    assert api.get(f"{types_url(hotel_id)}/dlx").status_code == 200


def test_get_nonexistent_room_type_returns_404(api: TestClient, hotel_id: str) -> None:
    response = api.get(f"{types_url(hotel_id)}/NOSUCH")
    body = response.json()

    assert response.status_code == 404
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "Room type not found for this hotel."


# --- 12. uniqueness ------------------------------------------------------------------------------


def test_duplicate_code_within_a_hotel_returns_409(api: TestClient, hotel_id: str) -> None:
    api.post(types_url(hotel_id), json=type_payload())

    response = api.post(types_url(hotel_id), json=type_payload(name="Different name"))
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "DLXDBL" in body["error"]["message"]


def test_duplicate_code_leaks_no_sql_or_constraint_name(api: TestClient, hotel_id: str) -> None:
    api.post(types_url(hotel_id), json=type_payload())
    text = api.post(types_url(hotel_id), json=type_payload()).text.lower()

    for leak in ["insert", "uq_room_types", "psycopg", "sqlalchemy", "traceback", "23505"]:
        assert leak not in text, f"leaked {leak!r}"


def test_a_rejected_duplicate_leaves_one_row(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(types_url(hotel_id), json=type_payload())
    api.post(types_url(hotel_id), json=type_payload(name="Different"))

    assert session.scalar(sa.select(sa.func.count()).select_from(RoomType)) == 1


# --- 10-11. update -------------------------------------------------------------------------------


def test_partial_update_preserves_unspecified_fields(api: TestClient, hotel_id: str) -> None:
    created = api.post(
        types_url(hotel_id), json=type_payload(description="Sea view", bed_count=2)
    ).json()

    updated = api.patch(f"{types_url(hotel_id)}/DLXDBL", json={"name": "Renamed"}).json()

    assert updated["name"] == "Renamed"
    assert updated["description"] == "Sea view"
    assert updated["bed_count"] == 2
    assert updated["code"] == created["code"]
    assert Decimal(updated["base_price"]) == Decimal("120.00")


def test_partial_update_persists(api: TestClient, hotel_id: str, session: Session) -> None:
    api.post(types_url(hotel_id), json=type_payload())
    api.patch(f"{types_url(hotel_id)}/DLXDBL", json={"base_price": "199.99"})

    assert session.scalars(sa.select(RoomType)).one().base_price == Decimal("199.99")


def test_update_can_explicitly_null_a_nullable_field(api: TestClient, hotel_id: str) -> None:
    api.post(types_url(hotel_id), json=type_payload(description="Sea view"))

    body = api.patch(f"{types_url(hotel_id)}/DLXDBL", json={"description": None}).json()

    assert body["description"] is None


def test_empty_update_is_a_no_op(api: TestClient, hotel_id: str) -> None:
    api.post(types_url(hotel_id), json=type_payload())

    response = api.patch(f"{types_url(hotel_id)}/DLXDBL", json={})

    assert response.status_code == 200
    assert response.json()["name"] == "Deluxe Double"


def test_update_validates_supplied_values(api: TestClient, hotel_id: str) -> None:
    api.post(types_url(hotel_id), json=type_payload())

    response = api.patch(f"{types_url(hotel_id)}/DLXDBL", json={"bed_count": 0})

    assert response.status_code == 422


def test_lowering_max_occupancy_below_stored_standard_is_rejected_cleanly(
    api: TestClient, hotel_id: str
) -> None:
    """The schema cannot catch this -- only one value is supplied, so the other comes from
    the stored row. The service checks the post-update state."""
    api.post(types_url(hotel_id), json=type_payload(max_occupancy=4, standard_occupancy=3))

    response = api.patch(f"{types_url(hotel_id)}/DLXDBL", json={"max_occupancy": 2})
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "standard_occupancy" in body["error"]["message"]


def test_code_cannot_be_changed_through_patch(api: TestClient, hotel_id: str) -> None:
    """The code is the URL identity, exactly as slug is for a hotel."""
    api.post(types_url(hotel_id), json=type_payload())

    assert api.patch(f"{types_url(hotel_id)}/DLXDBL", json={"code": "NEW"}).status_code == 422


def test_update_nonexistent_room_type_returns_404(api: TestClient, hotel_id: str) -> None:
    response = api.patch(f"{types_url(hotel_id)}/NOSUCH", json={"name": "Ghost"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_update_under_nonexistent_hotel_returns_404(api: TestClient) -> None:
    response = api.patch(f"{types_url(str(uuid.uuid4()))}/DLXDBL", json={"name": "Ghost"})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


def test_deactivating_is_an_update_not_a_delete(api: TestClient, hotel_id: str) -> None:
    api.post(types_url(hotel_id), json=type_payload())

    body = api.patch(f"{types_url(hotel_id)}/DLXDBL", json={"is_active": False}).json()

    assert body["is_active"] is False


# --- 13-14. delete -------------------------------------------------------------------------------


def test_delete_room_type_without_dependencies_returns_204(api: TestClient, hotel_id: str) -> None:
    api.post(types_url(hotel_id), json=type_payload())

    response = api.delete(f"{types_url(hotel_id)}/DLXDBL")

    assert response.status_code == 204
    assert response.content == b""
    assert api.get(f"{types_url(hotel_id)}/DLXDBL").status_code == 404


def test_delete_nonexistent_room_type_returns_404(api: TestClient, hotel_id: str) -> None:
    assert api.delete(f"{types_url(hotel_id)}/NOSUCH").status_code == 404


def test_delete_with_dependent_rooms_returns_409_and_does_not_cascade(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    """rooms.room_type_id is ON DELETE RESTRICT. The database stays the authority."""
    api.post(types_url(hotel_id), json=type_payload())
    hotel = session.scalars(sa.select(Hotel)).one()
    room_type = session.scalars(sa.select(RoomType)).one()
    session.add(Room(hotel_id=hotel.id, room_type_id=room_type.id, room_number="101"))
    session.commit()

    response = api.delete(f"{types_url(hotel_id)}/DLXDBL")
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "cannot be deleted" in body["error"]["message"]

    # Neither the room type nor the room was removed.
    assert session.scalar(sa.select(sa.func.count()).select_from(RoomType)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(Room)) == 1


def test_restrict_refusal_leaks_no_constraint_or_sql(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(types_url(hotel_id), json=type_payload())
    hotel = session.scalars(sa.select(Hotel)).one()
    room_type = session.scalars(sa.select(RoomType)).one()
    session.add(Room(hotel_id=hotel.id, room_type_id=room_type.id, room_number="101"))
    session.commit()

    text = api.delete(f"{types_url(hotel_id)}/DLXDBL").text.lower()

    for leak in [
        "fk_rooms_room_type_id_hotel_id_room_types",
        "delete from",
        "psycopg",
        "sqlalchemy",
        "restrictviolation",
        "traceback",
        "detail:",
        "23001",
    ]:
        assert leak not in text, f"leaked {leak!r}"


def test_room_type_remains_usable_after_a_refused_delete(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(types_url(hotel_id), json=type_payload())
    hotel = session.scalars(sa.select(Hotel)).one()
    room_type = session.scalars(sa.select(RoomType)).one()
    session.add(Room(hotel_id=hotel.id, room_type_id=room_type.id, room_number="101"))
    session.commit()

    api.delete(f"{types_url(hotel_id)}/DLXDBL")

    assert api.get(f"{types_url(hotel_id)}/DLXDBL").status_code == 200
    patched = api.patch(f"{types_url(hotel_id)}/DLXDBL", json={"name": "Still Works"})
    assert patched.status_code == 200


def test_deleting_a_hotel_with_room_types_is_refused(api: TestClient, hotel_id: str) -> None:
    """The hotel-level RESTRICT still holds now that a child exists."""
    api.post(types_url(hotel_id), json=type_payload())

    assert api.delete(f"/api/v1/hotels/{hotel_id}").status_code == 409


# --- 15-16. response schema and error envelope ---------------------------------------------------


def test_response_schema_is_exactly_the_declared_contract(api: TestClient, hotel_id: str) -> None:
    body = api.post(types_url(hotel_id), json=type_payload()).json()

    assert set(body) == {
        "hotel_public_id",
        "code",
        "name",
        "description",
        "max_occupancy",
        "standard_occupancy",
        "bed_count",
        "bed_configuration",
        "size_sqm",
        "base_price",
        "currency",
        "is_active",
        "created_at",
        "updated_at",
    }


def test_response_never_exposes_internal_ids(api: TestClient, hotel_id: str) -> None:
    body = api.post(types_url(hotel_id), json=type_payload()).json()

    assert "id" not in body
    assert "hotel_id" not in body
    assert "public_id" not in body  # room types have none, and none was invented


@pytest.mark.parametrize(
    ("method", "path_suffix", "expected"),
    [("get", "/NOSUCH", 404), ("patch", "/NOSUCH", 404), ("delete", "/NOSUCH", 404)],
)
def test_errors_use_the_shared_envelope(
    api: TestClient, hotel_id: str, method: str, path_suffix: str, expected: int
) -> None:
    call = getattr(api, method)
    url = f"{types_url(hotel_id)}{path_suffix}"
    response = call(url, json={}) if method == "patch" else call(url)
    body = response.json()

    assert response.status_code == expected
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
