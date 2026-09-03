"""Room domain against real PostgreSQL.

The two-level scoping is what needs proving here, and only the database can prove it: that
``UNIQUE (hotel_id, room_number)`` is per hotel rather than per room type, that the composite
foreign key refuses a room type from another hotel, and that ``ON DELETE RESTRICT`` on
``booking_rooms`` refuses a room with stay history. SQLite is not substituted.
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

from app.models import Booking, BookingRoom, BookingRoomNight, Guest, Hotel, Room, RoomType
from tests.integration.conftest import authenticated_client, requires_postgres

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "rooms@example.test"

pytestmark = requires_postgres


def hotel_payload(slug: str, **overrides: object) -> dict[str, object]:
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


def type_payload(code: str = "DLX", **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "code": code,
        "name": f"Type {code}",
        "max_occupancy": 3,
        "standard_occupancy": 2,
        "bed_count": 1,
        "base_price": "120.00",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


def room_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"room_number": "101"}
    body.update(overrides)
    return body


def rooms_url(hotel_public_id: str, code: str) -> str:
    return f"/api/v1/hotels/{hotel_public_id}/room-types/{code}/rooms"


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
    """A hotel with one room type, DLX."""
    public_id = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"])
    api.post(f"/api/v1/hotels/{public_id}/room-types", json=type_payload("DLX"))
    return public_id


def attach_reservation(session: Session, room: Room) -> None:
    """Give a room real stay history, so the RESTRICT policy has something to refuse.

    Bookings are a later stage, so the rows are created directly through the models. The
    night rows are mandatory: ``booking_rooms`` carries a DEFERRABLE constraint trigger that
    rejects an incomplete night set at COMMIT.
    """
    guest = Guest(hotel_id=room.hotel_id, first_name="Ada", last_name="Lovelace")
    session.add(guest)
    session.flush()

    check_in, check_out = dt.date(2026, 9, 1), dt.date(2026, 9, 3)
    booking = Booking(
        hotel_id=room.hotel_id,
        guest_id=guest.id,
        reference=f"BK-{uuid.uuid4().hex[:8]}",
        check_in_date=check_in,
        check_out_date=check_out,
        status="confirmed",
        total_amount=Decimal("240.00"),
        currency="EUR",
    )
    session.add(booking)
    session.flush()

    booking_room = BookingRoom(
        booking_id=booking.id,
        room_id=room.id,
        hotel_id=room.hotel_id,
        check_in_date=check_in,
        check_out_date=check_out,
        booking_status="confirmed",
    )
    session.add(booking_room)
    session.flush()

    for offset in range(2):
        session.add(
            BookingRoomNight(
                booking_room_id=booking_room.id,
                hotel_id=room.hotel_id,
                check_in_date=check_in,
                check_out_date=check_out,
                stay_date=check_in + dt.timedelta(days=offset),
                rate=Decimal("120.00"),
            )
        )
    session.commit()


# --- 1. create -------------------------------------------------------------------------------


def test_create_room_returns_201(api: TestClient, hotel_id: str) -> None:
    response = api.post(rooms_url(hotel_id, "DLX"), json=room_payload())
    body = response.json()

    assert response.status_code == 201
    assert body["room_number"] == "101"
    assert body["status"] == "available"
    assert body["is_active"] is True


def test_created_room_is_persisted_against_the_right_hotel_and_type(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    room = session.scalars(sa.select(Room)).one()
    hotel = session.scalars(sa.select(Hotel)).one()
    room_type = session.scalars(sa.select(RoomType)).one()
    assert room.hotel_id == hotel.id
    assert room.room_type_id == room_type.id


def test_create_accepts_optional_fields_and_every_schema_status(
    api: TestClient, hotel_id: str
) -> None:
    body = api.post(
        rooms_url(hotel_id, "DLX"),
        json=room_payload(floor=3, status="maintenance", notes="Radiator", is_active=False),
    ).json()

    assert body["floor"] == 3
    assert body["status"] == "maintenance"
    assert body["notes"] == "Radiator"
    assert body["is_active"] is False


def test_create_normalises_the_room_number(api: TestClient, hotel_id: str) -> None:
    body = api.post(rooms_url(hotel_id, "DLX"), json=room_payload(room_number="12a")).json()

    assert body["room_number"] == "12A"


def test_room_type_code_in_the_path_is_case_insensitive(api: TestClient, hotel_id: str) -> None:
    assert api.post(rooms_url(hotel_id, "dlx"), json=room_payload()).status_code == 201


# --- 18. validation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("room_number", ""),
        ("room_number", "has space"),
        ("status", "booked"),
        ("status", "dirty"),
        ("floor", 999),
    ],
)
def test_create_validation_failure_returns_422(
    api: TestClient, hotel_id: str, field: str, value: object
) -> None:
    response = api.post(rooms_url(hotel_id, "DLX"), json=room_payload(**{field: value}))
    body = response.json()

    assert response.status_code == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(field in d["location"] for d in body["error"]["details"])


def test_create_rejects_a_client_supplied_room_type(api: TestClient, hotel_id: str) -> None:
    """The hierarchy comes from the path and nowhere else."""
    assert (
        api.post(rooms_url(hotel_id, "DLX"), json=room_payload(room_type_id=1)).status_code == 422
    )


# --- 2-4. missing or mismatched parents ----------------------------------------------------------


def test_create_under_nonexistent_hotel_returns_404(api: TestClient) -> None:
    response = api.post(rooms_url(str(uuid.uuid4()), "DLX"), json=room_payload())

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


def test_create_under_nonexistent_room_type_returns_404(api: TestClient, hotel_id: str) -> None:
    response = api.post(rooms_url(hotel_id, "NOSUCH"), json=room_payload())

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Room type not found for this hotel."


def test_create_with_a_room_type_belonging_to_another_hotel_returns_404(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    """Hotel B has no STD; hotel A does. Addressing B/STD must not reach A's type."""
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    api.post(f"/api/v1/hotels/{hotel_id}/room-types", json=type_payload("STD"))

    response = api.post(rooms_url(other, "STD"), json=room_payload())

    assert response.status_code == 404
    assert session.scalar(sa.select(sa.func.count()).select_from(Room)) == 0


def test_a_missing_hotel_is_reported_before_a_missing_room_type(api: TestClient) -> None:
    """So a caller can tell which half of the path is wrong."""
    response = api.post(rooms_url(str(uuid.uuid4()), "NOSUCH"), json=room_payload())

    assert response.json()["error"]["message"] == "Hotel not found."


# --- 17. uniqueness ------------------------------------------------------------------------------


def test_duplicate_room_number_in_the_same_type_returns_409(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    response = api.post(rooms_url(hotel_id, "DLX"), json=room_payload())
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "101" in body["error"]["message"]


def test_room_numbers_are_unique_per_hotel_not_per_room_type(
    api: TestClient, hotel_id: str
) -> None:
    """The schema's rule, surfaced honestly: two room types at one property cannot both own
    room 101, even though their URLs read like separate collections."""
    api.post(f"/api/v1/hotels/{hotel_id}/room-types", json=type_payload("STD"))
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload(room_number="101"))

    response = api.post(rooms_url(hotel_id, "STD"), json=room_payload(room_number="101"))
    body = response.json()

    assert response.status_code == 409
    assert "unique per hotel" in body["error"]["message"]


def test_the_same_room_number_may_exist_at_two_different_hotels(api: TestClient) -> None:
    first = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"])
    second = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    for public_id in (first, second):
        api.post(f"/api/v1/hotels/{public_id}/room-types", json=type_payload("DLX"))

    assert api.post(rooms_url(first, "DLX"), json=room_payload()).status_code == 201
    assert api.post(rooms_url(second, "DLX"), json=room_payload()).status_code == 201


def test_duplicate_leaks_no_sql_or_constraint_name(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())
    text = api.post(rooms_url(hotel_id, "DLX"), json=room_payload()).text.lower()

    for leak in ["insert", "uq_rooms", "psycopg", "sqlalchemy", "traceback", "23505"]:
        assert leak not in text, f"leaked {leak!r}"


# --- 5-7. list, pagination, ordering -------------------------------------------------------------


def test_list_returns_the_shared_pagination_envelope(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())
    body = api.get(rooms_url(hotel_id, "DLX")).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] == 1


def test_list_is_empty_for_a_type_with_no_rooms(api: TestClient, hotel_id: str) -> None:
    body = api.get(rooms_url(hotel_id, "DLX")).json()

    assert body["items"] == []
    assert body["total"] == 0
    assert body["pages"] == 0


def test_list_under_nonexistent_room_type_returns_404_not_an_empty_page(
    api: TestClient, hotel_id: str
) -> None:
    assert api.get(rooms_url(hotel_id, "NOSUCH")).status_code == 404


def test_pagination_splits_results(api: TestClient, hotel_id: str) -> None:
    for index in range(5):
        api.post(rooms_url(hotel_id, "DLX"), json=room_payload(room_number=f"10{index}"))

    first = api.get(rooms_url(hotel_id, "DLX"), params={"page": 1, "page_size": 2}).json()
    third = api.get(rooms_url(hotel_id, "DLX"), params={"page": 3, "page_size": 2}).json()

    assert [first["total"], first["pages"]] == [5, 3]
    assert len(first["items"]) == 2
    assert len(third["items"]) == 1


def test_pages_do_not_overlap_or_skip(api: TestClient, hotel_id: str) -> None:
    for index in range(6):
        api.post(rooms_url(hotel_id, "DLX"), json=room_payload(room_number=f"10{index}"))

    seen: list[str] = []
    for page in (1, 2, 3):
        seen += [
            item["room_number"]
            for item in api.get(
                rooms_url(hotel_id, "DLX"), params={"page": page, "page_size": 2}
            ).json()["items"]
        ]

    assert len(seen) == len(set(seen)) == 6


def test_ordering_is_deterministic_by_room_number(api: TestClient, hotel_id: str) -> None:
    for number in ["103", "101", "102"]:
        api.post(rooms_url(hotel_id, "DLX"), json=room_payload(room_number=number))

    numbers = [i["room_number"] for i in api.get(rooms_url(hotel_id, "DLX")).json()["items"]]

    assert numbers == ["101", "102", "103"]
    assert numbers == [
        i["room_number"] for i in api.get(rooms_url(hotel_id, "DLX")).json()["items"]
    ]


def test_listing_is_scoped_to_the_room_type_in_the_path(api: TestClient, hotel_id: str) -> None:
    api.post(f"/api/v1/hotels/{hotel_id}/room-types", json=type_payload("STD"))
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload(room_number="101"))
    api.post(rooms_url(hotel_id, "STD"), json=room_payload(room_number="201"))

    assert [i["room_number"] for i in api.get(rooms_url(hotel_id, "DLX")).json()["items"]] == [
        "101"
    ]
    assert [i["room_number"] for i in api.get(rooms_url(hotel_id, "STD")).json()["items"]] == [
        "201"
    ]


def test_page_size_is_capped(api: TestClient, hotel_id: str) -> None:
    assert api.get(rooms_url(hotel_id, "DLX"), params={"page_size": 1000}).status_code == 422


# --- 8-11. get, and cross-hierarchy isolation ----------------------------------------------------


def test_get_existing_room(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    response = api.get(f"{rooms_url(hotel_id, 'DLX')}/101")

    assert response.status_code == 200
    assert response.json()["room_number"] == "101"


def test_get_nonexistent_room_returns_404(api: TestClient, hotel_id: str) -> None:
    response = api.get(f"{rooms_url(hotel_id, 'DLX')}/999")
    body = response.json()

    assert response.status_code == 404
    assert body["error"]["message"] == "Room not found for this hotel and room type."


def test_cross_hotel_access_returns_404(api: TestClient, hotel_id: str) -> None:
    """Hotel A -> DLX -> 101 must not be reachable through Hotel B -> DLX -> 101."""
    other = str(api.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    api.post(f"/api/v1/hotels/{other}/room-types", json=type_payload("DLX"))
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    assert api.get(f"{rooms_url(hotel_id, 'DLX')}/101").status_code == 200
    assert api.get(f"{rooms_url(other, 'DLX')}/101").status_code == 404
    assert api.patch(f"{rooms_url(other, 'DLX')}/101", json={"floor": 9}).status_code == 404
    assert api.delete(f"{rooms_url(other, 'DLX')}/101").status_code == 404


def test_cross_room_type_access_returns_404(api: TestClient, hotel_id: str) -> None:
    """Hotel A -> DLX -> 101 must not be reachable through Hotel A -> STD -> 101."""
    api.post(f"/api/v1/hotels/{hotel_id}/room-types", json=type_payload("STD"))
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    assert api.get(f"{rooms_url(hotel_id, 'DLX')}/101").status_code == 200
    assert api.get(f"{rooms_url(hotel_id, 'STD')}/101").status_code == 404
    assert api.patch(f"{rooms_url(hotel_id, 'STD')}/101", json={"floor": 9}).status_code == 404
    assert api.delete(f"{rooms_url(hotel_id, 'STD')}/101").status_code == 404


def test_a_failed_cross_hierarchy_update_changes_nothing(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(f"/api/v1/hotels/{hotel_id}/room-types", json=type_payload("STD"))
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload(floor=1))

    api.patch(f"{rooms_url(hotel_id, 'STD')}/101", json={"floor": 99})

    assert session.scalars(sa.select(Room)).one().floor == 1


def test_a_failed_cross_hierarchy_delete_removes_nothing(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(f"/api/v1/hotels/{hotel_id}/room-types", json=type_payload("STD"))
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    api.delete(f"{rooms_url(hotel_id, 'STD')}/101")

    assert session.scalar(sa.select(sa.func.count()).select_from(Room)) == 1


# --- 12-16. update -------------------------------------------------------------------------------


def test_partial_update_preserves_unspecified_fields(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload(floor=2, notes="Quiet"))

    updated = api.patch(f"{rooms_url(hotel_id, 'DLX')}/101", json={"status": "cleaning"}).json()

    assert updated["status"] == "cleaning"
    assert updated["floor"] == 2
    assert updated["notes"] == "Quiet"
    assert updated["room_number"] == "101"


def test_partial_update_persists(api: TestClient, hotel_id: str, session: Session) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())
    api.patch(f"{rooms_url(hotel_id, 'DLX')}/101", json={"status": "out_of_order"})

    assert session.scalars(sa.select(Room)).one().status == "out_of_order"


def test_update_can_explicitly_null_a_nullable_field(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload(notes="Quiet"))

    assert (
        api.patch(f"{rooms_url(hotel_id, 'DLX')}/101", json={"notes": None}).json()["notes"] is None
    )


def test_empty_update_is_a_no_op(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    response = api.patch(f"{rooms_url(hotel_id, 'DLX')}/101", json={})

    assert response.status_code == 200
    assert response.json()["room_number"] == "101"


def test_update_rejects_an_invented_status(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    assert (
        api.patch(f"{rooms_url(hotel_id, 'DLX')}/101", json={"status": "booked"}).status_code == 422
    )


def test_room_number_cannot_be_changed_through_patch(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    assert (
        api.patch(f"{rooms_url(hotel_id, 'DLX')}/101", json={"room_number": "202"}).status_code
        == 422
    )


def test_a_room_cannot_be_moved_between_types_through_patch(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    assert (
        api.patch(f"{rooms_url(hotel_id, 'DLX')}/101", json={"room_type_code": "STD"}).status_code
        == 422
    )


def test_update_nonexistent_room_returns_404(api: TestClient, hotel_id: str) -> None:
    assert api.patch(f"{rooms_url(hotel_id, 'DLX')}/999", json={"floor": 1}).status_code == 404


def test_deactivating_is_an_update_not_a_delete(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    assert (
        api.patch(f"{rooms_url(hotel_id, 'DLX')}/101", json={"is_active": False}).json()[
            "is_active"
        ]
        is False
    )


# --- 19-20. delete -------------------------------------------------------------------------------


def test_delete_room_without_dependencies_returns_204(api: TestClient, hotel_id: str) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    response = api.delete(f"{rooms_url(hotel_id, 'DLX')}/101")

    assert response.status_code == 204
    assert response.content == b""
    assert api.get(f"{rooms_url(hotel_id, 'DLX')}/101").status_code == 404


def test_delete_nonexistent_room_returns_404(api: TestClient, hotel_id: str) -> None:
    assert api.delete(f"{rooms_url(hotel_id, 'DLX')}/999").status_code == 404


def test_delete_with_dependent_reservations_returns_409_and_does_not_cascade(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    """booking_rooms.room_id is ON DELETE RESTRICT. The database stays the authority."""
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())
    attach_reservation(session, session.scalars(sa.select(Room)).one())

    response = api.delete(f"{rooms_url(hotel_id, 'DLX')}/101")
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "cannot be deleted" in body["error"]["message"]

    # Neither the room nor its reservation history was removed.
    assert session.scalar(sa.select(sa.func.count()).select_from(Room)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 2


def test_restrict_refusal_leaks_no_constraint_or_sql(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())
    attach_reservation(session, session.scalars(sa.select(Room)).one())

    text = api.delete(f"{rooms_url(hotel_id, 'DLX')}/101").text.lower()

    for leak in [
        "fk_booking_rooms_room_id_hotel_id_rooms",
        "delete from",
        "psycopg",
        "sqlalchemy",
        "restrictviolation",
        "traceback",
        "detail:",
        "23001",
    ]:
        assert leak not in text, f"leaked {leak!r}"


def test_room_remains_usable_after_a_refused_delete(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())
    attach_reservation(session, session.scalars(sa.select(Room)).one())

    api.delete(f"{rooms_url(hotel_id, 'DLX')}/101")

    assert api.get(f"{rooms_url(hotel_id, 'DLX')}/101").status_code == 200
    patched = api.patch(f"{rooms_url(hotel_id, 'DLX')}/101", json={"status": "maintenance"})
    assert patched.status_code == 200


def test_deleting_a_room_type_with_rooms_is_refused(api: TestClient, hotel_id: str) -> None:
    """The room-type RESTRICT from Stage 3B.2 still holds now that a child exists."""
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    response = api.delete(f"/api/v1/hotels/{hotel_id}/room-types/DLX")

    assert response.status_code == 409


def test_deleting_a_hotel_with_rooms_is_refused(api: TestClient, hotel_id: str) -> None:
    """And the hotel RESTRICT from Stage 3B.1 still holds through two levels."""
    api.post(rooms_url(hotel_id, "DLX"), json=room_payload())

    assert api.delete(f"/api/v1/hotels/{hotel_id}").status_code == 409


# --- 21-23. response schema, envelope, relationship ----------------------------------------------


def test_response_schema_is_exactly_the_declared_contract(api: TestClient, hotel_id: str) -> None:
    body = api.post(rooms_url(hotel_id, "DLX"), json=room_payload()).json()

    assert set(body) == {
        "hotel_public_id",
        "room_type_code",
        "room_number",
        "floor",
        "status",
        "notes",
        "is_active",
        "created_at",
        "updated_at",
    }


def test_response_never_exposes_internal_ids(api: TestClient, hotel_id: str) -> None:
    body = api.post(rooms_url(hotel_id, "DLX"), json=room_payload()).json()

    for forbidden in ["id", "hotel_id", "room_type_id", "public_id"]:
        assert forbidden not in body


def test_response_identifies_the_full_parent_chain(api: TestClient, hotel_id: str) -> None:
    """A client can rebuild the room's own URL from the payload alone."""
    body = api.post(rooms_url(hotel_id, "DLX"), json=room_payload()).json()

    assert body["hotel_public_id"] == hotel_id
    assert body["room_type_code"] == "DLX"
    rebuilt = (
        f"/api/v1/hotels/{body['hotel_public_id']}"
        f"/room-types/{body['room_type_code']}/rooms/{body['room_number']}"
    )
    assert api.get(rebuilt).status_code == 200


def test_errors_use_the_shared_envelope(api: TestClient, hotel_id: str) -> None:
    body = api.get(f"{rooms_url(hotel_id, 'DLX')}/999").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_timestamps_are_timezone_aware(api: TestClient, hotel_id: str) -> None:
    body = api.post(rooms_url(hotel_id, "DLX"), json=room_payload()).json()

    assert dt.datetime.fromisoformat(body["created_at"]).tzinfo is not None
