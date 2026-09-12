"""Mixed room-type availability against real PostgreSQL.

Stage 4.5.26. "Two Doubles and one Suite" is one question, and the reason it needs no
allocation algorithm is structural: a room belongs to exactly one room type, so the types
partition the inventory and the demands are independent. The request is satisfiable exactly
when each named type can supply its own share, and every test below is about that claim or
about the ways it can be got wrong.

What only a real database can settle is the same list as the single-type stage -- the overlap
test against the exclusion constraint, which statuses hold inventory, and the isolation claims,
which are assertions about rows a query did NOT return.

**A mixed answer still reserves nothing.** Naming three rooms across two types holds none of
them; the booking transaction remains the only authority.
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

SUITE_EMAIL = "mixedrooms@example.test"

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


def add_type(api: TestClient, hotel: str, code: str, rooms: tuple[str, ...]) -> None:
    created = api.post(
        f"/api/v1/hotels/{hotel}/room-types",
        json={
            "code": code,
            "name": f"Type {code}",
            "max_occupancy": 4,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": "120.00",
            "currency": "EUR",
        },
    )
    assert created.status_code == 201, created.text
    for number in rooms:
        added = api.post(
            f"/api/v1/hotels/{hotel}/room-types/{code}/rooms", json={"room_number": number}
        )
        assert added.status_code == 201, added.text


def build_hotel(api: TestClient, slug: str, catalogue: dict[str, tuple[str, ...]]) -> str:
    """A hotel whose catalogue is ``{room type code: room numbers}``."""
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    for code, rooms in catalogue.items():
        add_type(api, hotel, code, rooms)
    return hotel


def standard_hotel(api: TestClient, slug: str) -> str:
    """Three Doubles and two Suites -- enough to be short of some requests and not others."""
    return build_hotel(api, slug, {"DLX": ("101", "102", "103"), "SUI": ("201", "202")})


def book(
    api: TestClient,
    hotel: str,
    code: str,
    room_number: str,
    *,
    check_in: dt.date = CHECK_IN,
    check_out: dt.date = CHECK_OUT,
    status: str = "confirmed",
) -> str:
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
    api: TestClient, hotel: str, rooms: list[str] | None = None, **extra: object
) -> Response:
    params: dict[str, object] = {"check_in": str(CHECK_IN), "check_out": str(CHECK_OUT)}
    if rooms is not None:
        params["rooms"] = rooms
    params.update(extra)
    return api.get(f"/api/v1/hotels/{hotel}/availability", params=params)


def answer(response: Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return dict(response.json())


def by_code(body: dict) -> dict[str, dict]:
    return {entry["code"]: entry for entry in body["room_types"]}


# ======================================================================================
# The mixed question
# ======================================================================================


def test_a_mixed_request_the_hotel_can_meet(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-ok")

    body = answer(search(api, hotel, ["DLX:2", "SUI:1"]))

    assert body["sufficient"] is True
    entries = by_code(body)
    assert entries["DLX"]["requested_count"] == 2
    assert entries["DLX"]["available_count"] == 3
    assert entries["SUI"]["requested_count"] == 1
    assert entries["SUI"]["available_count"] == 2


def test_a_mixed_request_short_on_one_type_fails_as_a_whole(api: TestClient) -> None:
    """The Doubles are enough and the Suites are not, so the request is not satisfiable.

    A per-type answer that said 'two of three parts are fine' would be true and useless: the
    party arrives together or not at all.
    """
    hotel = standard_hotel(api, "mx-short")

    body = answer(search(api, hotel, ["DLX:2", "SUI:3"]))

    assert body["sufficient"] is False
    entries = by_code(body)
    assert entries["DLX"]["available_count"] >= entries["DLX"]["requested_count"]
    assert entries["SUI"]["available_count"] == 2
    assert entries["SUI"]["requested_count"] == 3


def test_the_shortfall_is_reported_rather_than_hidden(api: TestClient) -> None:
    """The type that cannot answer is still listed, unlike the single-type search.

    There, a type that falls short is noise in a search; here the caller NAMED it, and dropping
    it would conceal which half of the request the hotel cannot meet.
    """
    hotel = standard_hotel(api, "mx-shortfall")

    body = answer(search(api, hotel, ["DLX:9", "SUI:9"]))

    assert body["sufficient"] is False
    assert sorted(by_code(body)) == ["DLX", "SUI"]
    assert by_code(body)["DLX"]["available_count"] == 3


def test_exactly_enough_of_each_is_enough(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-exact")

    body = answer(search(api, hotel, ["DLX:3", "SUI:2"]))

    assert body["sufficient"] is True
    assert body["available_rooms"] == 5


def test_one_room_too_many_of_either_type_fails(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-one-over")

    assert answer(search(api, hotel, ["DLX:4", "SUI:2"]))["sufficient"] is False
    assert answer(search(api, hotel, ["DLX:3", "SUI:3"]))["sufficient"] is False


def test_a_single_named_type_is_a_mixed_request_of_one(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-single")

    body = answer(search(api, hotel, ["SUI:2"]))

    assert body["sufficient"] is True
    assert sorted(by_code(body)) == ["SUI"]
    assert body["available_rooms"] == 2


def test_three_types_are_answered_together(api: TestClient) -> None:
    hotel = build_hotel(api, "mx-three", {"DLX": ("101",), "SUI": ("201", "202"), "FAM": ("301",)})

    enough = answer(search(api, hotel, ["DLX:1", "SUI:2", "FAM:1"]))
    one_short = answer(search(api, hotel, ["DLX:1", "SUI:2", "FAM:2"]))

    assert enough["sufficient"] is True
    assert enough["available_rooms"] == 4
    assert one_short["sufficient"] is False


# ======================================================================================
# Types do not substitute for one another
# ======================================================================================


def test_a_surplus_of_one_type_does_not_cover_a_shortfall_in_another(
    api: TestClient,
) -> None:
    """The stage's central semantic, stated as a counter-example.

    Five free rooms, three of them Doubles; a request for one Double and four Suites fails
    even though the hotel has five rooms free. A room of one type cannot be sold as another.
    """
    hotel = standard_hotel(api, "mx-no-substitute")

    body = answer(search(api, hotel, ["DLX:1", "SUI:4"]))

    assert body["sufficient"] is False
    assert body["available_rooms"] == 5


def test_booking_one_type_leaves_the_other_alone(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-independent")
    book(api, hotel, "SUI", "201")

    body = answer(search(api, hotel, ["DLX:3", "SUI:2"]))

    assert body["sufficient"] is False
    entries = by_code(body)
    assert entries["DLX"]["available_count"] == 3
    assert entries["SUI"]["available_count"] == 1
    assert answer(search(api, hotel, ["DLX:3", "SUI:1"]))["sufficient"] is True


def test_a_named_type_with_nothing_free_is_reported_as_zero(api: TestClient) -> None:
    """Zero is a fact about the type, not an absence from the answer."""
    hotel = standard_hotel(api, "mx-zero")
    for number in ("201", "202"):
        book(api, hotel, "SUI", number)

    body = answer(search(api, hotel, ["DLX:1", "SUI:1"]))

    assert body["sufficient"] is False
    assert by_code(body)["SUI"]["available_count"] == 0
    assert by_code(body)["SUI"]["rooms"] == []


def test_a_type_the_caller_did_not_name_is_not_reported(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-unnamed")

    body = answer(search(api, hotel, ["DLX:1"]))

    assert sorted(by_code(body)) == ["DLX"]
    assert body["available_rooms"] == 3


# ======================================================================================
# Status, boundaries and inventory rules are the existing ones
# ======================================================================================


@pytest.mark.parametrize("status", ["pending", "cancelled"])
def test_a_status_that_holds_no_inventory_leaves_a_mixed_answer_alone(
    api: TestClient, status: str
) -> None:
    hotel = standard_hotel(api, f"mx-free-{status}")
    if status == "pending":
        book(api, hotel, "SUI", "201", status="pending")
    else:
        booking = book(api, hotel, "SUI", "201")
        api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": status})

    body = answer(search(api, hotel, ["DLX:3", "SUI:2"]))

    assert body["sufficient"] is True


def test_a_confirmed_booking_reduces_the_named_type(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-holds")
    book(api, hotel, "DLX", "101")

    assert answer(search(api, hotel, ["DLX:3"]))["sufficient"] is False
    assert answer(search(api, hotel, ["DLX:2"]))["sufficient"] is True


def test_a_stay_ending_when_ours_begins_does_not_collide(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-touch")
    book(api, hotel, "DLX", "101", check_in=CHECK_IN - dt.timedelta(days=2), check_out=CHECK_IN)

    assert answer(search(api, hotel, ["DLX:3", "SUI:2"]))["sufficient"] is True


def test_a_room_out_of_service_reduces_its_own_type_only(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-outofservice")
    moved = api.patch(
        f"/api/v1/hotels/{hotel}/room-types/DLX/rooms/101", json={"status": "maintenance"}
    )
    assert moved.status_code == 200, moved.text

    body = answer(search(api, hotel, ["DLX:3", "SUI:2"]))

    assert body["sufficient"] is False
    assert by_code(body)["DLX"]["available_count"] == 2
    assert by_code(body)["SUI"]["available_count"] == 2


# ======================================================================================
# Tenant isolation
# ======================================================================================


def test_another_hotels_rooms_do_not_fill_a_mixed_request(api: TestClient) -> None:
    """Both properties use the same codes and the same room numbers."""
    first = build_hotel(api, "mx-tenant-a", {"DLX": ("101",), "SUI": ("201",)})
    build_hotel(api, "mx-tenant-b", {"DLX": ("101", "102", "103"), "SUI": ("201", "202")})

    body = answer(search(api, first, ["DLX:2", "SUI:1"]))

    assert body["sufficient"] is False
    assert by_code(body)["DLX"]["available_count"] == 1


def test_another_hotels_bookings_do_not_reduce_a_mixed_answer(api: TestClient) -> None:
    first = standard_hotel(api, "mx-cross-a")
    second = standard_hotel(api, "mx-cross-b")
    book(api, second, "DLX", "101")
    book(api, second, "SUI", "201")

    assert answer(search(api, first, ["DLX:3", "SUI:2"]))["sufficient"] is True


def test_a_room_type_of_another_hotel_is_not_found(api: TestClient) -> None:
    """The code is resolved through the hotel's own catalogue, so another property's code is
    simply not a code here -- the hotel's 404, not an empty line in the answer."""
    first = build_hotel(api, "mx-code-a", {"DLX": ("101",)})
    build_hotel(api, "mx-code-b", {"PENTHOUSE": ("901",)})

    response = search(api, first, ["PENTHOUSE:1"])

    assert response.status_code == 404, response.text
    assert set(response.json()) == {"error"}


def test_a_non_member_cannot_ask_a_mixed_question(api: TestClient, engine: Engine) -> None:
    hotel = standard_hotel(api, "mx-stranger")
    stranger = authenticated_client(engine, email="mx-stranger@example.test")

    refused = stranger.get(
        f"/api/v1/hotels/{hotel}/availability",
        params={
            "check_in": str(CHECK_IN),
            "check_out": str(CHECK_OUT),
            "rooms": ["DLX:1"],
        },
    )

    assert refused.status_code == 404


# ======================================================================================
# Validation
# ======================================================================================


@pytest.mark.parametrize(
    "rooms",
    [
        ["DLX"],
        ["DLX:"],
        [":2"],
        ["DLX:0"],
        ["DLX:-1"],
        ["DLX:101"],
        ["DLX:two"],
        ["DLX:2", "DLX:1"],
        ["DLX:2:3"],
        [""],
    ],
    ids=[
        "no-separator",
        "no-count",
        "no-code",
        "zero",
        "negative",
        "over-the-bound",
        "not-a-number",
        "duplicate-type",
        "two-separators",
        "empty",
    ],
)
def test_a_malformed_mixed_request_is_refused(api: TestClient, rooms: list[str]) -> None:
    hotel = standard_hotel(api, "mx-malformed")

    response = search(api, hotel, rooms)

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}


def test_too_many_types_are_refused(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-too-many")

    response = search(api, hotel, [f"T{index}:1" for index in range(21)])

    assert response.status_code == 422, response.text


def test_the_code_is_matched_case_insensitively(api: TestClient) -> None:
    """Room type codes are upper case everywhere else in this API, and a lower-case one is a
    spelling rather than a different type."""
    hotel = standard_hotel(api, "mx-case")

    body = answer(search(api, hotel, ["dlx:2"]))

    assert body["sufficient"] is True
    assert sorted(by_code(body)) == ["DLX"]


@pytest.mark.parametrize(
    "extra", [{"room_type_code": "DLX"}, {"rooms_required": 2}], ids=["code", "count"]
)
def test_the_two_forms_cannot_be_combined(api: TestClient, extra: dict) -> None:
    """Refused rather than resolved by precedence: honouring one of two contradictory
    instructions answers a question the caller did not ask."""
    hotel = standard_hotel(api, "mx-combined")

    response = search(api, hotel, ["DLX:2"], **extra)

    assert response.status_code == 422, response.text


def test_a_refusal_names_no_internals(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-leak")

    rendered = str(search(api, hotel, ["DLX:0"]).json())

    for forbidden in ["SELECT", "room_type_id", "hotel_id", "sqlalchemy", "psycopg", "Room."]:
        assert forbidden not in rendered, f"the refusal leaked {forbidden!r}"


def test_a_rejected_entry_is_not_echoed(api: TestClient) -> None:
    """The shared handler drops rejected input everywhere else, and this is input."""
    hotel = standard_hotel(api, "mx-echo")

    rendered = str(search(api, hotel, ["SEKRIT-VALUE-1234:x"]).json())

    assert "SEKRIT-VALUE-1234" not in rendered


# ======================================================================================
# The single-type form is untouched
# ======================================================================================


def test_omitting_the_mixed_parameter_answers_exactly_as_before(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-untouched")

    body = answer(search(api, hotel))

    assert body["rooms_required"] == 1
    assert body["sufficient"] is True
    assert body["available_rooms"] == 5
    assert all(entry["requested_count"] is None for entry in body["room_types"])


def test_the_single_type_form_still_filters(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-still-filters")

    body = answer(search(api, hotel, rooms_required=3))

    assert sorted(by_code(body)) == ["DLX"], "SUI has two rooms and cannot supply three"


# ======================================================================================
# A mixed answer reserves nothing
# ======================================================================================


def test_a_mixed_search_reserves_nothing(api: TestClient, session: Session) -> None:
    hotel = standard_hotel(api, "mx-advisory")
    session.rollback()
    before = session.execute(sa.text("SELECT count(*) FROM booking_rooms")).scalar()

    first = answer(search(api, hotel, ["DLX:2", "SUI:1"]))
    second = answer(search(api, hotel, ["DLX:2", "SUI:1"]))

    assert first["sufficient"] is second["sufficient"] is True
    session.rollback()
    assert session.execute(sa.text("SELECT count(*) FROM booking_rooms")).scalar() == before


def test_a_mixed_answer_can_be_overtaken(api: TestClient) -> None:
    hotel = standard_hotel(api, "mx-overtaken")
    assert answer(search(api, hotel, ["DLX:3", "SUI:2"]))["sufficient"] is True

    book(api, hotel, "SUI", "201")

    assert answer(search(api, hotel, ["DLX:3", "SUI:2"]))["sufficient"] is False


# ======================================================================================
# Performance
# ======================================================================================


def test_a_mixed_request_costs_one_lookup_per_named_type_and_one_search(
    api: TestClient, engine: Engine
) -> None:
    """Bounded by the REQUEST, not by the inventory.

    Two hotels, one with four rooms and one with sixteen plus eight bookings, both asked the
    same two-type question: the same number of statements. What may grow is the number of
    types the caller names, and that is bounded at twenty.
    """
    small = build_hotel(api, "mx-perf-small", {"DLX": ("101", "102"), "SUI": ("201", "202")})
    large = build_hotel(
        api,
        "mx-perf-large",
        {
            "DLX": tuple(str(100 + n) for n in range(8)),
            "SUI": tuple(str(200 + n) for n in range(8)),
        },
    )
    for number in (str(100 + n) for n in range(4)):
        book(api, large, "DLX", number)
    for number in (str(200 + n) for n in range(4)):
        book(api, large, "SUI", number)

    statements: list[str] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: Any
    ) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        answer(search(api, small, ["DLX:1", "SUI:1"]))
        small_count = len(statements)
        statements.clear()
        answer(search(api, large, ["DLX:1", "SUI:1"]))
        large_count = len(statements)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert large_count == small_count, f"{large_count} vs {small_count} statements"
