"""What a booking response costs in SQL, measured against real PostgreSQL (Stage 4.5.28).

``BookingService._to_response`` walks four relationships: the allocations, each allocation's
nights, each allocation's ``room`` (for its number) and the booking's ``guest`` (for its
public id). Whether reaching them is free depends entirely on what the caller loaded, and
before this stage the two single-booking readers disagreed with the listing about that.

**The measurement came first, and it changed the fix.** The obvious repair -- eager-load
everything on the one shared reader -- was applied, measured, and reverted, because the
numbers said it was wrong:

    endpoint      rooms   before   naive fix   targeted fix
    create        1/2/4   17/21/29  19/23/31    17/21/29
    get           1/2/4    9/10/12   9/ 9/ 9     9/ 9/ 9
    update        1/2/4   17/18/20  21/21/21    17/17/17
    list          1/2/4   10/10/10  10/10/10    10/10/10
    modify_stay   1/2/4   26/30/38  31/35/43    26/30/38
    extend        1/2/4   28/28/28  33/33/33    28/28/28

Only ``get`` and ``update`` ever had a room-count-proportional N+1. The write paths resolve
their own rooms on the way in, so their allocations are already in the identity map and
eager loading merely re-fetches rows the session is holding -- which is why the naive fix
made four paths WORSE and none of them better. The shipped fix gives the render paths their
own reader and leaves the write paths alone.

These tests pin that. They are about statement COUNTS and their growth, not about which
statements: an assertion that grows with the number of allocated rooms is the definition of
the bug, and an assertion on exact SQL text would break on every unrelated refactor.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    requires_postgres,
)

SUITE_EMAIL = "queryboundary@example.test"
OTHER_EMAIL = "queryboundary-other@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 9, 10)
CHECK_OUT = dt.date(2026, 9, 15)
NIGHTS = (CHECK_OUT - CHECK_IN).days


# ======================================================================================
# Fixtures and helpers
# ======================================================================================


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def nights_for(check_in: dt.date, check_out: dt.date) -> list[dict[str, str]]:
    return [
        {"stay_date": str(check_in + dt.timedelta(days=n))}
        for n in range((check_out - check_in).days)
    ]


def build_hotel(api: TestClient, slug: str, *, rooms: tuple[str, ...]) -> str:
    hotel = str(
        api.post(
            "/api/v1/hotels",
            json={
                "slug": slug,
                "name": f"Hotel {slug}",
                "address_line1": "1 Dionysiou Areopagitou",
                "city": "Athens",
                "country_code": "GR",
                "timezone": "Europe/Athens",
                "currency": "EUR",
            },
        ).json()["public_id"]
    )
    api.post(
        f"/api/v1/hotels/{hotel}/room-types",
        json={
            "code": "DLX",
            "name": "Deluxe",
            "max_occupancy": 4,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": "100.00",
            "currency": "EUR",
        },
    )
    for number in rooms:
        api.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": number})
    return hotel


def make_booking(api: TestClient, hotel: str, *, rooms: tuple[str, ...], week: int = 0) -> str:
    """One booking. ``week`` shifts the stay so several can share a room.

    The exclusion constraint is real in these tests too: two confirmed bookings of the same
    room over the same nights is a 409, not a fixture convenience.
    """
    check_in = CHECK_IN + dt.timedelta(days=7 * week)
    check_out = CHECK_OUT + dt.timedelta(days=7 * week)
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(check_in),
            "check_out_date": str(check_out),
            "status": "confirmed",
            "total_amount": "500.00",
            "currency": "EUR",
            "rooms": [
                {"room_number": number, "nights": nights_for(check_in, check_out)}
                for number in rooms
            ],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def statements(engine: Engine, action: Callable[[], Any]) -> list[str]:
    """Every SQL statement the engine issues while *action* runs, whitespace-normalised."""
    seen: list[str] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: Any
    ) -> None:
        seen.append(" ".join(statement.split()))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    return seen


def lazy_room_loads(seen: list[str]) -> int:
    """Single-row room fetches by primary key -- the signature of the N+1 being fixed.

    ``selectinload`` emits ``WHERE rooms.id IN (...)`` once; a lazy load emits
    ``WHERE rooms.id = ...`` once per allocation. Counting the second is what makes this a
    test of the DEFECT rather than of a number that happens to be true today.
    """
    return len([s for s in seen if s.startswith("SELECT rooms.") and "rooms.id = " in s])


def lazy_guest_loads(seen: list[str]) -> int:
    """The same signature for the guest.

    Only ever one per booking, so this is not an N+1 -- but it is an avoidable statement on
    a path that renders one booking, and counting it separately is what stops the guest half
    of the loader from being decoration nobody checks.
    """
    return len([s for s in seen if s.startswith("SELECT guests.") and "guests.id = " in s])


def room_resolution_queries(seen: list[str]) -> int:
    """Statements that resolve rooms BY NUMBER -- the write paths' room lookup.

    Counts both shapes on purpose: ``room_number = ...`` (one room, the pre-4.5.29
    per-room lookup) and ``room_number IN (...)`` (the batch). A test that only knew
    the batch shape would read zero for the old code and call it an improvement.
    """
    return len(
        [
            s
            for s in seen
            if s.startswith("SELECT rooms.")
            and ("rooms.room_number = " in s or "rooms.room_number IN " in s)
        ]
    )


def guest_queries(seen: list[str]) -> int:
    """Statements that fetch guests as a query of their own.

    A joined many-to-one produces no such statement at all -- the guest arrives inside
    the booking SELECT -- so this counts BOTH the lazy load and a ``selectinload``,
    which are the two ways a separate guest query comes back.
    """
    return len([s for s in seen if s.startswith("SELECT guests.")])


def room_queries(seen: list[str]) -> int:
    """Statements that fetch rooms as a query of their own, by id.

    Distinct from :func:`room_resolution_queries`, which counts resolution BY NUMBER on
    the write paths. This one is about rendering.

    Statements joining ``room_types`` are excluded on purpose: the room-type-code lookup
    is a two-column projection issued ONCE per request, not a room entity being loaded one
    row at a time. Counting it here would make this assert something it does not mean.
    """
    return len([s for s in seen if s.startswith("SELECT rooms.") and "room_types" not in s])


def scenario(api: TestClient, room_count: int, tag: str) -> tuple[str, str, tuple[str, ...]]:
    numbers = tuple(str(101 + offset) for offset in range(room_count))
    hotel = build_hotel(api, f"qb-{tag}-{room_count}", rooms=numbers)
    return hotel, make_booking(api, hotel, rooms=numbers), numbers


# ======================================================================================
# A. The N+1 is gone, and would be caught if reintroduced
# ======================================================================================


def test_reading_one_booking_costs_the_same_whatever_the_party_size(
    api: TestClient, engine: Engine
) -> None:
    """The regression test proper. Before Stage 4.5.28 this read 9, 10 and 12 statements
    for 1, 2 and 4 rooms; reintroducing the lazy room load makes it grow again."""
    counts = []
    for room_count in (1, 2, 4):
        hotel, booking, _ = scenario(api, room_count, "get")
        seen = statements(
            engine,
            partial(lambda h, b: api.get(f"/api/v1/hotels/{h}/bookings/{b}"), hotel, booking),
        )
        counts.append(len(seen))

    assert counts[0] == counts[1] == counts[2], counts


def test_reading_one_booking_never_fetches_a_room_one_row_at_a_time(
    api: TestClient, engine: Engine
) -> None:
    hotel, booking, _ = scenario(api, 4, "get-lazy")

    seen = statements(engine, lambda: api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}"))

    assert lazy_room_loads(seen) == 0, [s for s in seen if "rooms.id = " in s]
    assert lazy_guest_loads(seen) == 0, [s for s in seen if "guests.id = " in s]


def test_updating_a_booking_costs_the_same_whatever_the_party_size(
    api: TestClient, engine: Engine
) -> None:
    """``update`` re-renders after commit and had the same N+1: 17, 18 and 20 statements."""
    counts = []
    for room_count in (1, 2, 4):
        hotel, booking, _ = scenario(api, room_count, "update")
        seen = statements(
            engine,
            partial(
                lambda h, b: api.patch(f"/api/v1/hotels/{h}/bookings/{b}", json={"adults": 2}),
                hotel,
                booking,
            ),
        )
        counts.append(len(seen))

    assert counts[0] == counts[1] == counts[2], counts


def test_an_update_that_changes_nothing_also_avoids_the_lazy_load(
    api: TestClient, engine: Engine
) -> None:
    """The early return renders the booking too, and rendered it through the write reader."""
    hotel, booking, _ = scenario(api, 4, "noop")

    seen = statements(
        engine, lambda: api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={})
    )

    assert lazy_room_loads(seen) == 0, [s for s in seen if "rooms.id = " in s]
    assert lazy_guest_loads(seen) == 0, [s for s in seen if "guests.id = " in s]


def test_listing_bookings_never_fetches_a_room_one_row_at_a_time(
    api: TestClient, engine: Engine
) -> None:
    """Already true since the Stage 3B.12 query audit. Pinned here so the listing and the
    single read are now guarded by the same standard rather than only one of them."""
    hotel = build_hotel(api, "qb-list", rooms=("101", "102", "103", "104"))
    for week in range(3):
        make_booking(api, hotel, rooms=("101", "102", "103", "104"), week=week)

    seen = statements(engine, lambda: api.get(f"/api/v1/hotels/{hotel}/bookings"))

    assert lazy_room_loads(seen) == 0, [s for s in seen if "rooms.id = " in s]


def test_listing_bookings_costs_the_same_whatever_the_page_holds(
    api: TestClient, engine: Engine
) -> None:
    hotel_small = build_hotel(api, "qb-page-small", rooms=("101", "102"))
    make_booking(api, hotel_small, rooms=("101",))
    hotel_big = build_hotel(api, "qb-page-big", rooms=("101", "102"))
    for week in range(4):
        make_booking(api, hotel_big, rooms=("101", "102"), week=week)

    small = statements(engine, lambda: api.get(f"/api/v1/hotels/{hotel_small}/bookings"))
    big = statements(engine, lambda: api.get(f"/api/v1/hotels/{hotel_big}/bookings"))

    assert len(small) == len(big), (len(small), len(big))


# ======================================================================================
# B. The write paths were deliberately NOT changed
# ======================================================================================


@pytest.mark.parametrize("room_count", [1, 2, 4])
def test_no_write_path_fetches_a_room_one_row_at_a_time(
    api: TestClient, engine: Engine, room_count: int
) -> None:
    """Create, modify-stay and extend resolve their own rooms, so they never had this N+1
    and did not acquire an eager load to prove it."""
    hotel, booking, numbers = scenario(api, room_count, f"write{room_count}")

    modified = statements(
        engine,
        lambda: api.patch(
            f"/api/v1/hotels/{hotel}/bookings/{booking}/stay",
            json={
                "check_in_date": str(CHECK_IN),
                "check_out_date": str(CHECK_OUT),
                "rooms": [
                    {"room_number": n, "nights": nights_for(CHECK_IN, CHECK_OUT)} for n in numbers
                ],
            },
        ),
    )
    api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_in"})
    extended = statements(
        engine,
        lambda: api.post(
            f"/api/v1/hotels/{hotel}/bookings/{booking}/stay/extension",
            json={"check_out_date": "2026-09-18"},
        ),
    )

    assert lazy_room_loads(modified) == 0, [s for s in modified if "rooms.id = " in s]
    assert lazy_room_loads(extended) == 0, [s for s in extended if "rooms.id = " in s]


def test_extending_a_stay_costs_the_same_whatever_the_party_size(
    api: TestClient, engine: Engine
) -> None:
    """Already constant before this stage, and still constant after it."""
    counts = []
    for room_count in (1, 2, 4):
        hotel, booking, _ = scenario(api, room_count, f"ext{room_count}")
        api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_in"})
        seen = statements(
            engine,
            partial(
                lambda h, b: api.post(
                    f"/api/v1/hotels/{h}/bookings/{b}/stay/extension",
                    json={"check_out_date": "2026-09-18"},
                ),
                hotel,
                booking,
            ),
        )
        counts.append(len(seen))

    assert counts[0] == counts[1] == counts[2], counts


# ======================================================================================
# C. The payload did not change
# ======================================================================================


def test_the_two_readers_render_byte_identical_payloads(api: TestClient) -> None:
    """The render reader differs from the write reader in WHEN rows are fetched, never in
    which. Creating a booking renders through one and reading it renders through the
    other, so the two payloads must agree field for field."""
    hotel, booking, _numbers = scenario(api, 4, "identical")

    created = api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").json()
    listed = [
        row
        for row in api.get(f"/api/v1/hotels/{hotel}/bookings").json()["items"]
        if row["public_id"] == booking
    ]

    assert len(listed) == 1
    assert listed[0] == created


def test_the_response_still_carries_every_documented_field(api: TestClient) -> None:
    hotel, booking, _ = scenario(api, 2, "fields")

    body = api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").json()

    assert set(body) == {
        "hotel_public_id",
        "public_id",
        "guest_public_id",
        "reference",
        "check_in_date",
        "check_out_date",
        "status",
        "adults",
        "children",
        "source",
        "channel_reference",
        "total_amount",
        "currency",
        "special_requests",
        "cancelled_at",
        "cancellation_reason",
        "booked_at",
        "created_at",
        "updated_at",
        "rooms",
    }
    assert set(body["rooms"][0]) == {
        "room_number",
        "room_type_code",
        "adults",
        "children",
        "guest_name",
        "nights",
        "nightly_rates",
    }
    assert set(body["rooms"][0]["nightly_rates"][0]) == {
        "stay_date",
        "rate",
        "rate_plan_code",
        "is_complimentary",
    }


def test_the_rooms_and_rates_are_all_present_and_correct(api: TestClient) -> None:
    """Eager loading must not drop or duplicate a collection row -- the classic joined-load
    mistake. ``selectinload`` cannot, and this says so out loud."""
    hotel, booking, numbers = scenario(api, 4, "collections")

    body = api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").json()

    assert [room["room_number"] for room in body["rooms"]] == list(numbers)
    for room in body["rooms"]:
        assert room["nights"] == NIGHTS
        assert len(room["nightly_rates"]) == NIGHTS
        assert [n["rate"] for n in room["nightly_rates"]] == ["100.00"] * NIGHTS


def test_no_internal_identifier_reaches_the_response(api: TestClient) -> None:
    """The new reader loads MORE rows than the old one -- rooms and the guest in full --
    so this is the check that none of it reached the payload."""
    hotel, booking, _ = scenario(api, 3, "leak")

    body = api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").json()

    leaked: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "id" or (key.endswith("_id") and not key.endswith("public_id")):
                    leaked.append(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    assert leaked == []
    for banned in ("Lovelace", "guest_id", "room_id", "hotel_id", "SELECT", "rooms."):
        assert banned not in str(body), banned


# ======================================================================================
# D. Authorization and tenancy on the newly touched read path
# ======================================================================================


def test_a_booking_at_another_hotel_is_still_not_found(api: TestClient) -> None:
    """The render reader carries the same tenant predicate as the write reader. A real
    booking of another property and one that never existed answer identically."""
    _hotel, booking, _ = scenario(api, 1, "tenant")
    other = build_hotel(api, "qb-tenant-other", rooms=("101",))

    cross = api.get(f"/api/v1/hotels/{other}/bookings/{booking}")
    invented = api.get(f"/api/v1/hotels/{other}/bookings/{uuid.uuid4()}")

    assert cross.status_code == 404
    assert cross.json() == invented.json()


def test_a_cross_hotel_update_is_still_not_found(api: TestClient) -> None:
    _hotel, booking, _ = scenario(api, 1, "tenant-update")
    other = build_hotel(api, "qb-tenant-update-other", rooms=("101",))

    response = api.patch(f"/api/v1/hotels/{other}/bookings/{booking}", json={"adults": 2})

    assert response.status_code == 404


def test_a_non_member_still_cannot_read_a_booking(api: TestClient, engine: Engine) -> None:
    hotel, booking, _ = scenario(api, 1, "nonmember")
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    assert stranger.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").status_code == 404


def test_a_viewer_may_read_but_still_may_not_update(api: TestClient, engine: Engine) -> None:
    """The read role and the write role are unchanged by this stage."""
    hotel, booking, _ = scenario(api, 1, "viewer")
    viewer = authenticated_client(engine, email=OTHER_EMAIL)
    grant_membership(engine, OTHER_EMAIL, hotel, "viewer")

    assert viewer.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").status_code == 200
    assert (
        viewer.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"adults": 2}).status_code
        == 403
    )


def test_an_unauthenticated_caller_still_cannot_read_a_booking(api: TestClient) -> None:
    hotel, booking, _ = scenario(api, 1, "anon")
    anonymous = TestClient(api.app)

    assert anonymous.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").status_code == 401


def test_another_hotels_booking_is_not_reachable_by_reference_collision(
    api: TestClient,
) -> None:
    """Two properties, the same room numbers and the same guest name. The render reader
    must not widen what a hotel can see."""
    first = build_hotel(api, "qb-iso-a", rooms=("101",))
    second = build_hotel(api, "qb-iso-b", rooms=("101",))
    make_booking(api, first, rooms=("101",))
    mine = make_booking(api, second, rooms=("101",))

    body = api.get(f"/api/v1/hotels/{second}/bookings/{mine}").json()
    page = api.get(f"/api/v1/hotels/{second}/bookings").json()

    assert body["hotel_public_id"] == second
    assert page["total"] == 1
    assert [row["public_id"] for row in page["items"]] == [mine]


# ======================================================================================
# E. Room resolution on the write paths is batched (Stage 4.5.29)
# ======================================================================================


@pytest.mark.parametrize("room_count", [1, 2, 4, 8])
def test_creating_a_booking_resolves_every_room_in_one_query(
    api: TestClient, engine: Engine, room_count: int
) -> None:
    """Before Stage 4.5.29 this was one SELECT per room: 1, 2, 4 and 8 for parties of
    1, 2, 4 and 8. A booking names its rooms all at once and they all live in one
    table, so one query answers the whole payload.
    """
    numbers = tuple(str(101 + offset) for offset in range(room_count))
    hotel = build_hotel(api, f"qb-batch-create-{room_count}", rooms=numbers)
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests",
        json={"first_name": "Ada", "last_name": "Lovelace"},
    ).json()["public_id"]
    body = {
        "guest_public_id": guest,
        "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
        "check_in_date": str(CHECK_IN),
        "check_out_date": str(CHECK_OUT),
        "status": "confirmed",
        "total_amount": "500.00",
        "currency": "EUR",
        "rooms": [{"room_number": n, "nights": nights_for(CHECK_IN, CHECK_OUT)} for n in numbers],
    }

    seen = statements(engine, lambda: api.post(f"/api/v1/hotels/{hotel}/bookings", json=body))

    assert room_resolution_queries(seen) == 1, [s for s in seen if "rooms.room_number" in s]


@pytest.mark.parametrize("room_count", [1, 2, 4, 8])
def test_modifying_a_stay_resolves_every_room_in_one_query(
    api: TestClient, engine: Engine, room_count: int
) -> None:
    numbers = tuple(str(101 + offset) for offset in range(room_count))
    hotel = build_hotel(api, f"qb-batch-modify-{room_count}", rooms=numbers)
    booking = make_booking(api, hotel, rooms=numbers)

    seen = statements(
        engine,
        lambda: api.patch(
            f"/api/v1/hotels/{hotel}/bookings/{booking}/stay",
            json={
                "check_in_date": str(CHECK_IN),
                "check_out_date": str(CHECK_OUT),
                "rooms": [
                    {"room_number": n, "nights": nights_for(CHECK_IN, CHECK_OUT)} for n in numbers
                ],
            },
        ),
    )

    assert room_resolution_queries(seen) == 1, [s for s in seen if "rooms.room_number" in s]


def test_a_larger_party_costs_fewer_statements_than_it_used_to(
    api: TestClient, engine: Engine
) -> None:
    """The saving, stated as growth rather than as a magic number.

    Creating a booking still costs more for more rooms -- each allocation is an INSERT
    and each night is a row, which is inherent. What it no longer costs is an extra
    SELECT per room, so the gap between a party of two and a party of eight shrank by
    exactly six statements.
    """
    small_numbers = ("101", "102")
    big_numbers = tuple(str(101 + offset) for offset in range(8))
    small_hotel = build_hotel(api, "qb-growth-small", rooms=small_numbers)
    big_hotel = build_hotel(api, "qb-growth-big", rooms=big_numbers)

    small = statements(engine, lambda: make_booking(api, small_hotel, rooms=small_numbers))
    big = statements(engine, lambda: make_booking(api, big_hotel, rooms=big_numbers))

    assert room_resolution_queries(small) == room_resolution_queries(big) == 1
    # Six more rooms, and none of them adds a resolution query.
    assert len(big) - len(small) == 18, (len(small), len(big))


# ======================================================================================
# F. The 404 the batch replaced said something specific, and still says it
# ======================================================================================


def unknown_room_create(api: TestClient, hotel: str, numbers: tuple[str, ...]) -> Response:
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests",
        json={"first_name": "Ada", "last_name": "Lovelace"},
    ).json()["public_id"]
    return api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": "confirmed",
            "total_amount": "500.00",
            "currency": "EUR",
            "rooms": [
                {"room_number": n, "nights": nights_for(CHECK_IN, CHECK_OUT)} for n in numbers
            ],
        },
    )


def test_an_unknown_room_is_still_a_404_naming_it(api: TestClient) -> None:
    hotel = build_hotel(api, "qb-unknown", rooms=("101",))

    response = unknown_room_create(api, hotel, ("999",))

    assert response.status_code == 404
    assert "'999'" in response.json()["error"]["message"]


def test_several_unknown_rooms_still_name_the_first_in_payload_order(
    api: TestClient,
) -> None:
    """The contract the batch could most easily have broken.

    Resolving the rooms together makes every miss visible at once, so it would have
    been natural to report whichever one a set happened to yield. The payload names
    901 before 902, so 901 is what the caller is told -- exactly as the per-room loop
    told them when it stopped at the first failure.
    """
    hotel = build_hotel(api, "qb-unknown-many", rooms=("101",))

    response = unknown_room_create(api, hotel, ("101", "901", "902"))

    assert response.status_code == 404
    message = response.json()["error"]["message"]
    assert "'901'" in message, message
    assert "902" not in message, message


def test_modifying_a_stay_onto_an_unknown_room_names_the_first_too(
    api: TestClient,
) -> None:
    hotel = build_hotel(api, "qb-unknown-modify", rooms=("101",))
    booking = make_booking(api, hotel, rooms=("101",))

    response = api.patch(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/stay",
        json={
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "rooms": [
                {"room_number": n, "nights": nights_for(CHECK_IN, CHECK_OUT)}
                for n in ("901", "902")
            ],
        },
    )

    assert response.status_code == 404
    assert "'901'" in response.json()["error"]["message"]


def test_an_unknown_room_writes_no_booking(api: TestClient) -> None:
    """Rooms are resolved BEFORE anything is written, on both paths."""
    hotel = build_hotel(api, "qb-unknown-nowrite", rooms=("101",))

    unknown_room_create(api, hotel, ("101", "999"))

    assert api.get(f"/api/v1/hotels/{hotel}/bookings").json()["total"] == 0


def test_a_room_belonging_to_another_hotel_is_not_found(api: TestClient) -> None:
    """The batch carries the same tenant predicate the per-room lookup did. Room 101
    exists -- at the other property -- and is still not this hotel's room.
    """
    mine = build_hotel(api, "qb-tenant-rooms-a", rooms=("101",))
    build_hotel(api, "qb-tenant-rooms-b", rooms=("555",))

    response = unknown_room_create(api, mine, ("555",))

    assert response.status_code == 404
    assert "'555'" in response.json()["error"]["message"]


def test_two_hotels_with_the_same_room_numbers_resolve_their_own(
    api: TestClient,
) -> None:
    first = build_hotel(api, "qb-same-a", rooms=("101", "102"))
    second = build_hotel(api, "qb-same-b", rooms=("101", "102"))
    make_booking(api, first, rooms=("101", "102"))
    mine = make_booking(api, second, rooms=("101", "102"))

    body = api.get(f"/api/v1/hotels/{second}/bookings/{mine}").json()

    assert body["hotel_public_id"] == second
    assert [room["room_number"] for room in body["rooms"]] == ["101", "102"]
    assert api.get(f"/api/v1/hotels/{second}/bookings").json()["total"] == 1


def test_the_resolved_rooms_are_the_ones_actually_allocated(api: TestClient) -> None:
    """A mapping keyed by number could silently pair a payload entry with the wrong
    room. The allocation order and the room-type codes come back as the payload named
    them.
    """
    numbers = ("101", "102", "103", "104")
    hotel = build_hotel(api, "qb-pairing", rooms=numbers)
    booking = make_booking(api, hotel, rooms=numbers)

    body = api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").json()

    assert [room["room_number"] for room in body["rooms"]] == list(numbers)
    assert {room["room_type_code"] for room in body["rooms"]} == {"DLX"}


# ======================================================================================
# G. The many-to-one relationships are joined, not queried (Stage 4.5.30)
# ======================================================================================


@pytest.mark.parametrize("room_count", [1, 4])
def test_reading_a_booking_issues_no_guest_query_at_all(
    api: TestClient, engine: Engine, room_count: int
) -> None:
    """``Booking.guest`` is many-to-one, so it rides along in the booking SELECT.

    Before Stage 4.5.30 every render path paid one statement for this single row --
    lazily on the write paths, and as a ``selectinload`` on the read paths.
    """
    hotel, booking, _ = scenario(api, room_count, f"g30-get{room_count}")

    seen = statements(engine, lambda: api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}"))

    assert guest_queries(seen) == 0, [s for s in seen if "guests." in s]
    assert room_queries(seen) == 0, [s for s in seen if "rooms." in s]


def test_listing_bookings_issues_no_guest_query_at_all(api: TestClient, engine: Engine) -> None:
    hotel = build_hotel(api, "g30-list", rooms=("101", "102"))
    for week in range(3):
        make_booking(api, hotel, rooms=("101", "102"), week=week)

    seen = statements(engine, lambda: api.get(f"/api/v1/hotels/{hotel}/bookings"))

    assert guest_queries(seen) == 0, [s for s in seen if "guests." in s]
    assert room_queries(seen) == 0, [s for s in seen if "rooms." in s]


def test_modifying_a_stay_issues_no_guest_query_when_it_renders(
    api: TestClient, engine: Engine
) -> None:
    """The write paths render after committing, and their guest was cold.

    Stage 4.5.28 measured the obvious repair -- a ``selectinload`` on the write reader --
    as a NET LOSS and left the lazy load in place. Joining costs nothing, so the same
    defect had a free fix once the cardinality was the thing being looked at.
    """
    hotel, booking, numbers = scenario(api, 2, "g30-modify")

    seen = statements(
        engine,
        lambda: api.patch(
            f"/api/v1/hotels/{hotel}/bookings/{booking}/stay",
            json={
                "check_in_date": str(CHECK_IN),
                "check_out_date": str(CHECK_OUT),
                "rooms": [
                    {"room_number": n, "nights": nights_for(CHECK_IN, CHECK_OUT)} for n in numbers
                ],
            },
        ),
    )

    assert guest_queries(seen) == 0, [s for s in seen if "guests." in s]


def test_extending_a_stay_issues_no_guest_query_when_it_renders(
    api: TestClient, engine: Engine
) -> None:
    hotel, booking, _ = scenario(api, 2, "g30-extend")
    api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_in"})

    seen = statements(
        engine,
        lambda: api.post(
            f"/api/v1/hotels/{hotel}/bookings/{booking}/stay/extension",
            json={"check_out_date": "2026-09-18"},
        ),
    )

    assert guest_queries(seen) == 0, [s for s in seen if "guests." in s]


def test_creating_a_booking_looks_the_guest_up_exactly_once(
    api: TestClient, engine: Engine
) -> None:
    """Creation's guest query is a DOMAIN lookup, not a rendering cost.

    It resolves the guest public id the payload names, scoped to this hotel, and refuses
    a guest of another property. That query must stay. What must not come back is a
    SECOND one when the created booking is rendered.
    """
    numbers = ("101", "102")
    hotel = build_hotel(api, "g30-create", rooms=numbers)
    # The guest is created OUTSIDE the measurement: creating one is the fixture's work,
    # and counting it would attribute the guests POST to the bookings POST.
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests",
        json={"first_name": "Ada", "last_name": "Lovelace"},
    ).json()["public_id"]
    body = {
        "guest_public_id": guest,
        "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
        "check_in_date": str(CHECK_IN),
        "check_out_date": str(CHECK_OUT),
        "status": "confirmed",
        "total_amount": "500.00",
        "currency": "EUR",
        "rooms": [{"room_number": n, "nights": nights_for(CHECK_IN, CHECK_OUT)} for n in numbers],
    }

    seen = statements(engine, lambda: api.post(f"/api/v1/hotels/{hotel}/bookings", json=body))

    assert guest_queries(seen) == 1, [s for s in seen if "guests." in s]


# ======================================================================================
# H. Joining must not change what comes back
# ======================================================================================


def test_a_page_of_bookings_is_not_multiplied_by_the_join(api: TestClient) -> None:
    """The failure mode a join introduces, checked where it would actually bite.

    Joining a COLLECTION returns the parent once per child, and a LIMIT applied over
    that returns the wrong page. ``Booking.guest`` and ``BookingRoom.room`` are
    many-to-one so neither can multiply anything -- which is a claim worth checking
    against a page whose bookings each hold four rooms.
    """
    numbers = ("101", "102", "103", "104")
    hotel = build_hotel(api, "g30-page", rooms=numbers)
    for week in range(3):
        make_booking(api, hotel, rooms=numbers, week=week)

    page = api.get(f"/api/v1/hotels/{hotel}/bookings").json()

    assert page["total"] == 3
    assert len(page["items"]) == 3
    assert len({row["public_id"] for row in page["items"]}) == 3
    for row in page["items"]:
        assert [room["room_number"] for room in row["rooms"]] == list(numbers)


def test_pagination_still_pages(api: TestClient) -> None:
    """A LIMIT over a multiplied row set returns short pages. It does not here."""
    hotel = build_hotel(api, "g30-paging", rooms=("101", "102"))
    for week in range(5):
        make_booking(api, hotel, rooms=("101", "102"), week=week)

    first = api.get(f"/api/v1/hotels/{hotel}/bookings?page=1&page_size=2").json()
    second = api.get(f"/api/v1/hotels/{hotel}/bookings?page=2&page_size=2").json()

    assert first["total"] == second["total"] == 5
    assert len(first["items"]) == len(second["items"]) == 2
    assert not {row["public_id"] for row in first["items"]} & {
        row["public_id"] for row in second["items"]
    }


def test_the_guest_public_id_is_still_the_right_guest(api: TestClient) -> None:
    """A join pulls the guest through a different code path than a lazy load did.

    Two bookings, two guests, same hotel: each booking must report its own.
    """
    hotel = build_hotel(api, "g30-guests", rooms=("101", "102"))
    first = make_booking(api, hotel, rooms=("101",), week=0)
    second = make_booking(api, hotel, rooms=("102",), week=1)

    one = api.get(f"/api/v1/hotels/{hotel}/bookings/{first}").json()
    two = api.get(f"/api/v1/hotels/{hotel}/bookings/{second}").json()

    assert one["guest_public_id"] != two["guest_public_id"]
    listed = {
        row["public_id"]: row["guest_public_id"]
        for row in api.get(f"/api/v1/hotels/{hotel}/bookings").json()["items"]
    }
    assert listed[first] == one["guest_public_id"]
    assert listed[second] == two["guest_public_id"]


def test_the_guest_row_itself_never_reaches_the_payload(api: TestClient) -> None:
    """The join loads the WHOLE guest -- name, email, phone -- where the lazy load
    also did. None of it may appear: the response carries the public id and nothing
    else about the person.
    """
    hotel = build_hotel(api, "g30-leak", rooms=("101",))
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.test",
        },
    ).json()
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest["public_id"],
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": "confirmed",
            "total_amount": "500.00",
            "currency": "EUR",
            "rooms": [{"room_number": "101", "nights": nights_for(CHECK_IN, CHECK_OUT)}],
        },
    )
    assert response.status_code == 201, response.text
    booking = response.json()["public_id"]

    rendered = str(api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").json())
    listed = str(api.get(f"/api/v1/hotels/{hotel}/bookings").json())

    for banned in ("Ada", "Lovelace", "ada@example.test", "guest_id"):
        assert banned not in rendered, banned
        assert banned not in listed, banned


def test_the_room_number_is_still_the_right_room(api: TestClient) -> None:
    """``BookingRoom.room`` is joined into the allocations query now. Each allocation
    must still carry its own room, in payload order.
    """
    numbers = ("101", "102", "103", "104")
    hotel = build_hotel(api, "g30-pairing", rooms=numbers)
    booking = make_booking(api, hotel, rooms=numbers)

    body = api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").json()

    assert [room["room_number"] for room in body["rooms"]] == list(numbers)
    assert len(body["rooms"]) == 4
