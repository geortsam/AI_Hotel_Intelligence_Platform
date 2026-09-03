"""Amenity catalogue and room-type assignments against real PostgreSQL.

What only the database can prove here: that ``uq_amenities_code`` is global, that the
composite primary key on ``room_type_amenities`` rejects a duplicate assignment, that
``ON DELETE RESTRICT`` refuses to delete an assigned amenity, and that removing an assignment
leaves the shared catalogue entry intact. SQLite is not substituted.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, NotFoundError
from app.models import Amenity, RoomTypeAmenity
from app.repositories.amenity import AmenityRepository
from app.schemas.amenity import AmenityCreate, AmenityResponse, AmenityUpdate
from app.services.amenity import AmenityService
from tests.integration.conftest import authenticated_client, requires_postgres

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "amenities@example.test"

pytestmark = requires_postgres

AMENITIES = "/api/v1/amenities"


def hotel_payload(slug: str) -> dict[str, object]:
    return {
        "slug": slug,
        "name": f"Hotel {slug}",
        "address_line1": "1 Dionysiou Areopagitou",
        "city": "Athens",
        "country_code": "GR",
        "timezone": "Europe/Athens",
        "currency": "EUR",
    }


def type_payload(code: str = "DLX") -> dict[str, object]:
    return {
        "code": code,
        "name": f"Type {code}",
        "max_occupancy": 3,
        "standard_occupancy": 2,
        "bed_count": 1,
        "base_price": "120.00",
        "currency": "EUR",
    }


def amenity_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"code": "WIFI", "name": "Wireless internet"}
    body.update(overrides)
    return body


def assign_url(hotel_public_id: str, room_type_code: str) -> str:
    return f"/api/v1/hotels/{hotel_public_id}/room-types/{room_type_code}/amenities"


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    # Stage 4.2: every hotel-scoped endpoint requires an authenticated MEMBER, so
    # the suite's client carries a token. `POST /hotels` grants its creator `owner`,
    # which is why suites that build their own hotels need nothing further.
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels, amenities RESTART IDENTITY CASCADE"))
        cleanup.commit()


@pytest.fixture
def hotel_id(api: TestClient) -> str:
    """A hotel with one room type, DLX."""
    public_id = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"])
    api.post(f"/api/v1/hotels/{public_id}/room-types", json=type_payload("DLX"))
    return public_id


@pytest.fixture
def catalogue(session: Session) -> AmenityService:
    """The amenity service, driven directly against live PostgreSQL.

    Stage 4.2 closed the catalogue write ROUTES, not the write behaviour: upcasing, duplicate
    detection and the RESTRICT refusal still have to be right, because the out-of-band
    maintenance that replaces those routes runs through this same service. Testing it here
    keeps that coverage rather than retiring it with the endpoint.
    """
    return AmenityService(session, AmenityRepository(session))


def make_amenity(catalogue: AmenityService, **overrides: object) -> AmenityResponse:
    """Create a catalogue entry the way a real installation now does: out of band."""
    return catalogue.create(AmenityCreate(**amenity_payload(**overrides)))


# --- the catalogue is read-only over HTTP -------------------------------------------------
#
# Stage 4.2. `amenities` has no `hotel_id`: one row is shared by every property. A role is a
# per-hotel grant, so no role can confer authority over a resource that belongs to no hotel --
# an owner at one property renaming "WIFI" would be renaming it in every other property's
# listing. POST/PATCH/DELETE are refused to every authenticated caller.
#
# The tests that drove those routes are not deleted. The behaviour they covered still runs,
# through the same service, whenever the catalogue is maintained out of band; they now drive
# that service directly against live PostgreSQL, below.


@pytest.mark.parametrize(
    ("method", "url", "payload"),
    [
        ("POST", AMENITIES, {"code": "POOL", "name": "Pool"}),
        ("PATCH", f"{AMENITIES}/WIFI", {"name": "Renamed"}),
        ("DELETE", f"{AMENITIES}/WIFI", None),
    ],
)
def test_every_catalogue_write_route_is_refused(
    api: TestClient,
    catalogue: AmenityService,
    method: str,
    url: str,
    payload: dict[str, object] | None,
) -> None:
    make_amenity(catalogue)

    response = api.request(method, url, json=payload)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_a_refused_catalogue_write_changes_nothing(
    api: TestClient, catalogue: AmenityService, session: Session
) -> None:
    """The refusal happens before the service, so the row must be untouched afterwards."""
    make_amenity(catalogue, category="technology")

    api.patch(f"{AMENITIES}/WIFI", json={"name": "Renamed", "category": None})
    api.delete(f"{AMENITIES}/WIFI")

    stored = session.scalars(sa.select(Amenity)).one()
    assert (stored.code, stored.name, stored.category) == (
        "WIFI",
        "Wireless internet",
        "technology",
    )


def test_the_catalogue_refusal_names_no_role(api: TestClient) -> None:
    """No role helps here, so the message must not name one and invite a privilege hunt."""
    text = api.post(AMENITIES, json=amenity_payload()).text.lower()

    for leak in ["owner", "manager", "staff", "viewer", "membership", "user_hotels"]:
        assert leak not in text, f"leaked {leak!r}"


# --- 1-4, 8. catalogue reads over HTTP, catalogue writes at the service boundary -----------


def test_creating_an_amenity_returns_the_stored_row(catalogue: AmenityService) -> None:
    created = make_amenity(catalogue, category="technology")

    assert created.code == "WIFI"
    assert created.name == "Wireless internet"
    assert created.category == "technology"


def test_created_amenity_is_persisted(catalogue: AmenityService, session: Session) -> None:
    make_amenity(catalogue)

    assert session.scalars(sa.select(Amenity)).one().code == "WIFI"


def test_create_normalises_a_lowercase_code(catalogue: AmenityService) -> None:
    assert make_amenity(catalogue, code="wifi").code == "WIFI"


def test_category_is_optional(catalogue: AmenityService) -> None:
    assert make_amenity(catalogue).category is None


def test_get_amenity(api: TestClient, catalogue: AmenityService) -> None:
    make_amenity(catalogue)

    response = api.get(f"{AMENITIES}/WIFI")

    assert response.status_code == 200
    assert response.json()["code"] == "WIFI"


def test_get_is_case_insensitive(api: TestClient, catalogue: AmenityService) -> None:
    make_amenity(catalogue)

    assert api.get(f"{AMENITIES}/wifi").status_code == 200


def test_list_amenities_uses_the_shared_envelope(
    api: TestClient, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    body = api.get(AMENITIES).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] == 1


def test_list_is_ordered_by_code_deterministically(
    api: TestClient, catalogue: AmenityService
) -> None:
    for code in ["SPA", "AIRCON", "WIFI"]:
        make_amenity(catalogue, code=code, name=code.title())

    codes = [item["code"] for item in api.get(AMENITIES).json()["items"]]

    assert codes == ["AIRCON", "SPA", "WIFI"]
    assert codes == [item["code"] for item in api.get(AMENITIES).json()["items"]]


def test_list_paginates(api: TestClient, catalogue: AmenityService) -> None:
    for index in range(5):
        make_amenity(catalogue, code=f"AM{index}", name=f"Amenity {index}")

    first = api.get(AMENITIES, params={"page": 1, "page_size": 2}).json()
    third = api.get(AMENITIES, params={"page": 3, "page_size": 2}).json()

    assert [first["total"], first["pages"]] == [5, 3]
    assert len(third["items"]) == 1


def test_update_amenity_partially(catalogue: AmenityService) -> None:
    make_amenity(catalogue, category="technology")

    updated = catalogue.update("WIFI", AmenityUpdate(name="Fast WiFi"))

    assert updated.name == "Fast WiFi"
    assert updated.category == "technology"  # preserved
    assert updated.code == "WIFI"


def test_update_can_null_the_category(catalogue: AmenityService) -> None:
    make_amenity(catalogue, category="technology")

    assert catalogue.update("WIFI", AmenityUpdate(category=None)).category is None


def test_code_cannot_be_changed_through_patch() -> None:
    """The code is the URL identity; renaming it would break every link pointing at it.

    Rejected by the payload itself, which is why this one needs no database.
    """
    with pytest.raises(PydanticValidationError):
        AmenityUpdate(code="NEW")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("field", "value"),
    [("code", ""), ("code", "has space"), ("name", ""), ("category", "")],
)
def test_invalid_payload_is_rejected(field: str, value: object) -> None:
    with pytest.raises(PydanticValidationError):
        AmenityCreate(**amenity_payload(**{field: value}))


def test_internal_fields_are_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        AmenityCreate(**amenity_payload(id=1))


# --- 5. uniqueness -------------------------------------------------------------------------------


def test_duplicate_code_is_a_conflict(catalogue: AmenityService) -> None:
    make_amenity(catalogue)

    with pytest.raises(ConflictError) as raised:
        make_amenity(catalogue, name="Different name")

    assert "WIFI" in str(raised.value)


def test_duplicate_leaks_no_sql_or_constraint_name(catalogue: AmenityService) -> None:
    make_amenity(catalogue)

    with pytest.raises(ConflictError) as raised:
        make_amenity(catalogue)

    text = str(raised.value).lower()
    for leak in ["insert", "uq_amenities_code", "psycopg", "sqlalchemy", "traceback", "23505"]:
        assert leak not in text, f"leaked {leak!r}"


def test_names_need_not_be_unique(catalogue: AmenityService) -> None:
    """Only `code` carries a unique constraint."""
    make_amenity(catalogue, code="WIFI", name="Internet")

    assert make_amenity(catalogue, code="WIRED", name="Internet").code == "WIRED"


# --- 6-7. delete and missing ---------------------------------------------------------------------


def test_delete_unassigned_amenity_succeeds(api: TestClient, catalogue: AmenityService) -> None:
    make_amenity(catalogue)

    catalogue.delete("WIFI")

    assert api.get(f"{AMENITIES}/WIFI").status_code == 404


def test_missing_amenity_returns_404(api: TestClient) -> None:
    response = api.get(f"{AMENITIES}/NOSUCH")
    body = response.json()

    assert response.status_code == 404
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "Amenity not found."


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_mutating_a_missing_amenity_is_not_found(catalogue: AmenityService, operation: str) -> None:
    with pytest.raises(NotFoundError):
        if operation == "update":
            catalogue.update("NOSUCH", AmenityUpdate(name="x"))
        else:
            catalogue.delete("NOSUCH")


# --- 10. identifier round-trip -------------------------------------------------------------------


def test_url_rebuilt_from_the_response_resolves(api: TestClient, catalogue: AmenityService) -> None:
    created = make_amenity(catalogue)

    assert api.get(f"{AMENITIES}/{created.code}").status_code == 200


# --- 11-13. assignment ---------------------------------------------------------------------------
#
# Assignment is HOTEL-scoped -- `room_type_amenities` has a hotel_id, unlike `amenities` -- so
# these routes are open to a manager and are exercised over HTTP exactly as before. Only the
# catalogue entry they point at is now created out of band.


def test_assign_amenity_to_room_type_returns_201(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)

    response = api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    assert response.status_code == 201
    assert response.json()["code"] == "WIFI"


def test_assignment_is_persisted(
    api: TestClient, hotel_id: str, session: Session, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    assert session.scalar(sa.select(sa.func.count()).select_from(RoomTypeAmenity)) == 1


def test_list_assigned_amenities(api: TestClient, hotel_id: str, catalogue: AmenityService) -> None:
    for code in ["WIFI", "SPA"]:
        make_amenity(catalogue, code=code, name=code.title())
        api.post(assign_url(hotel_id, "DLX"), json={"code": code})

    body = api.get(assign_url(hotel_id, "DLX")).json()

    assert [item["code"] for item in body["items"]] == ["SPA", "WIFI"]
    assert body["total"] == 2


def test_listing_assignments_excludes_unassigned_catalogue_entries(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    make_amenity(catalogue, code="WIFI", name="WiFi")
    make_amenity(catalogue, code="SPA", name="Spa")
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    assert [i["code"] for i in api.get(assign_url(hotel_id, "DLX")).json()["items"]] == ["WIFI"]
    # The catalogue itself still holds both.
    assert api.get(AMENITIES).json()["total"] == 2


def test_remove_assignment_returns_204(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    response = api.delete(f"{assign_url(hotel_id, 'DLX')}/WIFI")

    assert response.status_code == 204
    assert api.get(assign_url(hotel_id, "DLX")).json()["total"] == 0


def test_removing_an_unassigned_amenity_returns_404(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)

    response = api.delete(f"{assign_url(hotel_id, 'DLX')}/WIFI")

    assert response.status_code == 404
    assert "not assigned" in response.json()["error"]["message"]


# --- 14. duplicate assignment --------------------------------------------------------------------


def test_duplicate_assignment_returns_409(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    response = api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "already assigned" in body["error"]["message"]


def test_duplicate_assignment_creates_no_second_row(
    api: TestClient, hotel_id: str, session: Session, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    assert session.scalar(sa.select(sa.func.count()).select_from(RoomTypeAmenity)) == 1


# --- 15-18. missing parents and cross-hotel isolation --------------------------------------------


def test_assigning_a_missing_amenity_returns_404(api: TestClient, hotel_id: str) -> None:
    response = api.post(assign_url(hotel_id, "DLX"), json={"code": "NOSUCH"})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Amenity not found."


def test_assigning_under_a_missing_room_type_returns_404(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)

    response = api.post(assign_url(hotel_id, "NOSUCH"), json={"code": "WIFI"})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Room type not found for this hotel."


def test_assigning_under_a_missing_hotel_returns_404(
    api: TestClient, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)

    response = api.post(assign_url(str(uuid.uuid4()), "DLX"), json={"code": "WIFI"})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


def test_a_missing_hotel_is_reported_before_a_missing_room_type(api: TestClient) -> None:
    response = api.get(assign_url(str(uuid.uuid4()), "NOSUCH"))

    assert response.json()["error"]["message"] == "Hotel not found."


def test_cross_hotel_room_type_is_not_addressable(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    """Hotel B has no DLX; hotel A does. B/DLX must not reach A's room type."""
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    make_amenity(catalogue)

    assert api.post(assign_url(other, "DLX"), json={"code": "WIFI"}).status_code == 404
    assert api.get(assign_url(other, "DLX")).status_code == 404
    assert api.delete(f"{assign_url(other, 'DLX')}/WIFI").status_code == 404


def test_a_cross_hotel_assignment_attempt_modifies_nothing(
    api: TestClient, hotel_id: str, session: Session, catalogue: AmenityService
) -> None:
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    api.post(assign_url(other, "DLX"), json={"code": "WIFI"})

    # Still exactly the one assignment, belonging to hotel A.
    assert session.scalar(sa.select(sa.func.count()).select_from(RoomTypeAmenity)) == 1


def test_each_hotels_assignments_are_listed_separately(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    """Two hotels, each with a DLX, sharing the same catalogue entry."""
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    api.post(f"/api/v1/hotels/{other}/room-types", json=type_payload("DLX"))
    make_amenity(catalogue)

    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    assert api.get(assign_url(hotel_id, "DLX")).json()["total"] == 1
    assert api.get(assign_url(other, "DLX")).json()["total"] == 0


def test_removing_one_hotels_assignment_leaves_the_others(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    api.post(f"/api/v1/hotels/{other}/room-types", json=type_payload("DLX"))
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})
    api.post(assign_url(other, "DLX"), json={"code": "WIFI"})

    api.delete(f"{assign_url(hotel_id, 'DLX')}/WIFI")

    assert api.get(assign_url(hotel_id, "DLX")).json()["total"] == 0
    assert api.get(assign_url(other, "DLX")).json()["total"] == 1


# --- 19-21. the catalogue survives; RESTRICT is honoured -----------------------------------------


def test_removing_an_assignment_does_not_delete_the_amenity(
    api: TestClient, hotel_id: str, session: Session, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    api.delete(f"{assign_url(hotel_id, 'DLX')}/WIFI")

    assert api.get(f"{AMENITIES}/WIFI").status_code == 200
    assert session.scalar(sa.select(sa.func.count()).select_from(Amenity)) == 1


def test_deleting_an_assigned_amenity_is_a_conflict(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    """room_type_amenities.amenity_id is ON DELETE RESTRICT."""
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    with pytest.raises(ConflictError) as raised:
        catalogue.delete("WIFI")

    assert "cannot be deleted" in str(raised.value)


def test_a_refused_amenity_delete_removes_nothing(
    api: TestClient, hotel_id: str, session: Session, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    with pytest.raises(ConflictError):
        catalogue.delete("WIFI")

    assert session.scalar(sa.select(sa.func.count()).select_from(Amenity)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(RoomTypeAmenity)) == 1


def test_restrict_refusal_leaks_no_constraint_or_sql(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    with pytest.raises(ConflictError) as raised:
        catalogue.delete("WIFI")

    text = str(raised.value).lower()
    for leak in [
        "fk_room_type_amenities_amenity_id_amenities",
        "delete from",
        "psycopg",
        "sqlalchemy",
        "restrictviolation",
        "traceback",
        "detail:",
        "23001",
    ]:
        assert leak not in text, f"leaked {leak!r}"


def test_the_amenity_becomes_deletable_once_unassigned(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})
    with pytest.raises(ConflictError):
        catalogue.delete("WIFI")

    api.delete(f"{assign_url(hotel_id, 'DLX')}/WIFI")

    catalogue.delete("WIFI")
    assert api.get(f"{AMENITIES}/WIFI").status_code == 404


def test_an_amenity_is_reusable_across_room_types(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    """The whole point of a shared catalogue."""
    api.post(f"/api/v1/hotels/{hotel_id}/room-types", json=type_payload("STD"))
    make_amenity(catalogue)

    assert api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"}).status_code == 201
    assert api.post(assign_url(hotel_id, "STD"), json={"code": "WIFI"}).status_code == 201
    assert api.get(assign_url(hotel_id, "DLX")).json()["total"] == 1
    assert api.get(assign_url(hotel_id, "STD")).json()["total"] == 1


def test_deleting_a_room_type_cascades_its_assignments_only(
    api: TestClient, hotel_id: str, session: Session, catalogue: AmenityService
) -> None:
    """room_type_amenities.room_type_id is ON DELETE CASCADE, so a room type with amenities
    is still deletable -- unlike one with rooms. The amenity itself survives."""
    make_amenity(catalogue)
    api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"})

    assert api.delete(f"/api/v1/hotels/{hotel_id}/room-types/DLX").status_code == 204

    assert session.scalar(sa.select(sa.func.count()).select_from(RoomTypeAmenity)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(Amenity)) == 1


# --- 22. round-trip and envelope -----------------------------------------------------------------


def test_assignment_url_rebuilt_from_the_response_resolves(
    api: TestClient, hotel_id: str, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)
    body = api.post(assign_url(hotel_id, "DLX"), json={"code": "WIFI"}).json()

    assert api.delete(f"{assign_url(hotel_id, 'DLX')}/{body['code']}").status_code == 204


def test_response_schema_is_exactly_the_declared_contract(
    api: TestClient, catalogue: AmenityService
) -> None:
    make_amenity(catalogue, category="technology")

    body = api.get(f"{AMENITIES}/WIFI").json()

    assert set(body) == {"code", "name", "category"}


def test_response_exposes_no_internal_id_or_invented_timestamps(
    api: TestClient, catalogue: AmenityService
) -> None:
    make_amenity(catalogue)

    body = api.get(f"{AMENITIES}/WIFI").json()

    for forbidden in ["id", "amenity_id", "created_at", "updated_at"]:
        assert forbidden not in body


def test_errors_use_the_shared_envelope(api: TestClient) -> None:
    body = api.get(f"{AMENITIES}/NOSUCH").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
