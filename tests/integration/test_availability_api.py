"""Availability search against real PostgreSQL.

Stage 4.5.10. The search's overlap predicate has to mean exactly what
``excl_booking_rooms_room_no_overlap`` means, and only PostgreSQL can settle that: the
constraint is a GiST exclusion over ``daterange(check_in_date, check_out_date, '[)')``, and a
test on any other engine would be checking Python's opinion of an interval rather than the
database's.

The boundary cases below are the ones where a wrong interval definition hides. A stay abutting
another -- ``[Sep 14, Sep 18)`` against ``[Sep 10, Sep 14)`` -- must be available, because the
14th is a departure day and no night is sold on it. Get that wrong in either direction and the
property either loses a sale every day of the year or sells the same room twice.

**A result is not a reservation**, and one test says so explicitly rather than leaving it to
the docstring: the search is a read, and the exclusion constraint remains the authority.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models.enums import INVENTORY_HOLDING_STATUSES, BookingStatus
from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    requires_postgres,
)

SUITE_EMAIL = "availability@example.test"
OTHER_EMAIL = "availability-other@example.test"

pytestmark = requires_postgres


def sep(day: int) -> dt.date:
    """September 2027, so no test collides with another suite's dates."""
    return dt.date(2027, 9, day)


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


def availability_url(hotel: str) -> str:
    return f"/api/v1/hotels/{hotel}/availability"


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def add_room_type(api: TestClient, hotel: str, code: str, *, max_occupancy: int = 4) -> None:
    api.post(
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


def build_hotel(
    api: TestClient,
    slug: str = "avail-hotel",
    rooms: tuple[str, ...] = ("101", "102", "103"),
    code: str = "DLX",
) -> str:
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    add_room_type(api, hotel, code)
    for number in rooms:
        api.post(f"/api/v1/hotels/{hotel}/room-types/{code}/rooms", json={"room_number": number})
    return hotel


def book(
    api: TestClient,
    hotel: str,
    room_number: str,
    check_in: dt.date,
    check_out: dt.date,
    status: str = "confirmed",
) -> str:
    """Allocate one room for a stay, through the real booking endpoint."""
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
    api: TestClient, hotel: str, check_in: dt.date, check_out: dt.date, **extra: object
) -> Response:
    params: dict[str, object] = {"check_in": str(check_in), "check_out": str(check_out)}
    params.update(extra)
    return api.get(availability_url(hotel), params=params)


def free_rooms(api: TestClient, hotel: str, check_in: dt.date, check_out: dt.date) -> list[str]:
    response = search(api, hotel, check_in, check_out)
    assert response.status_code == 200, response.text
    return sorted(
        room["room_number"]
        for room_type in response.json()["room_types"]
        for room in room_type["rooms"]
    )


@pytest.fixture
def hotel(api: TestClient) -> str:
    return build_hotel(api)


# ======================================================================================
# Interval semantics -- the cases a wrong definition hides in
# ======================================================================================


def test_all_rooms_are_free_when_nothing_is_booked(api: TestClient, hotel: str) -> None:
    assert free_rooms(api, hotel, sep(10), sep(14)) == ["101", "102", "103"]


def test_the_exact_same_stay_is_blocked(api: TestClient, hotel: str) -> None:
    book(api, hotel, "101", sep(10), sep(14))

    assert free_rooms(api, hotel, sep(10), sep(14)) == ["102", "103"]


def test_a_stay_beginning_on_the_departure_day_is_free(api: TestClient, hotel: str) -> None:
    """The half-open boundary. No night is sold on the 14th, so the 14th is bookable.

    Getting this wrong loses a sale on every changeover day the property ever has.
    """
    book(api, hotel, "101", sep(10), sep(14))

    assert free_rooms(api, hotel, sep(14), sep(18)) == ["101", "102", "103"]


def test_a_stay_ending_on_the_arrival_day_is_free(api: TestClient, hotel: str) -> None:
    """The other side of the same boundary."""
    book(api, hotel, "101", sep(10), sep(14))

    assert free_rooms(api, hotel, sep(6), sep(10)) == ["101", "102", "103"]


def test_a_partial_overlap_is_blocked(api: TestClient, hotel: str) -> None:
    book(api, hotel, "101", sep(10), sep(14))

    assert free_rooms(api, hotel, sep(12), sep(16)) == ["102", "103"]


def test_a_search_contained_inside_a_booking_is_blocked(api: TestClient, hotel: str) -> None:
    book(api, hotel, "101", sep(10), sep(20))

    assert free_rooms(api, hotel, sep(12), sep(14)) == ["102", "103"]


def test_a_booking_contained_inside_the_search_is_blocked(api: TestClient, hotel: str) -> None:
    book(api, hotel, "101", sep(12), sep(14))

    assert free_rooms(api, hotel, sep(10), sep(20)) == ["102", "103"]


def test_a_single_night_stay_works(api: TestClient, hotel: str) -> None:
    book(api, hotel, "101", sep(10), sep(11))

    assert free_rooms(api, hotel, sep(10), sep(11)) == ["102", "103"]
    assert free_rooms(api, hotel, sep(11), sep(12)) == ["101", "102", "103"]


# ======================================================================================
# Which statuses hold inventory
# ======================================================================================


@pytest.mark.parametrize("status", [BookingStatus.CONFIRMED.value, BookingStatus.CHECKED_IN.value])
def test_inventory_holding_statuses_block_the_room(
    api: TestClient, hotel: str, status: str
) -> None:
    """Created directly in the status, since creation accepts any initial one."""
    book(api, hotel, "101", sep(10), sep(14), status=status)

    assert "101" not in free_rooms(api, hotel, sep(10), sep(14))


@pytest.mark.parametrize(
    "status",
    [
        BookingStatus.PENDING.value,
        BookingStatus.CANCELLED.value,
        BookingStatus.CHECKED_OUT.value,
        BookingStatus.NO_SHOW.value,
    ],
)
def test_non_holding_statuses_leave_the_room_free(api: TestClient, hotel: str, status: str) -> None:
    """Pending holds nothing, so an abandoned checkout never blocks a room."""
    book(api, hotel, "101", sep(10), sep(14), status=status)

    assert "101" in free_rooms(api, hotel, sep(10), sep(14))


def test_the_search_uses_the_shared_inventory_constant() -> None:
    """Not a second list. The same tuple the exclusion constraint's WHERE is built from."""
    assert INVENTORY_HOLDING_STATUSES == ("confirmed", "checked_in")


# ======================================================================================
# The state machine drives availability
# ======================================================================================


def test_pending_to_confirmed_takes_the_room_out_of_inventory(api: TestClient, hotel: str) -> None:
    booking = book(api, hotel, "101", sep(10), sep(14), status="pending")
    assert "101" in free_rooms(api, hotel, sep(10), sep(14))

    api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "confirmed"})

    assert "101" not in free_rooms(api, hotel, sep(10), sep(14))


def test_confirmed_to_checked_in_keeps_it_out(api: TestClient, hotel: str) -> None:
    booking = book(api, hotel, "101", sep(10), sep(14))

    api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_in"})

    assert "101" not in free_rooms(api, hotel, sep(10), sep(14))


def test_checked_in_to_checked_out_returns_it(api: TestClient, hotel: str) -> None:
    booking = book(api, hotel, "101", sep(10), sep(14))
    api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_in"})

    api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_out"})

    assert "101" in free_rooms(api, hotel, sep(10), sep(14))


def test_confirmed_to_cancelled_returns_it(api: TestClient, hotel: str) -> None:
    booking = book(api, hotel, "101", sep(10), sep(14))

    api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "cancelled"})

    assert "101" in free_rooms(api, hotel, sep(10), sep(14))


def test_confirmed_to_no_show_returns_it(api: TestClient, hotel: str) -> None:
    booking = book(api, hotel, "101", sep(10), sep(14))

    api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "no_show"})

    assert "101" in free_rooms(api, hotel, sep(10), sep(14))


# ======================================================================================
# Room-type inventory
# ======================================================================================


def test_the_count_is_derived_from_actual_rooms_as_they_are_taken(
    api: TestClient, hotel: str
) -> None:
    """Three rooms, occupied one at a time. Counted from allocations, not from a stored
    total minus a booking count."""
    counts = []
    for number in ("101", "102", "103"):
        body = search(api, hotel, sep(10), sep(14)).json()
        counts.append(body["room_types"][0]["available_count"] if body["room_types"] else 0)
        book(api, hotel, number, sep(10), sep(14))

    final = search(api, hotel, sep(10), sep(14)).json()

    assert counts == [3, 2, 1]
    assert final["room_types"] == []
    assert final["available_rooms"] == 0


def test_zero_availability_is_a_success_not_an_error(api: TestClient, hotel: str) -> None:
    for number in ("101", "102", "103"):
        book(api, hotel, number, sep(10), sep(14))

    response = search(api, hotel, sep(10), sep(14))

    assert response.status_code == 200
    assert response.json()["room_types"] == []
    assert response.json()["available_rooms"] == 0


def test_multiple_room_types_are_reported_separately(api: TestClient) -> None:
    hotel = build_hotel(api, "avail-multi", rooms=("101",), code="DLX")
    add_room_type(api, hotel, "STD")
    api.post(f"/api/v1/hotels/{hotel}/room-types/STD/rooms", json={"room_number": "201"})
    book(api, hotel, "101", sep(10), sep(14))

    body = search(api, hotel, sep(10), sep(14)).json()

    assert [t["code"] for t in body["room_types"]] == ["STD"]
    assert body["available_rooms"] == 1


def test_the_count_matches_the_rooms_listed(api: TestClient, hotel: str) -> None:
    body = search(api, hotel, sep(10), sep(14)).json()

    for room_type in body["room_types"]:
        assert room_type["available_count"] == len(room_type["rooms"])
    assert body["available_rooms"] == sum(t["available_count"] for t in body["room_types"])


# ======================================================================================
# Room eligibility
# ======================================================================================


def test_an_inactive_room_is_never_offered(api: TestClient, hotel: str) -> None:
    api.patch(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms/101", json={"is_active": False})

    assert "101" not in free_rooms(api, hotel, sep(10), sep(14))


@pytest.mark.parametrize("status", ["maintenance", "out_of_order"])
def test_an_out_of_service_room_is_never_offered(api: TestClient, hotel: str, status: str) -> None:
    """Conservative: a room that cannot be sold must not be offered."""
    api.patch(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms/101", json={"status": status})

    assert "101" not in free_rooms(api, hotel, sep(10), sep(14))


@pytest.mark.parametrize("status", ["cleaning", "occupied"])
def test_a_transient_housekeeping_state_does_not_block_a_future_stay(
    api: TestClient, hotel: str, status: str
) -> None:
    """`cleaning` and `occupied` are true of today and say nothing about next month, and
    `occupied` is in any case implied by the allocation the search already checks."""
    api.patch(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms/101", json={"status": status})

    assert "101" in free_rooms(api, hotel, sep(10), sep(14))


# ======================================================================================
# Filters
# ======================================================================================


def test_the_room_type_filter_narrows_the_result(api: TestClient) -> None:
    hotel = build_hotel(api, "avail-filter", rooms=("101",), code="DLX")
    add_room_type(api, hotel, "STD")
    api.post(f"/api/v1/hotels/{hotel}/room-types/STD/rooms", json={"room_number": "201"})

    body = search(api, hotel, sep(10), sep(14), room_type_code="STD").json()

    assert [t["code"] for t in body["room_types"]] == ["STD"]


def test_an_unknown_room_type_is_not_found(api: TestClient, hotel: str) -> None:
    response = search(api, hotel, sep(10), sep(14), room_type_code="NOPE")

    assert response.status_code == 404


def test_the_guest_filter_uses_max_occupancy(api: TestClient) -> None:
    hotel = build_hotel(api, "avail-guests", rooms=("101",), code="SML")
    api.post("/api/v1/hotels", json=hotel_payload("ignored"))
    add_room_type(api, hotel, "BIG", max_occupancy=6)
    api.post(f"/api/v1/hotels/{hotel}/room-types/BIG/rooms", json={"room_number": "301"})

    # SML was created with max_occupancy 4 by build_hotel.
    body = search(api, hotel, sep(10), sep(14), guests=5).json()

    assert [t["code"] for t in body["room_types"]] == ["BIG"]


def test_guests_within_capacity_returns_everything(api: TestClient, hotel: str) -> None:
    body = search(api, hotel, sep(10), sep(14), guests=2).json()

    assert body["available_rooms"] == 3


# ======================================================================================
# Input validation
# ======================================================================================


def test_a_zero_night_stay_is_refused(api: TestClient, hotel: str) -> None:
    """An empty daterange overlaps nothing, so every room would look free for a stay nobody
    can book. Refusing is the only honest answer."""
    response = search(api, hotel, sep(10), sep(10))

    assert response.status_code == 422
    assert "later than check_in" in response.json()["error"]["message"]


def test_a_reversed_stay_is_refused_not_swapped(api: TestClient, hotel: str) -> None:
    response = search(api, hotel, sep(14), sep(10))

    assert response.status_code == 422


def test_an_absurdly_long_stay_is_refused(api: TestClient, hotel: str) -> None:
    response = search(api, hotel, sep(10), dt.date(2030, 9, 10))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"check_out": "2027-09-14"},
        {"check_in": "2027-09-10"},
        {"check_in": "not-a-date", "check_out": "2027-09-14"},
        {"check_in": "2027-09-10", "check_out": "2027-13-40"},
    ],
)
def test_malformed_or_missing_dates_are_rejected(
    api: TestClient, hotel: str, params: dict[str, str]
) -> None:
    response = api.get(availability_url(hotel), params=params)

    assert response.status_code == 422


def test_guests_must_be_positive(api: TestClient, hotel: str) -> None:
    assert search(api, hotel, sep(10), sep(14), guests=0).status_code == 422


# ======================================================================================
# Tenancy and authorization
# ======================================================================================


def test_another_hotels_rooms_are_never_returned(api: TestClient) -> None:
    first = build_hotel(api, "avail-a", rooms=("101",))
    build_hotel(api, "avail-b", rooms=("999",))

    assert free_rooms(api, first, sep(10), sep(14)) == ["101"]


def test_another_hotels_booking_does_not_block_this_hotels_room(api: TestClient) -> None:
    """Same room NUMBER at two properties. Tenancy is asserted on the room, not the number."""
    first = build_hotel(api, "avail-c", rooms=("101",))
    second = build_hotel(api, "avail-d", rooms=("101",))
    book(api, second, "101", sep(10), sep(14))

    assert free_rooms(api, first, sep(10), sep(14)) == ["101"]


def test_a_room_type_code_from_another_hotel_does_not_leak(api: TestClient) -> None:
    """Identical to an invented code: the wall must not become an existence oracle."""
    first = build_hotel(api, "avail-e", rooms=("101",), code="DLX")
    build_hotel(api, "avail-f", rooms=("201",), code="PENT")

    foreign = search(api, first, sep(10), sep(14), room_type_code="PENT")
    invented = search(api, first, sep(10), sep(14), room_type_code="ZZZZ")

    assert foreign.status_code == 404
    assert foreign.json() == invented.json()


def test_an_unknown_hotel_is_not_found(api: TestClient) -> None:
    response = search(api, str(uuid.uuid4()), sep(10), sep(14))

    assert response.status_code == 404


def test_a_non_member_cannot_search(api: TestClient, engine: Engine, hotel: str) -> None:
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    response = stranger.get(
        availability_url(hotel), params={"check_in": "2027-09-10", "check_out": "2027-09-14"}
    )

    assert response.status_code == 404
    assert "101" not in response.text


def test_an_unauthenticated_caller_cannot_search(api: TestClient, hotel: str) -> None:
    anonymous = TestClient(api.app)

    response = anonymous.get(
        availability_url(hotel), params={"check_in": "2027-09-10", "check_out": "2027-09-14"}
    )

    assert response.status_code == 401


def test_a_viewer_may_search(api: TestClient, engine: Engine, hotel: str) -> None:
    """A read, so it carries the same rule as listing rooms: membership, no extra role."""
    viewer = authenticated_client(engine, email=OTHER_EMAIL)
    grant_membership(engine, OTHER_EMAIL, hotel, "viewer")

    response = viewer.get(
        availability_url(hotel), params={"check_in": "2027-09-10", "check_out": "2027-09-14"}
    )

    assert response.status_code == 200
    assert response.json()["available_rooms"] == 3


# ======================================================================================
# Search and booking agree; the constraint stays the authority
# ======================================================================================


def test_search_then_book_then_search(api: TestClient, hotel: str) -> None:
    """The consistency loop: what the search offered is exactly what could be sold."""
    assert "101" in free_rooms(api, hotel, sep(10), sep(14))

    book(api, hotel, "101", sep(10), sep(14))

    assert "101" not in free_rooms(api, hotel, sep(10), sep(14))


def test_a_result_is_not_a_reservation(api: TestClient, hotel: str) -> None:
    """Two callers both see room 101 free. One books it; the other is refused by the
    database, not by the search. The search holds nothing and promises nothing."""
    assert "101" in free_rooms(api, hotel, sep(10), sep(14))
    assert "101" in free_rooms(api, hotel, sep(10), sep(14))

    book(api, hotel, "101", sep(10), sep(14))

    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Second", "last_name": "Caller"}
    ).json()["public_id"]
    second = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(sep(10)),
            "check_out_date": str(sep(14)),
            "status": "confirmed",
            "total_amount": "100.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [{"stay_date": str(sep(10 + n))} for n in range(4)],
                }
            ],
        },
    )

    assert second.status_code == 409
    assert "already booked" in second.json()["error"]["message"]


def test_the_search_never_offers_what_the_constraint_would_refuse(
    api: TestClient, hotel: str
) -> None:
    """Booking every room the search offered must succeed for all of them.

    If the predicate were looser than the constraint, one of these would hit 409.
    """
    book(api, hotel, "102", sep(10), sep(14))
    offered = free_rooms(api, hotel, sep(11), sep(13))

    for number in offered:
        booked = book(api, hotel, number, sep(11), sep(13))
        assert booked


# ======================================================================================
# Response shape, leakage, and cost
# ======================================================================================


def test_the_response_exposes_no_internal_identifier(api: TestClient, hotel: str) -> None:
    body = search(api, hotel, sep(10), sep(14)).json()
    keys = set(body) | {k for t in body["room_types"] for k in t}
    keys |= {k for t in body["room_types"] for r in t["rooms"] for k in r}

    assert "id" not in keys
    assert not {k for k in keys if k.endswith("_id") and k != "hotel_public_id"}


def test_the_response_shape_is_exactly_as_documented(api: TestClient, hotel: str) -> None:
    body = search(api, hotel, sep(10), sep(14)).json()

    assert set(body) == {
        "hotel_public_id",
        "check_in",
        "check_out",
        "nights",
        # Stage 4.5.25: how many rooms of one type were asked for, and whether any type
        # can supply them. Both default to the single-room question this always answered.
        "rooms_required",
        "sufficient",
        "available_rooms",
        "room_types",
    }
    assert body["nights"] == 4
    assert set(body["room_types"][0]) == {
        "code",
        "name",
        "max_occupancy",
        "standard_occupancy",
        "base_price",
        "currency",
        "available_count",
        # Stage 4.5.26: how many of this type a mixed request asked for. Null here,
        # because this search named no types and so made no per-type request.
        "requested_count",
        "rooms",
    }
    assert set(body["room_types"][0]["rooms"][0]) == {"room_number", "floor"}


def test_a_refusal_leaks_no_internals(api: TestClient, hotel: str) -> None:
    response = search(api, hotel, sep(14), sep(10))

    for leak in [
        "SELECT",
        "daterange",
        "booking_rooms",
        "excl_booking_rooms",
        "room_id",
        "hotel_id",
        "sqlalchemy",
        "psycopg",
        "Traceback",
        "23P01",
    ]:
        assert leak not in response.text, f"leaked {leak!r}"


def test_the_search_costs_a_bounded_number_of_queries(
    api: TestClient, engine: Engine, hotel: str
) -> None:
    """No query per room and none per room type.

    Twelve rooms across two types with bookings against several of them; the statement count
    must not track any of those numbers.
    """
    add_room_type(api, hotel, "STD")
    for number in range(200, 210):
        api.post(f"/api/v1/hotels/{hotel}/room-types/STD/rooms", json={"room_number": str(number)})
    for room_number in ("201", "202", "203"):
        book(api, hotel, room_number, sep(10), sep(14))

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        response = search(api, hotel, sep(10), sep(14))
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # user, hotel, membership, availability. Bounded, and independent of 13 rooms and 3
    # bookings -- a per-room or per-type query would put this in the dozens.
    assert len(selects) <= 6, f"{len(selects)} SELECTs: {selects}"
