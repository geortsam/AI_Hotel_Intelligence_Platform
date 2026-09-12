"""Multi-room availability against real PostgreSQL.

Stage 4.5.25. The question is whether a hotel can supply N rooms of ONE type for a half-open
stay, and every part of the answer depends on a database the search does not own:

* the overlap test is the read-side mirror of the exclusion constraint, half-open bound for
  half-open bound, and only PostgreSQL can be asked whether the two agree;
* which statuses hold inventory is a property of allocations that exist, so the fixtures make
  real bookings through the real endpoint rather than asserting about a list;
* the isolation claims -- another hotel's rooms, another type's rooms -- are claims about what
  a query did NOT return, which is only meaningful when the excluded rows are really there.

**An availability answer is not a reservation**, and one test below says so by searching twice
and then booking: nothing is held between the two, and the second caller is told the same
thing as the first.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import authenticated_client, requires_postgres

SUITE_EMAIL = "multiroom@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 10, 10)
CHECK_OUT = dt.date(2026, 10, 15)


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


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def add_room_type(api: TestClient, hotel: str, code: str, *, max_occupancy: int = 4) -> None:
    created = api.post(
        f"/api/v1/hotels/{hotel}/room-types",
        json={
            "code": code,
            "name": f"Type {code}",
            "max_occupancy": max_occupancy,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": "120.00",
            "currency": "EUR",
        },
    )
    assert created.status_code == 201, created.text


def add_rooms(api: TestClient, hotel: str, code: str, numbers: tuple[str, ...]) -> None:
    for number in numbers:
        created = api.post(
            f"/api/v1/hotels/{hotel}/room-types/{code}/rooms", json={"room_number": number}
        )
        assert created.status_code == 201, created.text


def build_hotel(
    api: TestClient,
    slug: str,
    *,
    rooms: tuple[str, ...] = ("101", "102", "103"),
    code: str = "DLX",
) -> str:
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    add_room_type(api, hotel, code)
    add_rooms(api, hotel, code, rooms)
    return hotel


def book(
    api: TestClient,
    hotel: str,
    room_number: str,
    *,
    check_in: dt.date = CHECK_IN,
    check_out: dt.date = CHECK_OUT,
    status: str = "confirmed",
) -> str:
    """Allocate one room through the real booking endpoint."""
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    nights = (check_out - check_in).days
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(check_in),
            "check_out_date": str(check_out),
            "status": status,
            "total_amount": "100.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": room_number,
                    "nights": [
                        {"stay_date": str(check_in + dt.timedelta(days=n))} for n in range(nights)
                    ],
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def search(
    api: TestClient,
    hotel: str,
    *,
    rooms_required: int | None = None,
    check_in: dt.date = CHECK_IN,
    check_out: dt.date = CHECK_OUT,
    **extra: object,
) -> Response:
    params: dict[str, object] = {"check_in": str(check_in), "check_out": str(check_out)}
    if rooms_required is not None:
        params["rooms_required"] = rooms_required
    params.update(extra)
    return api.get(f"/api/v1/hotels/{hotel}/availability", params=params)


def answer(response: Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return dict(response.json())


def count_for(body: dict, code: str = "DLX") -> int:
    for entry in body["room_types"]:
        if entry["code"] == code:
            return int(entry["available_count"])
    return 0


# ======================================================================================
# A. One room required -- the question this endpoint always answered
# ======================================================================================


def test_the_default_request_is_for_one_room(api: TestClient) -> None:
    hotel = build_hotel(api, "mr-default")

    body = answer(search(api, hotel))

    assert body["rooms_required"] == 1
    assert body["sufficient"] is True
    assert body["available_rooms"] == 3


def test_asking_for_one_room_explicitly_is_the_same_answer(api: TestClient) -> None:
    """The new parameter's default must not be a new behaviour."""
    hotel = build_hotel(api, "mr-explicit-one")

    implicit = answer(search(api, hotel))
    explicit = answer(search(api, hotel, rooms_required=1))

    assert implicit["room_types"] == explicit["room_types"]
    assert implicit["available_rooms"] == explicit["available_rooms"]


# ======================================================================================
# B, C, L. Counting rooms
# ======================================================================================


@pytest.mark.parametrize("required", [1, 2, 3])
def test_a_hotel_with_three_free_rooms_can_supply_up_to_three(
    api: TestClient, required: int
) -> None:
    hotel = build_hotel(api, f"mr-supply-{required}")

    body = answer(search(api, hotel, rooms_required=required))

    assert body["sufficient"] is True
    assert body["rooms_required"] == required
    assert count_for(body) == 3


def test_a_hotel_with_three_free_rooms_cannot_supply_four(api: TestClient) -> None:
    hotel = build_hotel(api, "mr-supply-four")

    body = answer(search(api, hotel, rooms_required=4))

    assert body["sufficient"] is False
    assert body["room_types"] == []
    assert body["available_rooms"] == 0


def test_exactly_enough_is_enough(api: TestClient) -> None:
    """The boundary of the comparison: ``available_count >= rooms_required``."""
    hotel = build_hotel(api, "mr-exact", rooms=("101", "102"))

    enough = answer(search(api, hotel, rooms_required=2))
    one_too_many = answer(search(api, hotel, rooms_required=3))

    assert enough["sufficient"] is True
    assert count_for(enough) == 2
    assert one_too_many["sufficient"] is False


# ======================================================================================
# D. Overlapping bookings reduce the count
# ======================================================================================


def test_one_overlapping_booking_reduces_the_count_by_one(api: TestClient) -> None:
    hotel = build_hotel(api, "mr-one-booked")
    book(api, hotel, "101")

    body = answer(search(api, hotel, rooms_required=2))

    assert count_for(body) == 2
    assert {room["room_number"] for room in body["room_types"][0]["rooms"]} == {"102", "103"}


def test_two_overlapping_bookings_reduce_the_count_by_two(api: TestClient) -> None:
    hotel = build_hotel(api, "mr-two-booked")
    book(api, hotel, "101")
    book(api, hotel, "102")

    enough_for_one = answer(search(api, hotel, rooms_required=1))
    not_enough_for_two = answer(search(api, hotel, rooms_required=2))

    assert count_for(enough_for_one) == 1
    assert not_enough_for_two["sufficient"] is False


def test_a_booking_that_takes_the_last_room_makes_the_request_impossible(
    api: TestClient,
) -> None:
    hotel = build_hotel(api, "mr-last-room", rooms=("101",))
    book(api, hotel, "101")

    body = answer(search(api, hotel, rooms_required=1))

    assert body["sufficient"] is False
    assert body["available_rooms"] == 0


# ======================================================================================
# E. Only inventory-holding statuses reduce availability
# ======================================================================================


@pytest.mark.parametrize("status", ["confirmed", "checked_in"])
def test_an_inventory_holding_status_reduces_the_count(api: TestClient, status: str) -> None:
    hotel = build_hotel(api, f"mr-holds-{status.replace(chr(95), chr(45))}")
    booking = book(api, hotel, "101")
    if status == "checked_in":
        moved = api.patch(
            f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_in"}
        )
        assert moved.status_code == 200, moved.text

    body = answer(search(api, hotel, rooms_required=3))

    assert body["sufficient"] is False


@pytest.mark.parametrize("status", ["pending", "cancelled", "no_show", "checked_out"])
def test_a_status_that_holds_no_inventory_leaves_the_count_alone(
    api: TestClient, status: str
) -> None:
    """``INVENTORY_HOLDING_STATUSES`` is confirmed and checked_in, and nothing else.

    The exclusion constraint's own WHERE clause is generated from that same constant, so a
    search that disagreed with it would be a room sold twice or a room never sold.
    """
    hotel = build_hotel(api, f"mr-free-{status.replace(chr(95), chr(45))}")
    if status == "pending":
        book(api, hotel, "101", status="pending")
    else:
        booking = book(api, hotel, "101")
        if status == "checked_out":
            api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_in"})
        api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": status})

    body = answer(search(api, hotel, rooms_required=3))

    assert body["sufficient"] is True, f"{status} should not hold inventory"
    assert count_for(body) == 3


# ======================================================================================
# F. Half-open interval boundaries
# ======================================================================================


def test_a_stay_ending_when_ours_begins_does_not_collide(api: TestClient) -> None:
    """``[check_in, check_out)``: the departure day sells no night, so it is free."""
    hotel = build_hotel(api, "mr-touch-before")
    book(api, hotel, "101", check_in=CHECK_IN - dt.timedelta(days=3), check_out=CHECK_IN)

    body = answer(search(api, hotel, rooms_required=3))

    assert body["sufficient"] is True
    assert count_for(body) == 3


def test_a_stay_beginning_when_ours_ends_does_not_collide(api: TestClient) -> None:
    hotel = build_hotel(api, "mr-touch-after")
    book(api, hotel, "101", check_in=CHECK_OUT, check_out=CHECK_OUT + dt.timedelta(days=3))

    body = answer(search(api, hotel, rooms_required=3))

    assert body["sufficient"] is True
    assert count_for(body) == 3


def test_a_stay_overlapping_by_a_single_night_does_collide(api: TestClient) -> None:
    """One night of overlap is overlap. The pair with the test above is the whole boundary."""
    hotel = build_hotel(api, "mr-touch-one")
    book(
        api,
        hotel,
        "101",
        check_in=CHECK_IN - dt.timedelta(days=3),
        check_out=CHECK_IN + dt.timedelta(days=1),
    )

    body = answer(search(api, hotel, rooms_required=3))

    assert body["sufficient"] is False
    # The count is read from an unfiltered search: a type that cannot supply the request
    # is omitted from the filtered one, so it has no count there to read.
    assert count_for(answer(search(api, hotel, rooms_required=1))) == 2


def test_an_identical_stay_collides(api: TestClient) -> None:
    hotel = build_hotel(api, "mr-exact-overlap")
    book(api, hotel, "101")

    assert answer(search(api, hotel, rooms_required=3))["sufficient"] is False


# ======================================================================================
# G. Room-type isolation -- N rooms of ONE type
# ======================================================================================


def test_rooms_of_another_type_do_not_make_up_the_number(api: TestClient) -> None:
    """The stage's central semantic. Two Deluxe and two Suite rooms do not answer a request
    for three of one type; mixing types is a different question and is not answered here.
    """
    hotel = build_hotel(api, "mr-types", rooms=("101", "102"))
    add_room_type(api, hotel, "SUI")
    add_rooms(api, hotel, "SUI", ("201", "202"))

    body = answer(search(api, hotel, rooms_required=3))

    assert body["sufficient"] is False
    assert body["room_types"] == []
    assert answer(search(api, hotel, rooms_required=2))["sufficient"] is True


def test_only_the_types_that_can_supply_the_request_are_listed(api: TestClient) -> None:
    hotel = build_hotel(api, "mr-listing", rooms=("101", "102", "103"))
    add_room_type(api, hotel, "SUI")
    add_rooms(api, hotel, "SUI", ("201",))

    body = answer(search(api, hotel, rooms_required=2))

    assert [entry["code"] for entry in body["room_types"]] == ["DLX"]
    assert body["available_rooms"] == 3
    # Every listed type can supply the request by construction -- that is what the
    # filter means, so the fact is asserted here rather than carried as a field that
    # could only ever be true.
    assert all(entry["available_count"] >= 2 for entry in body["room_types"])


def test_the_room_type_filter_still_narrows_to_one_type(api: TestClient) -> None:
    hotel = build_hotel(api, "mr-filter", rooms=("101", "102", "103"))
    add_room_type(api, hotel, "SUI")
    add_rooms(api, hotel, "SUI", ("201", "202", "203"))

    body = answer(search(api, hotel, rooms_required=3, room_type_code="SUI"))

    assert [entry["code"] for entry in body["room_types"]] == ["SUI"]
    assert body["sufficient"] is True


# ======================================================================================
# H. Hotel isolation
# ======================================================================================


def test_another_hotels_rooms_do_not_make_up_the_number(api: TestClient) -> None:
    """Two properties, three rooms each, same room-type code and same room numbers. A search
    at one must count only its own three."""
    first = build_hotel(api, "mr-tenant-a", rooms=("101", "102"))
    build_hotel(api, "mr-tenant-b", rooms=("101", "102", "103", "104"))

    body = answer(search(api, first, rooms_required=3))

    assert body["sufficient"] is False
    assert answer(search(api, first, rooms_required=2))["available_rooms"] == 2


def test_another_hotels_bookings_do_not_reduce_this_hotels_count(api: TestClient) -> None:
    """The other direction, and the one a lost predicate would break silently."""
    first = build_hotel(api, "mr-cross-a")
    second = build_hotel(api, "mr-cross-b")
    book(api, second, "101")
    book(api, second, "102")

    body = answer(search(api, first, rooms_required=3))

    assert body["sufficient"] is True
    assert count_for(body) == 3


def test_a_non_member_cannot_search_a_hotels_inventory(api: TestClient, engine: Engine) -> None:
    hotel = build_hotel(api, "mr-stranger")
    stranger = authenticated_client(engine, email="mr-stranger@example.test")

    refused = stranger.get(
        f"/api/v1/hotels/{hotel}/availability",
        params={"check_in": str(CHECK_IN), "check_out": str(CHECK_OUT), "rooms_required": 2},
    )
    invented = stranger.get(
        f"/api/v1/hotels/{uuid.uuid4()}/availability",
        params={"check_in": str(CHECK_IN), "check_out": str(CHECK_OUT), "rooms_required": 2},
    )

    assert refused.status_code == 404
    assert refused.json() == invented.json()


# ======================================================================================
# I. Room status
# ======================================================================================


@pytest.mark.parametrize("status", ["maintenance", "out_of_order"])
def test_a_room_out_of_service_does_not_count(api: TestClient, status: str) -> None:
    hotel = build_hotel(api, f"mr-status-{status.replace(chr(95), chr(45))}")
    moved = api.patch(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms/101", json={"status": status})
    assert moved.status_code == 200, moved.text

    body = answer(search(api, hotel, rooms_required=3))

    assert body["sufficient"] is False
    assert count_for(answer(search(api, hotel, rooms_required=1))) == 2


# ======================================================================================
# J. Validation
# ======================================================================================


@pytest.mark.parametrize("value", [0, -1, 101, 100000])
def test_an_out_of_range_room_count_is_refused(api: TestClient, value: int) -> None:
    hotel = build_hotel(api, f"mr-invalid-{abs(value)}")

    response = search(api, hotel, rooms_required=value)

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}


def test_the_largest_permitted_request_is_accepted(api: TestClient) -> None:
    """100 is answerable -- with `false`, which is an answer and not an error."""
    hotel = build_hotel(api, "mr-max")

    body = answer(search(api, hotel, rooms_required=100))

    assert body["sufficient"] is False


def test_a_rejected_room_count_echoes_no_internals(api: TestClient) -> None:
    hotel = build_hotel(api, "mr-leak")

    rendered = str(search(api, hotel, rooms_required=0).json())

    # "rooms_required" is the parameter the caller sent; naming it is the point of a
    # validation error. What must not appear is anything internal.
    for forbidden in ["SELECT", "room_type_id", "hotel_id", "sqlalchemy", "psycopg", "Room."]:
        assert forbidden not in rendered, f"the refusal leaked {forbidden!r}"


# ======================================================================================
# K. Empty inventory
# ======================================================================================


def test_a_hotel_with_no_rooms_supplies_nothing(api: TestClient) -> None:
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload("mr-empty")).json()["public_id"])
    add_room_type(api, hotel, "DLX")

    body = answer(search(api, hotel, rooms_required=1))

    assert body["sufficient"] is False
    assert body["available_rooms"] == 0
    assert body["room_types"] == []


# ======================================================================================
# Availability is advisory, not a reservation
# ======================================================================================


def test_a_search_reserves_nothing(api: TestClient, session: Session) -> None:
    """Two identical searches, and no allocation between them.

    The point is not that the numbers match -- it is that the first caller was given no claim
    on anything. Nothing was written, so the second caller is told exactly the same thing.
    """
    hotel = build_hotel(api, "mr-advisory")
    session.rollback()
    before = session.execute(sa.text("SELECT count(*) FROM booking_rooms")).scalar()

    first = answer(search(api, hotel, rooms_required=3))
    second = answer(search(api, hotel, rooms_required=3))

    assert first["available_rooms"] == second["available_rooms"] == 3
    session.rollback()
    assert session.execute(sa.text("SELECT count(*) FROM booking_rooms")).scalar() == before


def test_an_answer_can_be_overtaken_by_a_booking(api: TestClient) -> None:
    """Availability is a point-in-time search result, and this is what that costs.

    The search says three rooms are free; a booking then takes one; the same search now says
    two. Nothing was violated -- the first answer was true when it was given, and the booking
    transaction, not the search, is what decides inventory.
    """
    hotel = build_hotel(api, "mr-overtaken")
    assert answer(search(api, hotel, rooms_required=3))["sufficient"] is True

    book(api, hotel, "101")

    assert answer(search(api, hotel, rooms_required=3))["sufficient"] is False
    assert answer(search(api, hotel, rooms_required=2))["sufficient"] is True


# ======================================================================================
# Performance
# ======================================================================================


def test_the_search_costs_the_same_however_much_inventory_there_is(
    api: TestClient, engine: Engine
) -> None:
    """Set-based, and the assertion is that the count does not move with the data.

    Twelve rooms and eight overlapping bookings are searched with the same number of
    statements as three rooms and none. An N+1 implementation would grow with both.
    """
    small = build_hotel(api, "mr-perf-small", rooms=("101",))
    large = build_hotel(api, "mr-perf-large", rooms=tuple(str(100 + n) for n in range(12)))
    for number in (str(100 + n) for n in range(8)):
        book(api, large, number)

    statements: list[str] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: Any
    ) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        small_answer = answer(search(api, small, rooms_required=1))
        small_count = len(statements)
        statements.clear()
        large_answer = answer(search(api, large, rooms_required=4))
        large_count = len(statements)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert small_answer["available_rooms"] == 1
    assert large_answer["available_rooms"] == 4
    assert large_count == small_count, f"{large_count} vs {small_count} statements"
