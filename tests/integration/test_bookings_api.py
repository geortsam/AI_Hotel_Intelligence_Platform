"""Booking domain against real PostgreSQL.

Four database mechanisms carry this suite, and none of them can be exercised anywhere else:
the partial GiST exclusion constraint on room occupancy, the DEFERRABLE night-completeness
trigger, the composite foreign keys that keep tenants apart, and the cascade that propagates
a status change from a booking to its allocations. SQLite is not substituted.
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Booking, BookingRoom, BookingRoomNight, Hotel, Room
from tests.integration.conftest import authenticated_client, requires_postgres

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "bookings@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 9, 1)
CHECK_OUT = dt.date(2026, 9, 4)  # three nights


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


def nights(check_in: dt.date = CHECK_IN, count: int = 3, rate: str = "120.00") -> list[dict]:
    return [
        {"stay_date": str(check_in + dt.timedelta(days=offset)), "rate": rate}
        for offset in range(count)
    ]


def booking_payload(guest_public_id: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "guest_public_id": guest_public_id,
        "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
        "check_in_date": str(CHECK_IN),
        "check_out_date": str(CHECK_OUT),
        "status": "confirmed",
        "adults": 2,
        "total_amount": "360.00",
        "currency": "EUR",
        "rooms": [{"room_number": "101", "nights": nights()}],
    }
    body.update(overrides)
    return body


def bookings_url(hotel_public_id: str) -> str:
    return f"/api/v1/hotels/{hotel_public_id}/bookings"


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


def build_hotel(api: TestClient, slug: str, rooms: tuple[str, ...] = ("101", "102")) -> str:
    """A hotel with one room type and the given rooms. Returns its public id."""
    public_id = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    api.post(f"/api/v1/hotels/{public_id}/room-types", json=type_payload("DLX"))
    for number in rooms:
        api.post(f"/api/v1/hotels/{public_id}/room-types/DLX/rooms", json={"room_number": number})
    return public_id


def make_guest(api: TestClient, hotel_public_id: str, last_name: str = "Lovelace") -> str:
    return str(
        api.post(
            f"/api/v1/hotels/{hotel_public_id}/guests",
            json={"first_name": "Ada", "last_name": last_name},
        ).json()["public_id"]
    )


@pytest.fixture
def hotel_id(api: TestClient) -> str:
    return build_hotel(api, "hotel-a")


@pytest.fixture
def guest_id(api: TestClient, hotel_id: str) -> str:
    return make_guest(api, hotel_id)


# --- 1-3. create, retrieve, list -----------------------------------------------------------


def test_create_booking_returns_201(api: TestClient, hotel_id: str, guest_id: str) -> None:
    response = api.post(bookings_url(hotel_id), json=booking_payload(guest_id))
    body = response.json()

    assert response.status_code == 201
    assert body["status"] == "confirmed"
    assert body["guest_public_id"] == guest_id
    assert uuid.UUID(body["public_id"])
    assert len(body["rooms"]) == 1


def test_creation_writes_the_whole_aggregate_in_one_transaction(
    api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))

    assert session.scalar(sa.select(sa.func.count()).select_from(Booking)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 3


def test_the_response_carries_allocations_and_their_nightly_rates(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    body = api.post(
        bookings_url(hotel_id),
        json=booking_payload(
            guest_id,
            rooms=[
                {
                    "room_number": "101",
                    "nights": [
                        {"stay_date": "2026-09-01", "rate": "120.00"},
                        {"stay_date": "2026-09-02", "rate": "140.00"},
                        {"stay_date": "2026-09-03", "rate": "180.00"},
                    ],
                }
            ],
        ),
    ).json()

    room = body["rooms"][0]
    assert room["room_number"] == "101"
    assert room["room_type_code"] == "DLX"
    assert room["nights"] == 3  # generated column
    assert [n["rate"] for n in room["nightly_rates"]] == ["120.00", "140.00", "180.00"]


def test_a_booking_may_allocate_several_rooms(
    api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    body = api.post(
        bookings_url(hotel_id),
        json=booking_payload(
            guest_id,
            rooms=[
                {"room_number": "101", "nights": nights()},
                {"room_number": "102", "nights": nights(rate="100.00")},
            ],
        ),
    ).json()

    assert len(body["rooms"]) == 2
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 6


def test_get_booking(api: TestClient, hotel_id: str, guest_id: str) -> None:
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()

    response = api.get(f"{bookings_url(hotel_id)}/{created['public_id']}")

    assert response.status_code == 200
    assert response.json()["public_id"] == created["public_id"]


def test_list_uses_the_shared_envelope(api: TestClient, hotel_id: str, guest_id: str) -> None:
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))
    body = api.get(bookings_url(hotel_id)).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] == 1


def test_list_is_ordered_by_arrival_date_descending(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    for day, room in [(1, "101"), (10, "102")]:
        check_in = dt.date(2026, 9, day)
        api.post(
            bookings_url(hotel_id),
            json=booking_payload(
                guest_id,
                check_in_date=str(check_in),
                check_out_date=str(check_in + dt.timedelta(days=3)),
                rooms=[{"room_number": room, "nights": nights(check_in)}],
            ),
        )

    arrivals = [item["check_in_date"] for item in api.get(bookings_url(hotel_id)).json()["items"]]

    assert arrivals == ["2026-09-10", "2026-09-01"]


def test_pagination_splits_results(api: TestClient, hotel_id: str, guest_id: str) -> None:
    for index in range(3):
        check_in = dt.date(2026, 10, 1 + index * 5)
        api.post(
            bookings_url(hotel_id),
            json=booking_payload(
                guest_id,
                check_in_date=str(check_in),
                check_out_date=str(check_in + dt.timedelta(days=3)),
                rooms=[{"room_number": "101", "nights": nights(check_in)}],
            ),
        )

    first = api.get(bookings_url(hotel_id), params={"page": 1, "page_size": 2}).json()
    second = api.get(bookings_url(hotel_id), params={"page": 2, "page_size": 2}).json()

    assert [first["total"], first["pages"]] == [3, 2]
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1


# --- 6-11. invalid or cross-hotel parents --------------------------------------------------------


def test_invalid_hotel_returns_404(api: TestClient, guest_id: str) -> None:
    response = api.post(bookings_url(str(uuid.uuid4())), json=booking_payload(guest_id))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


def test_invalid_guest_returns_404(api: TestClient, hotel_id: str) -> None:
    response = api.post(bookings_url(hotel_id), json=booking_payload(str(uuid.uuid4())))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Guest not found for this hotel."


def test_a_guest_from_another_hotel_cannot_be_used(
    api: TestClient, hotel_id: str, session: Session
) -> None:
    other = build_hotel(api, "hotel-b")
    foreign_guest = make_guest(api, other, last_name="Hopper")

    response = api.post(bookings_url(hotel_id), json=booking_payload(foreign_guest))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Guest not found for this hotel."
    assert session.scalar(sa.select(sa.func.count()).select_from(Booking)) == 0


def test_invalid_room_returns_404(api: TestClient, hotel_id: str, guest_id: str) -> None:
    response = api.post(
        bookings_url(hotel_id),
        json=booking_payload(guest_id, rooms=[{"room_number": "999", "nights": nights()}]),
    )

    assert response.status_code == 404
    assert "999" in response.json()["error"]["message"]


def test_a_room_from_another_hotel_cannot_be_used(
    api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    """Hotel B has room 201; hotel A does not. Naming it under hotel A must not reach it."""
    build_hotel(api, "hotel-b", rooms=("201",))

    response = api.post(
        bookings_url(hotel_id),
        json=booking_payload(guest_id, rooms=[{"room_number": "201", "nights": nights()}]),
    )

    assert response.status_code == 404
    assert session.scalar(sa.select(sa.func.count()).select_from(Booking)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 0


def test_a_failed_creation_leaves_nothing_behind(
    api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    """The second room is unknown, so the whole transaction must roll back -- including the
    header and the first room's nights."""
    api.post(
        bookings_url(hotel_id),
        json=booking_payload(
            guest_id,
            rooms=[
                {"room_number": "101", "nights": nights()},
                {"room_number": "999", "nights": nights()},
            ],
        ),
    )

    assert session.scalar(sa.select(sa.func.count()).select_from(Booking)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 0


# --- 12-13. uniqueness and the exclusion constraint ----------------------------------------------


def test_duplicate_reference_at_one_hotel_returns_409(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    payload = booking_payload(guest_id)
    api.post(bookings_url(hotel_id), json=payload)

    second = dict(payload)
    second["rooms"] = [{"room_number": "102", "nights": nights()}]
    response = api.post(bookings_url(hotel_id), json=second)

    assert response.status_code == 409
    assert payload["reference"] in response.json()["error"]["message"]


def test_overlapping_active_bookings_on_one_room_return_409(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """The GiST exclusion constraint -- the single most important rule in the schema."""
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))

    overlapping = dt.date(2026, 9, 3)
    response = api.post(
        bookings_url(hotel_id),
        json=booking_payload(
            guest_id,
            check_in_date=str(overlapping),
            check_out_date=str(overlapping + dt.timedelta(days=3)),
            rooms=[{"room_number": "101", "nights": nights(overlapping)}],
        ),
    )
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "already booked" in body["error"]["message"]


def test_back_to_back_bookings_on_one_room_are_allowed(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """Half-open [): one guest checks out on the 4th, another checks in the same day."""
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))

    response = api.post(
        bookings_url(hotel_id),
        json=booking_payload(
            guest_id,
            check_in_date=str(CHECK_OUT),
            check_out_date=str(CHECK_OUT + dt.timedelta(days=3)),
            rooms=[{"room_number": "101", "nights": nights(CHECK_OUT)}],
        ),
    )

    assert response.status_code == 201


def test_a_refused_overlap_leaves_the_original_booking_intact(
    api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))

    assert session.scalar(sa.select(sa.func.count()).select_from(Booking)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 3


def test_the_overlap_error_leaks_no_constraint_or_sql(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))
    text = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).text.lower()

    for leak in [
        "excl_booking_rooms_room_no_overlap",
        "daterange",
        "insert",
        "psycopg",
        "sqlalchemy",
        "exclusionviolation",
        "23p01",
        "detail:",
    ]:
        assert leak not in text, f"leaked {leak!r}"


# --- 14. status and inventory --------------------------------------------------------------------


def test_pending_bookings_do_not_hold_inventory(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """The exclusion constraint is partial: only confirmed/checked_in block a room."""
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id, status="pending"))

    response = api.post(bookings_url(hotel_id), json=booking_payload(guest_id, status="pending"))

    assert response.status_code == 201


def test_a_cancelled_booking_releases_the_room(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    first = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()
    api.patch(f"{bookings_url(hotel_id)}/{first['public_id']}", json={"status": "cancelled"})

    response = api.post(bookings_url(hotel_id), json=booking_payload(guest_id))

    assert response.status_code == 201


def test_a_checked_out_booking_releases_the_room(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """checked_out is outside the inventory-holding set, per approved decision 8."""
    first = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()
    api.patch(f"{bookings_url(hotel_id)}/{first['public_id']}", json={"status": "checked_out"})

    assert api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).status_code == 201


def test_confirming_a_pending_booking_whose_room_is_taken_returns_409(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """The status change cascades to the allocation and re-triggers the exclusion
    constraint -- the database refuses it, not application code."""
    pending = api.post(
        bookings_url(hotel_id), json=booking_payload(guest_id, status="pending")
    ).json()
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id, status="confirmed"))

    response = api.patch(
        f"{bookings_url(hotel_id)}/{pending['public_id']}", json={"status": "confirmed"}
    )

    assert response.status_code == 409
    assert "already booked" in response.json()["error"]["message"]


def test_cancelling_sets_the_timestamp_and_uncancelling_clears_it(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """ck_bookings_cancellation_consistent is a biconditional; the service keeps both sides
    in step so the constraint is never violated."""
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()
    assert created["cancelled_at"] is None

    cancelled = api.patch(
        f"{bookings_url(hotel_id)}/{created['public_id']}",
        json={"status": "cancelled", "cancellation_reason": "Guest request"},
    ).json()
    assert cancelled["cancelled_at"] is not None

    restored = api.patch(
        f"{bookings_url(hotel_id)}/{created['public_id']}", json={"status": "pending"}
    ).json()
    assert restored["cancelled_at"] is None


def test_the_status_cascade_reaches_the_allocations(
    api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    """ON UPDATE CASCADE on the composite FK keeps the mirror honest."""
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()

    api.patch(f"{bookings_url(hotel_id)}/{created['public_id']}", json={"status": "checked_in"})

    session.expire_all()
    assert session.scalars(sa.select(BookingRoom)).one().booking_status == "checked_in"


@pytest.mark.parametrize("status", ["booked", "active", ""])
def test_invented_statuses_are_rejected(
    api: TestClient, hotel_id: str, guest_id: str, status: str
) -> None:
    assert (
        api.post(bookings_url(hotel_id), json=booking_payload(guest_id, status=status)).status_code
        == 422
    )


# --- 16-19. the deferred night-completeness trigger ----------------------------------------------


def test_an_incomplete_night_set_is_rejected_at_the_edge(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """The schema mirrors the trigger so the client gets a field-level message."""
    response = api.post(
        bookings_url(hotel_id),
        json=booking_payload(guest_id, rooms=[{"room_number": "101", "nights": nights(count=2)}]),
    )

    assert response.status_code == 422
    assert "must price exactly the nights" in response.text


def test_the_deferred_trigger_rejects_an_incomplete_night_set_at_commit(
    engine: Engine, api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    """Drives the trigger directly, bypassing the API's edge validation.

    This is the point of the deferral: the allocation legitimately has zero nights part-way
    through the transaction, and only the COMMIT judges it.
    """
    hotel = session.scalars(sa.select(Hotel)).one()
    room = session.scalars(sa.select(Room)).first()
    assert room is not None

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as work:
        booking = Booking(
            hotel_id=hotel.id,
            guest_id=session.execute(sa.text("select id from guests limit 1")).scalar_one(),
            reference="BK-TRIGGER",
            check_in_date=CHECK_IN,
            check_out_date=CHECK_OUT,
            status="confirmed",
            total_amount=Decimal("360.00"),
            currency="EUR",
        )
        work.add(booking)
        work.flush()

        allocation = BookingRoom(
            booking_id=booking.id,
            room_id=room.id,
            hotel_id=hotel.id,
            check_in_date=CHECK_IN,
            check_out_date=CHECK_OUT,
            booking_status="confirmed",
        )
        work.add(allocation)
        work.flush()  # zero nights so far -- legal, because the trigger is deferred

        work.add(
            BookingRoomNight(
                booking_room_id=allocation.id,
                hotel_id=hotel.id,
                check_in_date=CHECK_IN,
                check_out_date=CHECK_OUT,
                stay_date=CHECK_IN,
                rate=Decimal("120.00"),
            )
        )
        work.flush()  # one night against a three-night stay -- still legal mid-transaction

        with pytest.raises(IntegrityError, match="night row"):
            work.commit()
        work.rollback()

    # 19. the rollback is real: nothing survived.
    session.expire_all()
    assert (
        session.scalar(
            sa.select(sa.func.count()).select_from(Booking).where(Booking.reference == "BK-TRIGGER")
        )
        == 0
    )


def test_a_complete_night_set_commits(
    engine: Engine, api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    """The same construction, completed, is accepted -- confirming the trigger judges the
    final state rather than each statement."""
    hotel = session.scalars(sa.select(Hotel)).one()
    room = session.scalars(sa.select(Room)).first()
    assert room is not None
    guest_pk = session.execute(sa.text("select id from guests limit 1")).scalar_one()

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as work:
        booking = Booking(
            hotel_id=hotel.id,
            guest_id=guest_pk,
            reference="BK-COMPLETE",
            check_in_date=CHECK_IN,
            check_out_date=CHECK_OUT,
            status="confirmed",
            total_amount=Decimal("360.00"),
            currency="EUR",
        )
        work.add(booking)
        work.flush()
        allocation = BookingRoom(
            booking_id=booking.id,
            room_id=room.id,
            hotel_id=hotel.id,
            check_in_date=CHECK_IN,
            check_out_date=CHECK_OUT,
            booking_status="confirmed",
        )
        work.add(allocation)
        work.flush()
        work.add_all(
            [
                BookingRoomNight(
                    booking_room_id=allocation.id,
                    hotel_id=hotel.id,
                    check_in_date=CHECK_IN,
                    check_out_date=CHECK_OUT,
                    stay_date=CHECK_IN + dt.timedelta(days=offset),
                    rate=Decimal("120.00"),
                )
                for offset in range(3)
            ]
        )
        work.commit()

    session.expire_all()
    assert (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(Booking)
            .where(Booking.reference == "BK-COMPLETE")
        )
        == 1
    )


# --- 20. cross-hotel access ----------------------------------------------------------------------


def test_cross_hotel_access_returns_404(api: TestClient, hotel_id: str, guest_id: str) -> None:
    other = build_hotel(api, "hotel-b")
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()
    pid = created["public_id"]

    assert api.get(f"{bookings_url(hotel_id)}/{pid}").status_code == 200
    assert api.get(f"{bookings_url(other)}/{pid}").status_code == 404
    assert api.patch(f"{bookings_url(other)}/{pid}", json={"adults": 3}).status_code == 404
    assert api.delete(f"{bookings_url(other)}/{pid}").status_code == 404


def test_a_failed_cross_hotel_mutation_changes_nothing(
    api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    other = build_hotel(api, "hotel-b")
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()

    api.patch(f"{bookings_url(other)}/{created['public_id']}", json={"status": "cancelled"})

    session.expire_all()
    assert session.scalars(sa.select(Booking)).one().status == "confirmed"


def test_listing_never_contains_another_hotels_bookings(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    other = build_hotel(api, "hotel-b")
    other_guest = make_guest(api, other, last_name="Hopper")
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))
    api.post(bookings_url(other), json=booking_payload(other_guest))

    assert api.get(bookings_url(hotel_id)).json()["total"] == 1
    assert api.get(bookings_url(other)).json()["total"] == 1


def test_the_same_room_number_at_two_hotels_does_not_collide(api: TestClient) -> None:
    """Room 101 exists at both properties; the exclusion constraint is per physical room."""
    first = build_hotel(api, "hotel-a")
    second = build_hotel(api, "hotel-b")

    assert (
        api.post(bookings_url(first), json=booking_payload(make_guest(api, first))).status_code
        == 201
    )
    assert (
        api.post(bookings_url(second), json=booking_payload(make_guest(api, second))).status_code
        == 201
    )


# --- 4-5. update and delete ----------------------------------------------------------------------


def test_partial_update_preserves_unspecified_fields(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    created = api.post(
        bookings_url(hotel_id), json=booking_payload(guest_id, special_requests="Late arrival")
    ).json()

    updated = api.patch(
        f"{bookings_url(hotel_id)}/{created['public_id']}", json={"adults": 3}
    ).json()

    assert updated["adults"] == 3
    assert updated["special_requests"] == "Late arrival"
    assert updated["reference"] == created["reference"]
    assert len(updated["rooms"]) == 1


def test_empty_update_is_a_no_op(api: TestClient, hotel_id: str, guest_id: str) -> None:
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()

    response = api.patch(f"{bookings_url(hotel_id)}/{created['public_id']}", json={})

    assert response.status_code == 200
    assert response.json()["reference"] == created["reference"]


@pytest.mark.parametrize(
    "field", ["check_in_date", "check_out_date", "reference", "guest_public_id"]
)
def test_immutable_fields_are_rejected_by_patch(
    api: TestClient, hotel_id: str, guest_id: str, field: str
) -> None:
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()

    response = api.patch(
        f"{bookings_url(hotel_id)}/{created['public_id']}", json={field: "2026-10-01"}
    )

    assert response.status_code == 422


def test_delete_removes_the_booking_and_cascades_its_allocations(
    api: TestClient, hotel_id: str, guest_id: str, session: Session
) -> None:
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()

    response = api.delete(f"{bookings_url(hotel_id)}/{created['public_id']}")

    assert response.status_code == 204
    assert session.scalar(sa.select(sa.func.count()).select_from(Booking)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 0


def test_deleting_a_booking_frees_the_room(api: TestClient, hotel_id: str, guest_id: str) -> None:
    created = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()
    api.delete(f"{bookings_url(hotel_id)}/{created['public_id']}")

    assert api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).status_code == 201


def test_missing_booking_returns_404(api: TestClient, hotel_id: str) -> None:
    response = api.get(f"{bookings_url(hotel_id)}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Booking not found for this hotel."


def test_deleting_a_room_with_a_booking_is_refused(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """The Room-domain RESTRICT from Stage 3B.3 still holds."""
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))

    response = api.delete(f"/api/v1/hotels/{hotel_id}/room-types/DLX/rooms/101")

    assert response.status_code == 409


def test_deleting_a_guest_with_a_booking_is_refused(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    """The Guest-domain RESTRICT from Stage 3B.5 still holds."""
    api.post(bookings_url(hotel_id), json=booking_payload(guest_id))

    assert api.delete(f"/api/v1/hotels/{hotel_id}/guests/{guest_id}").status_code == 409


# --- 22-24. response shape and error hygiene -----------------------------------------------------


def test_response_exposes_no_internal_ids(api: TestClient, hotel_id: str, guest_id: str) -> None:
    body = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()

    for forbidden in ["id", "hotel_id", "guest_id"]:
        assert forbidden not in body
    for forbidden in ["id", "room_id", "booking_id", "hotel_id"]:
        assert forbidden not in body["rooms"][0]


def test_url_rebuilt_from_the_response_resolves(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    body = api.post(bookings_url(hotel_id), json=booking_payload(guest_id)).json()

    rebuilt = f"/api/v1/hotels/{body['hotel_public_id']}/bookings/{body['public_id']}"
    assert api.get(rebuilt).status_code == 200


def test_money_survives_the_round_trip_as_exact_decimal(
    api: TestClient, hotel_id: str, guest_id: str
) -> None:
    body = api.post(
        bookings_url(hotel_id),
        json=booking_payload(
            guest_id,
            total_amount="360.10",
            rooms=[{"room_number": "101", "nights": nights(rate="120.03")}],
        ),
    ).json()

    assert Decimal(body["total_amount"]) == Decimal("360.10")
    assert Decimal(body["rooms"][0]["nightly_rates"][0]["rate"]) == Decimal("120.03")


def test_errors_use_the_shared_envelope(api: TestClient, hotel_id: str) -> None:
    body = api.get(f"{bookings_url(hotel_id)}/{uuid.uuid4()}").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_malformed_identifier_returns_422(api: TestClient, hotel_id: str) -> None:
    assert api.get(f"{bookings_url(hotel_id)}/not-a-uuid").status_code == 422
