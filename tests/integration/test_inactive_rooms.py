"""An inactive room is not for sale, and a room with stays ahead cannot be deactivated (Issue H1).

``is_active`` marks a room as sellable inventory: availability search never offers an inactive
room or a room of an inactive type. Before H1 every booking write accepted one anyway, by number,
and deactivating a room never looked at the stays still to come on it. Now:

* creating a booking, changing a stay, extending one, and moving a booking into an
  inventory-holding status are refused with 409 on an inactive room or a room of an inactive
  type -- whatever the booking's status;
* deactivating a room, or a room type, is refused with 409 while it (or one of its rooms) holds a
  confirmed or checked-in stay ending after the hotel's own today. Pending, cancelled, no-show and
  checked-out stays hold nothing; a stay ending today has ended;
* a sale and a deactivation of the same room serialise on row locks, so neither can slip in
  between the other's check and commit.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import ConflictError
from app.main import create_app
from app.models.hotel import Hotel
from app.models.room import Room, RoomType
from app.repositories.booking import BookingRepository
from app.repositories.room import RoomRepository
from app.repositories.room_type import RoomTypeRepository
from app.schemas.room import RoomUpdate
from app.schemas.room_type import RoomTypeUpdate
from app.services.room import RoomService
from app.services.room_type import RoomTypeService
from tests.integration.conftest import (
    allocate_room,
    authenticated_client,
    make_booking,
    make_guest,
    make_hotel,
    make_room,
    make_room_type,
    price_nights,
    requires_postgres,
)

pytestmark = requires_postgres

SUITE_EMAIL = "inactive-rooms@example.test"
#: Comfortably in the future and in the past, whatever day the suite runs on.
TODAY = dt.date.today()
AHEAD = TODAY + dt.timedelta(days=400)
BEHIND = TODAY - dt.timedelta(days=400)
INACTIVE = "Room '101' is inactive and cannot be booked."
INACTIVE_TYPE = "Room '101' belongs to an inactive room type and cannot be booked."


# --- the API ---------------------------------------------------------------------------------


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


@pytest.fixture
def hotel(api: TestClient) -> str:
    hotel = str(
        api.post(
            "/api/v1/hotels",
            json={
                "slug": "inactive-rooms",
                "name": "Inactive Rooms",
                "address_line1": "1 Ermou",
                "city": "Athens",
                "country_code": "GR",
                "timezone": "Europe/Athens",
                "currency": "EUR",
            },
        ).json()["public_id"]
    )
    for code in ("DLX", "STD"):
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
    for code, number in (("DLX", "101"), ("DLX", "102"), ("STD", "201")):
        created = api.post(
            f"/api/v1/hotels/{hotel}/room-types/{code}/rooms", json={"room_number": number}
        )
        assert created.status_code == 201, created.text
    return hotel


def room_url(hotel: str, number: str) -> str:
    code = "STD" if number.startswith("2") else "DLX"
    return f"/api/v1/hotels/{hotel}/room-types/{code}/rooms/{number}"


def type_url(hotel: str, code: str) -> str:
    return f"/api/v1/hotels/{hotel}/room-types/{code}"


def stay(number: str, check_in: dt.date, check_out: dt.date) -> dict[str, Any]:
    nights = (check_out - check_in).days
    return {
        "room_number": number,
        "nights": [{"stay_date": str(check_in + dt.timedelta(days=n))} for n in range(nights)],
    }


def create(
    api: TestClient,
    hotel: str,
    number: str,
    check_in: dt.date,
    check_out: dt.date,
    status: str = "confirmed",
) -> Any:
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    return api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(check_in),
            "check_out_date": str(check_out),
            "status": status,
            "total_amount": "100.00",
            "currency": "EUR",
            "rooms": [stay(number, check_in, check_out)],
        },
    )


def book(
    api: TestClient,
    hotel: str,
    number: str,
    check_in: dt.date,
    check_out: dt.date,
    status: str = "confirmed",
) -> str:
    response = create(api, hotel, number, check_in, check_out, status)
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def deactivate(api: TestClient, url: str) -> Any:
    return api.patch(url, json={"is_active": False})


def refused(response: Any, message: str) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT"
    assert response.json()["error"]["message"] == message


@pytest.mark.parametrize("status", ["pending", "confirmed", "checked_in"])
def test_a_booking_cannot_be_created_on_an_inactive_room(
    api: TestClient, hotel: str, status: str
) -> None:
    assert deactivate(api, room_url(hotel, "101")).status_code == 200

    refused(create(api, hotel, "101", AHEAD, AHEAD + dt.timedelta(days=2), status), INACTIVE)
    assert api.get(f"/api/v1/hotels/{hotel}/bookings").json()["total"] == 0


def test_a_booking_cannot_be_created_on_a_room_of_an_inactive_type(
    api: TestClient, hotel: str
) -> None:
    assert deactivate(api, type_url(hotel, "DLX")).status_code == 200

    refused(create(api, hotel, "101", AHEAD, AHEAD + dt.timedelta(days=2)), INACTIVE_TYPE)
    # The other type is still for sale.
    assert create(api, hotel, "201", AHEAD, AHEAD + dt.timedelta(days=2)).status_code == 201


def test_one_inactive_room_refuses_the_whole_party(api: TestClient, hotel: str) -> None:
    assert deactivate(api, room_url(hotel, "101")).status_code == 200
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    end = AHEAD + dt.timedelta(days=1)

    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": "BK-PARTY",
            "check_in_date": str(AHEAD),
            "check_out_date": str(end),
            "status": "confirmed",
            "total_amount": "100.00",
            "currency": "EUR",
            "rooms": [stay("102", AHEAD, end), stay("101", AHEAD, end)],
        },
    )

    refused(response, INACTIVE)
    assert api.get(f"/api/v1/hotels/{hotel}/bookings").json()["total"] == 0


def test_a_stay_cannot_be_moved_onto_an_inactive_room(api: TestClient, hotel: str) -> None:
    end = AHEAD + dt.timedelta(days=2)
    booking = book(api, hotel, "102", AHEAD, end)
    assert deactivate(api, room_url(hotel, "101")).status_code == 200

    moved = api.patch(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/stay",
        json={
            "check_in_date": str(AHEAD),
            "check_out_date": str(end),
            "rooms": [stay("101", AHEAD, end)],
        },
    )

    refused(moved, INACTIVE)
    body = api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").json()
    assert [room["room_number"] for room in body["rooms"]] == ["102"]


def test_a_pending_booking_on_a_retired_room_cannot_be_confirmed(
    api: TestClient, hotel: str
) -> None:
    """A pending stay holds nothing, so it does not stop the room being retired -- but it cannot
    then put the room back on sale by being confirmed. It can still be cancelled."""
    booking = book(api, hotel, "101", AHEAD, AHEAD + dt.timedelta(days=2), status="pending")
    assert deactivate(api, room_url(hotel, "101")).status_code == 200
    url = f"/api/v1/hotels/{hotel}/bookings/{booking}"

    refused(api.patch(url, json={"status": "confirmed"}), INACTIVE)
    assert api.get(url).json()["status"] == "pending"
    assert api.patch(url, json={"status": "cancelled"}).status_code == 200


def test_a_confirmed_stay_on_a_retired_room_cannot_be_checked_in(
    api: TestClient, hotel: str
) -> None:
    """A confirmed stay that ended without anyone moving it on does not block retiring the room;
    checking it in afterwards would hold the room again, and is refused."""
    booking = book(api, hotel, "101", BEHIND, BEHIND + dt.timedelta(days=2))
    assert deactivate(api, room_url(hotel, "101")).status_code == 200
    url = f"/api/v1/hotels/{hotel}/bookings/{booking}"

    refused(api.patch(url, json={"status": "checked_in"}), INACTIVE)
    assert api.get(url).json()["status"] == "confirmed"


def test_a_stay_on_an_inactive_room_cannot_be_extended(api: TestClient, hotel: str) -> None:
    """A stay that ended but was never checked out does not stop the room being retired; the
    added nights of an extension would be a new sale of it, and are refused."""
    check_out = TODAY - dt.timedelta(days=2)
    booking = book(
        api, hotel, "101", check_out - dt.timedelta(days=2), check_out, status="checked_in"
    )
    assert deactivate(api, room_url(hotel, "101")).status_code == 200

    extended = api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/stay/extension",
        json={"check_out_date": str(check_out + dt.timedelta(days=1))},
    )

    refused(extended, INACTIVE)
    assert api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}").json()["check_out_date"] == str(
        check_out
    )


def test_an_ordinary_edit_of_a_booking_on_an_inactive_room_is_not_refused(
    api: TestClient, hotel: str
) -> None:
    booking = book(api, hotel, "101", BEHIND, BEHIND + dt.timedelta(days=2))
    assert deactivate(api, room_url(hotel, "101")).status_code == 200
    url = f"/api/v1/hotels/{hotel}/bookings/{booking}"

    assert api.patch(url, json={"adults": 2}).status_code == 200
    assert api.patch(url, json={"status": "confirmed"}).status_code == 200  # no change
    assert api.patch(url, json={"status": "no_show"}).status_code == 200


def test_a_room_with_a_stay_ahead_cannot_be_deactivated(api: TestClient, hotel: str) -> None:
    book(api, hotel, "101", AHEAD, AHEAD + dt.timedelta(days=2))

    response = deactivate(api, room_url(hotel, "101"))

    assert response.status_code == 409, response.text
    message = response.json()["error"]["message"]
    assert message.startswith("Room '101' cannot be deactivated while it holds a confirmed or ")
    assert api.get(room_url(hotel, "101")).json()["is_active"] is True


def test_a_room_type_with_a_stay_ahead_on_any_room_cannot_be_deactivated(
    api: TestClient, hotel: str
) -> None:
    book(api, hotel, "102", AHEAD, AHEAD + dt.timedelta(days=2), status="checked_in")

    response = deactivate(api, type_url(hotel, "DLX"))

    assert response.status_code == 409, response.text
    assert response.json()["error"]["message"].startswith(
        "Room type 'DLX' cannot be deactivated while one of its rooms holds a confirmed or "
    )
    assert api.get(type_url(hotel, "DLX")).json()["is_active"] is True
    # Another type's rooms hold nothing, so it can go.
    assert deactivate(api, type_url(hotel, "STD")).status_code == 200


@pytest.mark.parametrize("status", ["pending", "cancelled", "no_show", "checked_out"])
def test_a_stay_that_holds_nothing_does_not_block_deactivation(
    api: TestClient, hotel: str, status: str
) -> None:
    book(api, hotel, "101", AHEAD, AHEAD + dt.timedelta(days=2), status=status)

    assert deactivate(api, room_url(hotel, "101")).status_code == 200
    assert deactivate(api, type_url(hotel, "DLX")).status_code == 200


def test_a_stay_that_has_ended_does_not_block_deactivation(api: TestClient, hotel: str) -> None:
    """Including a confirmed or checked-in one nobody moved on: it is history, not a sale."""
    book(api, hotel, "101", BEHIND, BEHIND + dt.timedelta(days=2))
    book(api, hotel, "102", BEHIND, BEHIND + dt.timedelta(days=2), status="checked_in")

    assert deactivate(api, room_url(hotel, "101")).status_code == 200
    assert deactivate(api, type_url(hotel, "DLX")).status_code == 200


def test_a_reactivated_room_is_for_sale_again(api: TestClient, hotel: str) -> None:
    assert deactivate(api, room_url(hotel, "101")).status_code == 200
    assert api.patch(room_url(hotel, "101"), json={"is_active": True}).status_code == 200

    assert create(api, hotel, "101", AHEAD, AHEAD + dt.timedelta(days=2)).status_code == 201


def test_deactivating_an_inactive_room_again_is_not_refused(api: TestClient, hotel: str) -> None:
    """Nothing is being taken off sale, so there is nothing to protect."""
    assert deactivate(api, room_url(hotel, "101")).status_code == 200
    book(api, hotel, "102", AHEAD, AHEAD + dt.timedelta(days=2))

    assert deactivate(api, room_url(hotel, "101")).status_code == 200


def test_the_delete_refusal_says_when_deactivation_is_possible(api: TestClient, hotel: str) -> None:
    book(api, hotel, "101", AHEAD, AHEAD + dt.timedelta(days=2))

    response = api.delete(room_url(hotel, "101"))

    assert response.status_code == 409, response.text
    assert (
        "possible once it holds no confirmed or checked-in stay ending after today"
        in (response.json()["error"]["message"])
    )


def test_the_writes_that_sell_a_room_declare_the_inactive_room_conflict() -> None:
    """The four writes that put a room on sale say so in their 409; the others do not."""
    paths = create_app(Settings(environment="test")).openapi()["paths"]
    base = "/api/v1/hotels/{hotel_public_id}/bookings"
    one = f"{base}/{{booking_public_id}}"

    def conflict(path: str, method: str) -> str:
        return str(paths[path][method]["responses"]["409"]["description"])

    for path, method in (
        (base, "post"),
        (one, "patch"),
        (f"{one}/stay", "patch"),
        (f"{one}/stay/extension", "post"),
    ):
        assert "a room or its room type is inactive" in conflict(path, method), (path, method)
    for path, method in ((one, "delete"), (f"{one}/reconciliation", "get")):
        assert "inactive" not in conflict(path, method), (path, method)


# --- the hotel's today, at an instant the test chooses --------------------------------------

#: 22:30 UTC on 9 March 2027 is 00:30 on 10 March in Athens (UTC+2 until 28 March).
INSTANT = dt.datetime(2027, 3, 9, 22, 30, tzinfo=dt.UTC)
ATHENS_TODAY = dt.date(2027, 3, 10)


def clock() -> dt.datetime:
    return INSTANT


def scenario(
    session: Session, check_out: dt.date, status: str = "confirmed"
) -> tuple[Hotel, RoomType, Room]:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="101")
    check_in = check_out - dt.timedelta(days=3)
    booking = make_booking(
        session,
        hotel,
        make_guest(session, hotel),
        check_in=check_in,
        check_out=check_out,
        status=status,
    )
    price_nights(session, allocate_room(session, booking, room), ["100.00"] * 3)
    session.commit()
    return hotel, room_type, room


def retire_room(session: Session, hotel: Hotel, room_type: RoomType) -> bool:
    scope: Any = SimpleNamespace(require_hotel_and_room_type=lambda *_: (hotel, room_type))
    return (
        RoomService(session, RoomRepository(session), scope, clock=clock)
        .update(hotel.public_id, room_type.code, "101", RoomUpdate(is_active=False))
        .is_active
    )


def retire_type(session: Session, hotel: Hotel, room_type: RoomType) -> bool:
    scope: Any = SimpleNamespace(
        require_hotel=lambda _: hotel, require_room_type=lambda *_: room_type
    )
    return (
        RoomTypeService(session, RoomTypeRepository(session), scope, clock=clock)
        .update(hotel.public_id, room_type.code, RoomTypeUpdate(is_active=False))
        .is_active
    )


@pytest.mark.parametrize("retire", [retire_room, retire_type], ids=["room", "room_type"])
def test_a_stay_ending_on_the_hotels_today_has_ended(session: Session, retire: Any) -> None:
    """Checked out on the hotel's today -- 10 March in Athens, still 9 March in UTC."""
    hotel, room_type, _ = scenario(session, ATHENS_TODAY)

    assert retire(session, hotel, room_type) is False


@pytest.mark.parametrize("retire", [retire_room, retire_type], ids=["room", "room_type"])
@pytest.mark.parametrize("status", ["confirmed", "checked_in"])
def test_a_stay_ending_the_day_after_the_hotels_today_blocks(
    session: Session, retire: Any, status: str
) -> None:
    hotel, room_type, _ = scenario(session, ATHENS_TODAY + dt.timedelta(days=1), status)

    with pytest.raises(ConflictError, match=r"ending after today \(2027-03-10\)"):
        retire(session, hotel, room_type)


# --- the locks -------------------------------------------------------------------------------


@pytest.fixture
def other(engine: Engine) -> Iterator[Session]:
    """A second connection, for the transaction the first one must wait for."""
    with sessionmaker(bind=engine, future=True)() as second:
        yield second
        second.rollback()


def waits_for_a_lock(other: Session, attempt: Callable[[], object]) -> None:
    """*attempt*, in a fresh transaction of *other*, waits on a row lock and gives up.

    ``SET LOCAL`` rather than a session-level ``SET``: the timeout must outlive no transaction,
    and a ``SET`` made inside a transaction is undone when that transaction rolls back.
    """
    other.rollback()
    other.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
    with pytest.raises(OperationalError) as refused:
        attempt()
    assert getattr(refused.value.orig, "sqlstate", None) == "55P03"  # lock_not_available
    other.rollback()


def test_a_sale_holds_its_room_and_type_against_a_deactivation(
    session: Session, other: Session
) -> None:
    _, room_type, room = scenario(session, AHEAD)
    room_id, room_type_id = room.id, room_type.id

    BookingRepository(session).lock_rooms_for_sale([room_id])

    waits_for_a_lock(
        other, lambda: RoomRepository(other).lock_for_update(other.get_one(Room, room_id))
    )
    waits_for_a_lock(
        other,
        lambda: RoomTypeRepository(other).lock_for_update(other.get_one(RoomType, room_type_id)),
    )
    session.rollback()


def test_two_sales_of_one_room_do_not_wait_for_each_other(session: Session, other: Session) -> None:
    _, _, room = scenario(session, AHEAD)
    room_id = room.id

    BookingRepository(session).lock_rooms_for_sale([room_id])

    other.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
    assert BookingRepository(other).lock_rooms_for_sale([room_id]) == {room_id: ("101", True, True)}
    other.rollback()
    session.rollback()


def test_a_deactivation_holds_its_room_against_a_sale(session: Session, other: Session) -> None:
    _, room_type, room = scenario(session, AHEAD)
    room_id = room.id

    RoomRepository(session).lock_for_update(room)
    waits_for_a_lock(other, lambda: BookingRepository(other).lock_rooms_for_sale([room_id]))
    session.rollback()

    RoomTypeRepository(session).lock_for_update(room_type)
    waits_for_a_lock(other, lambda: BookingRepository(other).lock_rooms_for_sale([room_id]))
    session.rollback()
