"""The booking lifecycle over HTTP, against real PostgreSQL.

Stage 4.5.7. `tests/backend/test_booking_status_machine.py` pins the transition graph as a
pure function; this suite pins that the API actually consults it -- a correct policy wired up
loosely would pass the unit tests and still let a client cancel a checked-out stay.

Three things here can only be tested against a real database: that a refused transition leaves
the row untouched, that the row lock serialises two competing transitions rather than letting
both commit against a stale read, and that a booking belonging to another hotel is refused by
the same 404 wall as a booking that does not exist.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    requires_postgres,
)

SUITE_EMAIL = "booking-status@example.test"
OTHER_EMAIL = "booking-status-other@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 11, 2)
CHECK_OUT = dt.date(2026, 11, 5)

ALL_STATUSES = ["pending", "confirmed", "checked_in", "checked_out", "cancelled", "no_show"]

#: Restated here rather than imported, for the same reason as in the unit suite: a test that
#: derives its expectation from the code under test agrees with any change to it.
ALLOWED: dict[str, set[str]] = {
    "pending": {"pending", "confirmed", "cancelled"},
    "confirmed": {"confirmed", "checked_in", "cancelled", "no_show"},
    "checked_in": {"checked_in", "checked_out"},
    "checked_out": {"checked_out"},
    "cancelled": {"cancelled"},
    "no_show": {"no_show"},
}

#: How to reach each status from a freshly created booking, using only legal hops. Creation
#: sets the first element; the rest are PATCHes.
ROUTE: dict[str, list[str]] = {
    "pending": ["pending"],
    "confirmed": ["confirmed"],
    "checked_in": ["confirmed", "checked_in"],
    "checked_out": ["confirmed", "checked_in", "checked_out"],
    "cancelled": ["confirmed", "cancelled"],
    "no_show": ["confirmed", "no_show"],
}


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


def bookings_url(hotel_public_id: str) -> str:
    return f"/api/v1/hotels/{hotel_public_id}/bookings"


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build_hotel(api: TestClient, slug: str, rooms: tuple[str, ...] = ("101", "102")) -> str:
    public_id = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    api.post(
        f"/api/v1/hotels/{public_id}/room-types",
        json={
            "code": "DLX",
            "name": "Deluxe",
            "max_occupancy": 3,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": "120.00",
            "currency": "EUR",
        },
    )
    for number in rooms:
        api.post(f"/api/v1/hotels/{public_id}/room-types/DLX/rooms", json={"room_number": number})
    return public_id


@pytest.fixture
def hotel_id(api: TestClient) -> str:
    return build_hotel(api, "status-hotel")


@pytest.fixture
def guest_id(api: TestClient, hotel_id: str) -> str:
    return str(
        api.post(
            f"/api/v1/hotels/{hotel_id}/guests",
            json={"first_name": "Ada", "last_name": "Lovelace"},
        ).json()["public_id"]
    )


def booking_payload(guest_public_id: str, status: str, room: str = "101") -> dict[str, object]:
    return {
        "guest_public_id": guest_public_id,
        "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
        "check_in_date": str(CHECK_IN),
        "check_out_date": str(CHECK_OUT),
        "status": status,
        "adults": 2,
        "total_amount": "360.00",
        "currency": "EUR",
        "rooms": [
            {
                "room_number": room,
                "nights": [{"stay_date": str(CHECK_IN + dt.timedelta(days=n))} for n in range(3)],
            }
        ],
    }


def booking_in(
    api: TestClient, hotel_id: str, guest_id: str, status: str, room: str = "101"
) -> str:
    """Create a booking and walk it to *status* using only legal hops.

    Every hop is asserted, so a route that silently stopped working could not leave a booking
    in the wrong state and quietly make a later assertion vacuous.
    """
    route = ROUTE[status]
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id, route[0], room))
    assert created.status_code == 201, created.text
    public_id = str(created.json()["public_id"])

    for step in route[1:]:
        moved = api.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": step})
        assert moved.status_code == 200, f"setup hop to {step} failed: {moved.text}"

    current = api.get(f"{bookings_url(hotel_id)}/{public_id}").json()["status"]
    assert current == status
    return public_id


# ======================================================================================
# The matrix, over HTTP
# ======================================================================================


@pytest.mark.parametrize("requested", ALL_STATUSES)
@pytest.mark.parametrize("current", ALL_STATUSES)
def test_the_transition_matrix_over_http(
    api: TestClient, hotel_id: str, guest_id: str, current: str, requested: str
) -> None:
    """All 36 ordered pairs, exercised through the endpoint rather than the policy."""
    public_id = booking_in(api, hotel_id, guest_id, current)

    response = api.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": requested})

    expected = 200 if requested in ALLOWED[current] else 409
    assert response.status_code == expected, response.text

    settled = api.get(f"{bookings_url(hotel_id)}/{public_id}").json()["status"]
    assert settled == (requested if expected == 200 else current)


def test_a_refused_transition_changes_nothing_at_all(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """Not merely the status: the whole representation is untouched."""
    public_id = booking_in(api, hotel_id, guest_id, "cancelled")
    before = api.get(f"{bookings_url(hotel_id)}/{public_id}").json()

    refused = api.patch(
        f"{bookings_url(hotel_id)}/{public_id}",
        json={"status": "confirmed", "special_requests": "should not be applied"},
    )

    assert refused.status_code == 409
    assert api.get(f"{bookings_url(hotel_id)}/{public_id}").json() == before


def test_a_legal_transition_still_applies_the_other_fields(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """The guard refuses; it does not otherwise change what an accepted PATCH does."""
    public_id = booking_in(api, hotel_id, guest_id, "confirmed")

    response = api.patch(
        f"{bookings_url(hotel_id)}/{public_id}",
        json={"status": "cancelled", "cancellation_reason": "Guest request"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancellation_reason"] == "Guest request"
    assert response.json()["cancelled_at"] is not None


def test_a_patch_that_omits_status_is_unaffected(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """No status in the body means no transition to judge -- including from a terminal
    state, where editing a note is still legitimate."""
    public_id = booking_in(api, hotel_id, guest_id, "checked_out")

    response = api.patch(
        f"{bookings_url(hotel_id)}/{public_id}", json={"special_requests": "late invoice"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "checked_out"


# ======================================================================================
# Same-status: idempotent
# ======================================================================================


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_repeating_a_status_is_accepted(
    api: TestClient, hotel_id: str, guest_id: str, status: str
) -> None:
    """A retried PATCH must not become a conflict. See the unit suite for the reasoning."""
    public_id = booking_in(api, hotel_id, guest_id, status)

    first = api.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": status})
    second = api.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": status})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == status


def test_repeating_a_cancellation_does_not_move_the_timestamp(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """Idempotence has to reach the derived field too, or a retry silently rewrites when the
    booking was cancelled."""
    public_id = booking_in(api, hotel_id, guest_id, "cancelled")
    first = api.get(f"{bookings_url(hotel_id)}/{public_id}").json()["cancelled_at"]

    again = api.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": "cancelled"})

    assert again.status_code == 200
    assert again.json()["cancelled_at"] == first


# ======================================================================================
# Error shape: safe, and in the project's existing envelope
# ======================================================================================


def test_an_illegal_transition_uses_the_shared_error_envelope(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    public_id = booking_in(api, hotel_id, guest_id, "checked_out")

    response = api.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": "cancelled"})
    body = response.json()

    assert response.status_code == 409
    assert set(body) == {"error"}
    assert body["error"]["code"] == "CONFLICT"
    assert set(body["error"]) == {"code", "message", "details"}


def test_the_message_names_the_statuses_and_leaks_nothing_else(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """The two statuses are public vocabulary the caller just sent or can already read.
    Everything else about how the refusal was reached stays inside."""
    public_id = booking_in(api, hotel_id, guest_id, "checked_in")

    response = api.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": "pending"})
    text = response.text

    assert "checked_in" in text
    assert "pending" in text
    for leak in [
        "SELECT",
        "UPDATE",
        "bookings",
        "booking_rooms",
        "ck_bookings",
        "excl_booking_rooms",
        "23505",
        "23514",
        "23P01",
        "sqlalchemy",
        "psycopg",
        "Traceback",
        "FOR UPDATE",
    ]:
        assert leak not in text, f"the refusal leaked {leak!r}"


def test_the_refusal_exposes_no_internal_identifier(
    api: TestClient, hotel_id: str, guest_id: str, session: object
) -> None:
    """Internal BIGINT ids never reach a response, error envelopes included."""
    public_id = booking_in(api, hotel_id, guest_id, "no_show")

    response = api.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": "checked_in"})
    body = response.json()

    assert response.status_code == 409
    assert "id" not in body["error"]
    assert public_id not in body["error"]["message"]


def test_a_terminal_booking_says_so_rather_than_naming_a_target(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """A dead end and a wrong hop are different failures and read differently."""
    terminal = booking_in(api, hotel_id, guest_id, "cancelled")
    forward = booking_in(api, hotel_id, guest_id, "pending", room="102")

    dead_end = api.patch(f"{bookings_url(hotel_id)}/{terminal}", json={"status": "confirmed"})
    wrong_hop = api.patch(f"{bookings_url(hotel_id)}/{forward}", json={"status": "checked_out"})

    assert "no longer change status" in dead_end.json()["error"]["message"]
    assert "cannot move from" in wrong_hop.json()["error"]["message"]


# ======================================================================================
# Authorization and tenant isolation -- unchanged by this stage, and proven so
# ======================================================================================


def test_a_viewer_still_cannot_change_a_status(
    api: TestClient, engine: Engine, hotel_id: str, guest_id: str
) -> None:
    """The state machine answers "is this transition valid"; it must never become the thing
    that decides who may ask. A viewer is refused before the policy is consulted."""
    public_id = booking_in(api, hotel_id, guest_id, "confirmed")
    # The client first: `grant_membership` matches on an EXISTING user row, so granting
    # before registration silently inserts nothing and the caller would be a non-member --
    # which answers 404, not the 403 this test is about.
    viewer = authenticated_client(engine, email=OTHER_EMAIL)
    grant_membership(engine, OTHER_EMAIL, hotel_id, "viewer")

    response = viewer.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": "cancelled"})

    assert response.status_code == 403
    assert api.get(f"{bookings_url(hotel_id)}/{public_id}").json()["status"] == "confirmed"


def test_a_non_member_still_meets_the_404_wall(
    api: TestClient, engine: Engine, hotel_id: str, guest_id: str
) -> None:
    """Membership is not disclosed by the status endpoint any more than by any other."""
    public_id = booking_in(api, hotel_id, guest_id, "confirmed")
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    response = stranger.patch(f"{bookings_url(hotel_id)}/{public_id}", json={"status": "cancelled"})

    assert response.status_code == 404
    assert api.get(f"{bookings_url(hotel_id)}/{public_id}").json()["status"] == "confirmed"


def test_an_unauthenticated_caller_cannot_change_a_status(
    api: TestClient, engine: Engine, hotel_id: str, guest_id: str
) -> None:
    public_id = booking_in(api, hotel_id, guest_id, "confirmed")
    anonymous = TestClient(api.app)

    response = anonymous.patch(
        f"{bookings_url(hotel_id)}/{public_id}", json={"status": "cancelled"}
    )

    assert response.status_code == 401


def test_a_booking_at_another_hotel_cannot_be_transitioned(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """Tenant isolation: the same owner, two hotels, and the booking is invisible from the
    wrong one -- 404, identical to a booking that does not exist."""
    public_id = booking_in(api, hotel_id, guest_id, "confirmed")
    other_hotel = build_hotel(api, "status-hotel-b")

    response = api.patch(f"{bookings_url(other_hotel)}/{public_id}", json={"status": "cancelled"})
    invented = api.patch(
        f"{bookings_url(other_hotel)}/{uuid.uuid4()}", json={"status": "cancelled"}
    )

    assert response.status_code == 404
    assert invented.status_code == 404
    assert response.json() == invented.json()
    assert api.get(f"{bookings_url(hotel_id)}/{public_id}").json()["status"] == "confirmed"


# ======================================================================================
# Concurrency
# ======================================================================================


def test_two_competing_transitions_cannot_both_win(
    api: TestClient, engine: Engine, hotel_id: str, guest_id: str
) -> None:
    """The lost-update race the row lock exists to close.

    Both requests start from ``confirmed``, and ``checked_in`` and ``no_show`` are each legal
    from there. Without ``SELECT ... FOR UPDATE`` both read ``confirmed``, both validate, and
    both commit -- measured before the fix -- leaving a booking that reached its final status
    through a hop (``checked_in -> no_show``) that the lifecycle forbids and nothing checked.

    With the lock the second request blocks, then re-reads the status the first one committed
    and is judged against it. Whichever order the threads arrive in, exactly one succeeds and
    the loser is refused, because neither ``checked_in -> no_show`` nor ``no_show ->
    checked_in`` is legal.
    """
    public_id = booking_in(api, hotel_id, guest_id, "confirmed")
    url = f"{bookings_url(hotel_id)}/{public_id}"
    both_ready = threading.Barrier(2, timeout=20)
    codes: dict[str, int] = {}

    def attempt(name: str, status: str) -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        both_ready.wait()
        codes[name] = client.patch(url, json={"status": status}).status_code

    threads = [
        threading.Thread(target=attempt, args=("a", "checked_in")),
        threading.Thread(target=attempt, args=("b", "no_show")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(codes.values()) == [200, 409], codes
    winner = "checked_in" if codes["a"] == 200 else "no_show"
    assert api.get(url).json()["status"] == winner
