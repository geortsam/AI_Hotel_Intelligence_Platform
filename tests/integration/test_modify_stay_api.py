"""Modify-stay against real PostgreSQL.

Stage 4.5.11. The highest-risk mutation in the platform: it rewrites a booking's dates, its
allocations and its priced nights, and every one of those is guarded by something only the
database enforces -- the GiST exclusion constraint, the deferred night-completeness trigger,
and a CHECK that every night falls inside its own stay.

**The case this suite exists for is self-conflict.** A booking in room 101 for
``[Sep 10, Sep 14)`` moving to ``[Sep 11, Sep 15)`` in the same room overlaps *itself*, and an
exclusion constraint cannot tell that the old row is about to be deleted. Get that wrong and
the most ordinary modification in a hotel -- push the departure back a day -- is impossible.
Get it wrong the other way and a room is sold twice.

**Financial records are not touched.** Payments are append-only and modify-stay writes none;
several tests assert the payment rows are byte-identical before and after, including their
public ids, amounts, statuses and transaction references. What DOES change is the accommodation
total, because that is derived from the night rows the modification replaces -- and Stage
4.5.9's reconciliation is asserted to agree with the new rows every time.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Booking, BookingRoom, BookingRoomNight, Payment
from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    requires_postgres,
)

SUITE_EMAIL = "modify-stay@example.test"
OTHER_EMAIL = "modify-stay-other@example.test"

pytestmark = requires_postgres


def sep(day: int) -> dt.date:
    return dt.date(2027, 10, day)


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


def bookings_url(hotel: str) -> str:
    return f"/api/v1/hotels/{hotel}/bookings"


def stay_url(hotel: str, booking: str) -> str:
    return f"{bookings_url(hotel)}/{booking}/stay"


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build_hotel(
    api: TestClient, slug: str = "stay-hotel", rooms: tuple[str, ...] = ("101", "102", "103")
) -> str:
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    api.post(
        f"/api/v1/hotels/{hotel}/room-types",
        json={
            "code": "DLX",
            "name": "Deluxe",
            "max_occupancy": 4,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": "120.00",
            "currency": "EUR",
        },
    )
    for number in rooms:
        api.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": number})
    return hotel


def nights_for(check_in: dt.date, check_out: dt.date) -> list[dict]:
    """Nights for either payload. Neither carries an amount any more.

    Stage 4.5.23 moved creation to server-side pricing; Stage 4.5.24 moved modification
    with it, once the financial consequence of doing so could be stated. The two shapes
    are identical again, which is the point -- a stay is priced the same way whether it
    is being made or moved.
    """
    return [
        {"stay_date": str(check_in + dt.timedelta(days=n))}
        for n in range((check_out - check_in).days)
    ]


unpriced_nights_for = nights_for


def set_base_price(api: TestClient, hotel: str, price: str, code: str = "DLX") -> None:
    """Configure what a night costs. Since Stage 4.5.24 this is how a MODIFICATION is
    priced too, so a test that wants a repricing moves this and then modifies.
    """
    response = api.patch(f"/api/v1/hotels/{hotel}/room-types/{code}", json={"base_price": price})
    assert response.status_code == 200, response.text


def make_booking(
    api: TestClient,
    hotel: str,
    *,
    rooms: tuple[str, ...] = ("101",),
    check_in: dt.date | None = None,
    check_out: dt.date | None = None,
    status: str = "confirmed",
    rates: list[str] | None = None,
) -> str:
    check_in = check_in or sep(10)
    check_out = check_out or sep(14)
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    room_payloads = []
    for number in rooms:
        # ``rates`` is accepted and ignored for creation: since Stage 4.5.23 the server
        # decides. Kept in the signature because the modification helper below still
        # honours it, and the tests that exercise repricing pass it to both.
        room_payloads.append(
            {"room_number": number, "nights": unpriced_nights_for(check_in, check_out)}
        )
    response = api.post(
        bookings_url(hotel),
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(check_in),
            "check_out_date": str(check_out),
            "status": status,
            "total_amount": "400.00",
            "currency": "EUR",
            "rooms": room_payloads,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def modify(
    api: TestClient,
    hotel: str,
    booking: str,
    check_in: dt.date,
    check_out: dt.date,
    *,
    rooms: tuple[str, ...] = ("101",),
    rates: list[str] | None = None,
) -> Response:
    # ``rates`` is accepted and ignored: since Stage 4.5.24 the server prices a
    # modification exactly as it prices a creation. Tests that want a particular rate
    # call set_base_price first.
    room_payloads = [
        {"room_number": number, "nights": nights_for(check_in, check_out)} for number in rooms
    ]
    return api.patch(
        stay_url(hotel, booking),
        json={
            "check_in_date": str(check_in),
            "check_out_date": str(check_out),
            "rooms": room_payloads,
        },
    )


def accommodation_total(api: TestClient, hotel: str, booking: str) -> Decimal:
    """Through the Stage 4.5.9 endpoint, so the two features are asserted to agree."""
    body = api.get(f"{bookings_url(hotel)}/{booking}/reconciliation").json()
    return Decimal(body["accommodation_total"])


@pytest.fixture
def hotel(api: TestClient) -> str:
    return build_hotel(api)


# ======================================================================================
# The three shapes of a stay change
# ======================================================================================


def test_extending_a_stay(api: TestClient, hotel: str, session: Session) -> None:
    """3 nights to 5. Night rows, the computed count and the total all follow."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(13))

    response = modify(api, hotel, booking, sep(10), sep(15))

    assert response.status_code == 200
    assert response.json()["booking"]["check_in_date"] == str(sep(10))
    assert response.json()["booking"]["check_out_date"] == str(sep(15))
    session.expire_all()
    allocation = session.scalars(sa.select(BookingRoom)).one()
    assert allocation.nights == 5
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 5
    # Five nights at the configured 120.00: since Stage 4.5.24 a modification is priced
    # by the server too, so this is the rate card, not a number the payload chose.
    assert accommodation_total(api, hotel, booking) == Decimal("600.00")


def test_shortening_a_stay(api: TestClient, hotel: str, session: Session) -> None:
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(15))
    # Five nights at the room type base price of 120.00: the CREATED booking is priced
    # by the server since Stage 4.5.23. The modification below still prices at 100.00,
    # because modify-stay deliberately still takes client rates.
    assert accommodation_total(api, hotel, booking) == Decimal("600.00")

    response = modify(api, hotel, booking, sep(10), sep(13))

    assert response.status_code == 200
    session.expire_all()
    assert session.scalars(sa.select(BookingRoom)).one().nights == 3
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 3
    assert accommodation_total(api, hotel, booking) == Decimal("360.00")


def test_shifting_a_stay_leaves_no_old_nights_behind(
    api: TestClient, hotel: str, session: Session
) -> None:
    """The orphan-row case: every stay_date must be inside the NEW window."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))

    assert modify(api, hotel, booking, sep(12), sep(16)).status_code == 200

    session.expire_all()
    stay_dates = sorted(session.scalars(sa.select(BookingRoomNight.stay_date)).all())
    assert stay_dates == [sep(12), sep(13), sep(14), sep(15)]


def test_the_night_rows_always_match_the_computed_count(
    api: TestClient, hotel: str, session: Session
) -> None:
    """The invariant the deferred trigger enforces, asserted directly after each shape."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))

    for check_in, check_out in [(sep(10), sep(18)), (sep(11), sep(13)), (sep(20), sep(24))]:
        assert modify(api, hotel, booking, check_in, check_out).status_code == 200
        session.expire_all()
        allocation = session.scalars(sa.select(BookingRoom)).one()
        rows = session.scalar(
            sa.select(sa.func.count())
            .select_from(BookingRoomNight)
            .where(BookingRoomNight.booking_room_id == allocation.id)
        )
        assert allocation.nights == (check_out - check_in).days
        assert rows == allocation.nights


# ======================================================================================
# Self-conflict: the case the feature lives or dies on
# ======================================================================================


def test_a_booking_can_move_within_its_own_room(api: TestClient, hotel: str) -> None:
    """Overlaps itself. Must succeed -- this is the most ordinary modification there is."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))

    response = modify(api, hotel, booking, sep(11), sep(15))

    assert response.status_code == 200


def test_a_booking_can_be_extended_by_one_night_in_place(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))

    assert modify(api, hotel, booking, sep(10), sep(15)).status_code == 200


def test_restating_the_identical_stay_succeeds(
    api: TestClient, hotel: str, session: Session
) -> None:
    """Maximal self-overlap: the same room, the same dates."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))

    response = modify(api, hotel, booking, sep(10), sep(14))

    assert response.status_code == 200
    session.expire_all()
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 4


def test_the_same_modification_twice_is_stable(
    api: TestClient, hotel: str, session: Session
) -> None:
    """Retry safety without an idempotency subsystem: the second call converges on the same
    state rather than duplicating allocations or nights."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))

    first = modify(api, hotel, booking, sep(12), sep(16))
    second = modify(api, hotel, booking, sep(12), sep(16))

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    session.expire_all()
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 4


# ======================================================================================
# Another booking still wins where it should
# ======================================================================================


def test_moving_onto_an_occupied_room_is_refused(api: TestClient, hotel: str) -> None:
    make_booking(api, hotel, rooms=("102",), check_in=sep(12), check_out=sep(16))
    booking = make_booking(api, hotel, rooms=("101",), check_in=sep(10), check_out=sep(14))

    response = modify(api, hotel, booking, sep(12), sep(16), rooms=("102",))

    assert response.status_code == 409
    assert "already booked" in response.json()["error"]["message"]


def test_extending_into_another_bookings_dates_is_refused(api: TestClient, hotel: str) -> None:
    make_booking(api, hotel, rooms=("101",), check_in=sep(16), check_out=sep(20))
    booking = make_booking(api, hotel, rooms=("101",), check_in=sep(10), check_out=sep(14))

    response = modify(api, hotel, booking, sep(10), sep(18))

    assert response.status_code == 409


def test_moving_to_abut_another_booking_succeeds(api: TestClient, hotel: str) -> None:
    """Half-open: ending exactly where another begins is not an overlap."""
    make_booking(api, hotel, rooms=("101",), check_in=sep(14), check_out=sep(18))
    booking = make_booking(api, hotel, rooms=("102",), check_in=sep(1), check_out=sep(5))

    response = modify(api, hotel, booking, sep(10), sep(14), rooms=("101",))

    assert response.status_code == 200


def test_moving_to_begin_where_another_ends_succeeds(api: TestClient, hotel: str) -> None:
    make_booking(api, hotel, rooms=("101",), check_in=sep(6), check_out=sep(10))
    booking = make_booking(api, hotel, rooms=("102",), check_in=sep(1), check_out=sep(3))

    assert modify(api, hotel, booking, sep(10), sep(14), rooms=("101",)).status_code == 200


def test_a_pending_booking_does_not_block_a_move(api: TestClient, hotel: str) -> None:
    """Pending holds no inventory, per INVENTORY_HOLDING_STATUSES."""
    make_booking(api, hotel, rooms=("102",), check_in=sep(10), check_out=sep(14), status="pending")
    booking = make_booking(api, hotel, rooms=("101",), check_in=sep(10), check_out=sep(14))

    assert modify(api, hotel, booking, sep(10), sep(14), rooms=("102",)).status_code == 200


# ======================================================================================
# Atomicity: a refused modification changes nothing
# ======================================================================================


def test_a_refused_modification_leaves_the_original_stay_intact(
    api: TestClient, hotel: str, session: Session
) -> None:
    """The rollback case. No new dates with old nights, no missing allocation."""
    make_booking(api, hotel, rooms=("102",), check_in=sep(12), check_out=sep(16))
    booking = make_booking(api, hotel, rooms=("101",), check_in=sep(10), check_out=sep(14))
    before = api.get(f"{bookings_url(hotel)}/{booking}").json()

    assert modify(api, hotel, booking, sep(12), sep(16), rooms=("102",)).status_code == 409

    session.expire_all()
    assert api.get(f"{bookings_url(hotel)}/{booking}").json() == before
    row = session.scalars(sa.select(Booking).where(Booking.public_id == uuid.UUID(booking))).one()
    assert row.check_in_date == sep(10)
    assert row.check_out_date == sep(14)


def test_a_refused_modification_leaves_the_night_rows_intact(
    api: TestClient, hotel: str, session: Session
) -> None:
    make_booking(api, hotel, rooms=("102",), check_in=sep(12), check_out=sep(16))
    booking = make_booking(api, hotel, rooms=("101",), check_in=sep(10), check_out=sep(14))

    modify(api, hotel, booking, sep(12), sep(16), rooms=("102",))

    session.expire_all()
    dates = sorted(
        session.scalars(
            sa.select(BookingRoomNight.stay_date).where(BookingRoomNight.hotel_id.isnot(None))
        ).all()
    )
    # Four nights for the blocker, four for the untouched booking. None orphaned or lost.
    assert len(dates) == 8
    assert sep(10) in dates and sep(13) in dates


def test_an_unknown_room_changes_nothing(api: TestClient, hotel: str, session: Session) -> None:
    """Rooms are resolved before anything is written, so this is a 404 and not a rollback."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))

    response = modify(api, hotel, booking, sep(12), sep(16), rooms=("999",))

    assert response.status_code == 404
    session.expire_all()
    assert session.scalars(sa.select(Booking)).one().check_in_date == sep(10)


# ======================================================================================
# Status policy
# ======================================================================================


@pytest.mark.parametrize("status", ["pending", "confirmed"])
def test_a_modifiable_status_can_move(api: TestClient, hotel: str, status: str) -> None:
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14), status=status)

    assert modify(api, hotel, booking, sep(11), sep(15)).status_code == 200


@pytest.mark.parametrize("status", ["checked_in", "checked_out", "cancelled", "no_show"])
def test_a_non_modifiable_status_is_refused(
    api: TestClient, hotel: str, status: str, session: Session
) -> None:
    """checked_in is refused deliberately -- see MODIFIABLE_BOOKING_STATUSES. The three
    terminal statuses are refused because reopening them would undo the state machine."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14), status=status)

    response = modify(api, hotel, booking, sep(11), sep(15))

    assert response.status_code == 409
    assert "cannot have its stay changed" in response.json()["error"]["message"]
    session.expire_all()
    assert session.scalars(sa.select(Booking)).one().check_in_date == sep(10)


def test_the_status_is_never_changed_by_a_modification(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14), status="pending")

    modify(api, hotel, booking, sep(11), sep(15))

    session.expire_all()
    assert session.scalars(sa.select(Booking)).one().status == "pending"
    assert session.scalars(sa.select(BookingRoom)).one().booking_status == "pending"


# ======================================================================================
# Multi-room bookings move together
# ======================================================================================


def test_every_allocation_moves_together(api: TestClient, hotel: str, session: Session) -> None:
    booking = make_booking(api, hotel, rooms=("101", "102"), check_in=sep(10), check_out=sep(14))

    assert modify(api, hotel, booking, sep(12), sep(16), rooms=("101", "102")).status_code == 200

    session.expire_all()
    allocations = session.scalars(sa.select(BookingRoom)).all()
    assert len(allocations) == 2
    assert {a.check_in_date for a in allocations} == {sep(12)}
    assert {a.check_out_date for a in allocations} == {sep(16)}


def test_one_blocked_room_rolls_the_whole_move_back(
    api: TestClient, hotel: str, session: Session
) -> None:
    """No half-moved booking: 101 must not land on the new dates while 103 is refused."""
    make_booking(api, hotel, rooms=("103",), check_in=sep(12), check_out=sep(16))
    booking = make_booking(api, hotel, rooms=("101", "102"), check_in=sep(10), check_out=sep(14))

    response = modify(api, hotel, booking, sep(12), sep(16), rooms=("101", "103"))

    assert response.status_code == 409
    session.expire_all()
    moved = session.scalars(sa.select(Booking).where(Booking.public_id == uuid.UUID(booking))).one()
    assert moved.check_in_date == sep(10)
    assert {a.room_id for a in moved.booking_rooms} != set()


def test_the_allocation_set_itself_can_change(
    api: TestClient, hotel: str, session: Session
) -> None:
    """Dropping a room is part of restating the stay."""
    booking = make_booking(api, hotel, rooms=("101", "102"), check_in=sep(10), check_out=sep(14))

    assert modify(api, hotel, booking, sep(10), sep(14), rooms=("103",)).status_code == 200

    session.expire_all()
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 4


# ======================================================================================
# Rates and financial integrity
# ======================================================================================


def test_the_calculated_rates_are_what_is_stored(api: TestClient, hotel: str) -> None:
    """Since Stage 4.5.24 the stored rate is the CONFIGURED one, not a supplied one.

    The test kept its subject and lost its premise: what a modification stores is still
    the thing worth pinning, but nobody sends it any more.
    """
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(13))
    set_base_price(api, hotel, "120.00")

    modify(api, hotel, booking, sep(10), sep(13))

    assert accommodation_total(api, hotel, booking) == Decimal("360.00")


def test_reconciliation_agrees_with_the_new_night_rows(
    api: TestClient, hotel: str, session: Session
) -> None:
    """The 4.5.9 authority is recomputed from the rows this stage rewrote -- no stale total."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))
    set_base_price(api, hotel, "50.00")
    modify(api, hotel, booking, sep(10), sep(16))

    session.expire_all()
    summed = session.scalar(sa.select(sa.func.coalesce(sa.func.sum(BookingRoomNight.rate), 0)))
    assert summed is not None  # COALESCE(..., 0) cannot return NULL

    assert accommodation_total(api, hotel, booking) == Decimal("300.00")
    assert Decimal(summed) == Decimal("300.00")


def test_payments_are_untouched_by_a_modification(
    api: TestClient, hotel: str, session: Session
) -> None:
    """Append-only. Every field of every payment row must survive byte-identical."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))
    api.post(
        f"{bookings_url(hotel)}/{booking}/payments",
        json={
            "amount": "400.00",
            "currency": "EUR",
            "method": "card",
            "provider": "stripe",
            "transaction_reference": "pi_modify_stay",
        },
    )

    def snapshot() -> list[tuple]:
        session.expire_all()
        return [
            (p.public_id, p.kind, p.amount, p.currency, p.status, p.transaction_reference)
            for p in session.scalars(sa.select(Payment).order_by(Payment.id)).all()
        ]

    before = snapshot()
    assert modify(api, hotel, booking, sep(12), sep(18)).status_code == 200

    assert snapshot() == before


def test_a_modification_writes_no_payment_row(
    api: TestClient, hotel: str, session: Session
) -> None:
    """Repricing a stay does not charge or refund anything. That workflow does not exist."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))
    api.post(
        f"{bookings_url(hotel)}/{booking}/payments",
        json={"amount": "400.00", "currency": "EUR", "method": "card"},
    )

    modify(api, hotel, booking, sep(10), sep(20))

    session.expire_all()
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1


def test_the_declared_total_is_not_silently_rewritten(api: TestClient, hotel: str) -> None:
    """`total_amount` is contractual metadata and modify-stay does not accept or change it,
    so a stay change makes the divergence visible rather than papering over it."""
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))

    modify(api, hotel, booking, sep(10), sep(12))

    body = api.get(f"{bookings_url(hotel)}/{booking}/reconciliation").json()
    assert Decimal(body["declared_total"]) == Decimal("400.00")
    assert Decimal(body["accommodation_total"]) == Decimal("240.00")
    assert body["totals_agree"] is False


# ======================================================================================
# Identity is immutable
# ======================================================================================


def test_the_booking_keeps_its_identity(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel, check_in=sep(10), check_out=sep(14))
    before = api.get(f"{bookings_url(hotel)}/{booking}").json()

    after = modify(api, hotel, booking, sep(12), sep(16)).json()["booking"]

    for field in (
        "public_id",
        "hotel_public_id",
        "guest_public_id",
        "reference",
        "currency",
        "total_amount",
        "status",
        "source",
        "booked_at",
        "created_at",
    ):
        assert after[field] == before[field], field
    # Only the stay itself moved.
    assert after["check_in_date"] != before["check_in_date"]


# ======================================================================================
# Validation
# ======================================================================================


@pytest.mark.parametrize(("check_in", "check_out"), [(sep(14), sep(14)), (sep(14), sep(10))])
def test_a_non_positive_stay_is_refused(
    api: TestClient, hotel: str, check_in: dt.date, check_out: dt.date
) -> None:
    booking = make_booking(api, hotel)

    assert modify(api, hotel, booking, check_in, check_out).status_code == 422


def test_nights_must_cover_exactly_the_new_stay(api: TestClient, hotel: str) -> None:
    """The edge mirror of the deferred completeness trigger."""
    booking = make_booking(api, hotel)

    response = api.patch(
        stay_url(hotel, booking),
        json={
            "check_in_date": str(sep(10)),
            "check_out_date": str(sep(14)),
            "rooms": [{"room_number": "101", "nights": nights_for(sep(10), sep(13))}],
        },
    )

    assert response.status_code == 422


def test_a_duplicated_room_is_refused(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    response = modify(api, hotel, booking, sep(10), sep(14), rooms=("101", "101"))

    assert response.status_code == 422


def test_the_payload_refuses_unknown_fields(api: TestClient, hotel: str) -> None:
    """`extra="forbid"`: a client cannot smuggle a status or a total through this route."""
    booking = make_booking(api, hotel)

    response = api.patch(
        stay_url(hotel, booking),
        json={
            "check_in_date": str(sep(10)),
            "check_out_date": str(sep(14)),
            "rooms": [{"room_number": "101", "nights": nights_for(sep(10), sep(14))}],
            "status": "cancelled",
            "total_amount": "0.00",
        },
    )

    assert response.status_code == 422


# ======================================================================================
# Security
# ======================================================================================


def test_a_booking_at_another_hotel_is_not_found(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    other = build_hotel(api, "stay-hotel-b", rooms=("101",))

    cross = modify(api, other, booking, sep(12), sep(16))
    invented = modify(api, other, str(uuid.uuid4()), sep(12), sep(16))

    assert cross.status_code == 404
    assert cross.json() == invented.json()


def test_a_non_member_cannot_modify(api: TestClient, engine: Engine, hotel: str) -> None:
    booking = make_booking(api, hotel)
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    response = stranger.patch(
        stay_url(hotel, booking),
        json={
            "check_in_date": str(sep(12)),
            "check_out_date": str(sep(16)),
            "rooms": [{"room_number": "101", "nights": nights_for(sep(12), sep(16))}],
        },
    )

    assert response.status_code == 404


def test_a_viewer_cannot_modify(
    api: TestClient, engine: Engine, hotel: str, session: Session
) -> None:
    """STAFF is required, exactly as for creating or updating a booking."""
    booking = make_booking(api, hotel)
    viewer = authenticated_client(engine, email=OTHER_EMAIL)
    grant_membership(engine, OTHER_EMAIL, hotel, "viewer")

    response = viewer.patch(
        stay_url(hotel, booking),
        json={
            "check_in_date": str(sep(12)),
            "check_out_date": str(sep(16)),
            "rooms": [{"room_number": "101", "nights": nights_for(sep(12), sep(16))}],
        },
    )

    assert response.status_code == 403
    session.expire_all()
    assert session.scalars(sa.select(Booking)).one().check_in_date == sep(10)


def test_an_unauthenticated_caller_cannot_modify(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    anonymous = TestClient(api.app)

    response = anonymous.patch(
        stay_url(hotel, booking),
        json={
            "check_in_date": str(sep(12)),
            "check_out_date": str(sep(16)),
            "rooms": [{"room_number": "101", "nights": nights_for(sep(12), sep(16))}],
        },
    )

    assert response.status_code == 401


def test_a_conflict_leaks_no_internals(api: TestClient, hotel: str) -> None:
    make_booking(api, hotel, rooms=("102",), check_in=sep(12), check_out=sep(16))
    booking = make_booking(api, hotel, rooms=("101",), check_in=sep(10), check_out=sep(14))

    response = modify(api, hotel, booking, sep(12), sep(16), rooms=("102",))

    assert response.status_code == 409
    for leak in [
        "SELECT",
        "INSERT",
        "DELETE",
        "daterange",
        "booking_rooms",
        "excl_booking_rooms",
        "23P01",
        "room_id",
        "hotel_id",
        "sqlalchemy",
        "psycopg",
        "Traceback",
    ]:
        assert leak not in response.text, f"leaked {leak!r}"


def test_the_response_carries_no_internal_identifier(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    body = modify(api, hotel, booking, sep(12), sep(16)).json()

    assert "id" not in body
    assert not {k for k in body if k.endswith("_id") and not k.endswith("public_id")}


# ======================================================================================
# Concurrency -- real connections, real contention
# ======================================================================================


def concurrent(engine: Engine, calls: list[tuple[str, dict]]) -> list[int]:
    """Fire PATCHes from separate clients, released together by a barrier."""
    both_ready = threading.Barrier(len(calls), timeout=30)
    codes: dict[int, int] = {}

    def attempt(index: int, url: str, body: dict) -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        both_ready.wait()
        codes[index] = client.patch(url, json=body).status_code

    threads = [
        threading.Thread(target=attempt, args=(i, url, body)) for i, (url, body) in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)
    return [codes[i] for i in range(len(calls))]


def stay_body(check_in: dt.date, check_out: dt.date, room: str) -> dict:
    return {
        "check_in_date": str(check_in),
        "check_out_date": str(check_out),
        "rooms": [{"room_number": room, "nights": nights_for(check_in, check_out)}],
    }


def test_two_bookings_cannot_move_onto_the_same_room_and_dates(
    api: TestClient, engine: Engine, hotel: str, session: Session
) -> None:
    """Scenario A. Both move to room 103 for the same window; the constraint picks one."""
    first = make_booking(api, hotel, rooms=("101",), check_in=sep(1), check_out=sep(3))
    second = make_booking(api, hotel, rooms=("102",), check_in=sep(1), check_out=sep(3))

    codes = concurrent(
        engine,
        [
            (stay_url(hotel, first), stay_body(sep(20), sep(24), "103")),
            (stay_url(hotel, second), stay_body(sep(20), sep(24), "103")),
        ],
    )

    # Exactly one move succeeds; the other is refused. HOW it is refused is the database's
    # choice: usually the loser blocks and then fails the exclusion constraint (409), and
    # sometimes PostgreSQL detects a deadlock and aborts it instead, which the application
    # maps to 503. CI run 35097766182 took the second path. Asserting one of those two is
    # asserting a scheduling outcome; the safety property is that only one booking wins.
    assert codes.count(200) == 1, codes
    assert sum(code in {409, 503} for code in codes) == 1, codes
    session.expire_all()
    holders = session.scalars(
        sa.select(BookingRoom).where(BookingRoom.check_in_date == sep(20))
    ).all()
    assert len(holders) == 1


def test_a_modification_racing_a_new_booking(
    api: TestClient, engine: Engine, hotel: str, session: Session
) -> None:
    """Scenario B. One wins; the loser gets the ordinary booking conflict."""
    booking = make_booking(api, hotel, rooms=("101",), check_in=sep(1), check_out=sep(3))
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Race", "last_name": "Caller"}
    ).json()["public_id"]

    both_ready = threading.Barrier(2, timeout=30)
    codes: dict[str, int] = {}

    def move() -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        both_ready.wait()
        codes["move"] = client.patch(
            stay_url(hotel, booking), json=stay_body(sep(20), sep(24), "102")
        ).status_code

    def create() -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        both_ready.wait()
        codes["create"] = client.post(
            bookings_url(hotel),
            json={
                "guest_public_id": guest,
                "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
                "check_in_date": str(sep(20)),
                "check_out_date": str(sep(24)),
                "status": "confirmed",
                "total_amount": "400.00",
                "currency": "EUR",
                "rooms": [{"room_number": "102", "nights": unpriced_nights_for(sep(20), sep(24))}],
            },
        ).status_code

    threads = [threading.Thread(target=move), threading.Thread(target=create)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    assert sorted([codes["move"], codes["create"]]) in ([200, 409], [201, 409]), codes
    session.expire_all()
    holders = session.scalars(
        sa.select(BookingRoom).where(BookingRoom.check_in_date == sep(20))
    ).all()
    assert len(holders) == 1


def test_two_modifications_of_one_booking_produce_one_coherent_state(
    api: TestClient, engine: Engine, hotel: str, session: Session
) -> None:
    """Scenario C. The booking's row lock serialises them; the result is never a hybrid."""
    booking = make_booking(api, hotel, rooms=("101",), check_in=sep(10), check_out=sep(14))

    codes = concurrent(
        engine,
        [
            (stay_url(hotel, booking), stay_body(sep(20), sep(24), "101")),
            (stay_url(hotel, booking), stay_body(sep(2), sep(6), "101")),
        ],
    )

    assert codes == [200, 200], codes
    session.expire_all()
    row = session.scalars(sa.select(Booking)).one()
    allocations = session.scalars(sa.select(BookingRoom)).all()
    nights = sorted(session.scalars(sa.select(BookingRoomNight.stay_date)).all())

    assert len(allocations) == 1
    assert (row.check_in_date, row.check_out_date) in (
        (sep(20), sep(24)),
        (sep(2), sep(6)),
    )
    assert allocations[0].check_in_date == row.check_in_date
    assert nights[0] == row.check_in_date
    assert len(nights) == 4


# ======================================================================================
# Cost
# ======================================================================================


def test_the_modification_costs_a_bounded_number_of_queries(
    api: TestClient, engine: Engine, hotel: str
) -> None:
    """No statement per night. Three rooms over six nights is eighteen night rows, inserted
    as three batches, not eighteen statements."""
    booking = make_booking(
        api, hotel, rooms=("101", "102", "103"), check_in=sep(10), check_out=sep(14)
    )

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        response = modify(api, hotel, booking, sep(20), sep(26), rooms=("101", "102", "103"))
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    inserts = [s for s in statements if s.lstrip().upper().startswith("INSERT")]
    # One INSERT per allocation and one executemany per allocation's nights: 6, not 21.
    # Stage 4.5.12 added one: the INSERT that records the modification. Auditing a mutation
    # costs exactly one statement, because `AuditRepository.add` takes `public_id` and
    # `occurred_at` from the INSERT's RETURNING clause instead of refreshing the row.
    assert len(inserts) <= 9, f"{len(inserts)} INSERTs: {inserts}"
    # Stage 4.5.24 adds exactly four, and the number is the point: the old value, the
    # ledger currencies, the room-type rates and the ledger totals. One each, whatever the
    # stay looks like -- none of them grows with rooms or nights.
    assert len(statements) <= 35, f"{len(statements)} statements"
