"""The 20 required PostgreSQL scenarios.

Every test here needs a real PostgreSQL server; see ``conftest.py`` for why SQLite is not an
acceptable stand-in. They skip -- and are reported as PENDING, never as passing -- when
``TEST_DATABASE_URL`` is unset.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import sqlstate_of
from app.models import (
    Booking,
    BookingRoom,
    BookingRoomNight,
    DailyHotelMetric,
    Hotel,
    Payment,
    Revenue,
    RevenueCategory,
    Review,
    Room,
    User,
    UserHotel,
)
from tests.integration.conftest import (
    allocate_room,
    make_booking,
    make_guest,
    make_hotel,
    make_room,
    make_room_type,
    price_nights,
    requires_postgres,
)

pytestmark = requires_postgres

SEP1 = dt.date(2026, 9, 1)
SEP5 = dt.date(2026, 9, 5)
RATES = ["120.00", "140.00", "180.00", "220.00"]


# --- 1-4: the basic entities ----------------------------------------------------------


def test_01_create_hotel(session: Session) -> None:
    hotel = make_hotel(session, slug="acropolis-view")
    session.commit()

    assert hotel.id > 0
    assert hotel.public_id is not None  # server-side gen_random_uuid()
    assert hotel.is_active is True
    assert hotel.created_at is not None


def test_02_create_room_type(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    session.commit()

    assert room_type.hotel_id == hotel.id
    assert room_type.base_price == Decimal("120.00")


def test_03_create_room(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    session.commit()

    assert room.status == "available"
    assert room.room_type_id == room_type.id


def test_04_create_guest(session: Session) -> None:
    hotel = make_hotel(session)
    guest = make_guest(session, hotel, email="ada@example.test")
    session.commit()

    assert guest.hotel_id == hotel.id
    assert guest.marketing_opt_in is False  # consent defaults to no


# --- 5-7: bookings, multi-room bookings, night rows -----------------------------------


def test_05_create_booking(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)

    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    booking_room = allocate_room(session, booking, room)
    price_nights(session, booking_room, RATES)
    session.commit()

    assert booking.status == "confirmed"
    assert booking_room.nights == 4  # generated column


def test_06_create_booking_with_multiple_rooms(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room_a = make_room(session, hotel, room_type, number="205")
    room_b = make_room(session, hotel, room_type, number="206")
    guest = make_guest(session, hotel)

    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    for room in (room_a, room_b):
        booking_room = allocate_room(session, booking, room)
        price_nights(session, booking_room, RATES)
    session.commit()

    session.refresh(booking)
    assert len(booking.booking_rooms) == 2
    # 1 booking + 2 allocations + 8 night rows
    total_nights = session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight))
    assert total_nights == 8


def test_07_create_booking_room_nights_with_varying_rates(session: Session) -> None:
    """The revision-2 scenario: 120 / 140 / 180 / 220 across four nights."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)
    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    booking_room = allocate_room(session, booking, room)
    price_nights(session, booking_room, RATES)
    session.commit()

    rows = session.scalars(
        sa.select(BookingRoomNight)
        .where(BookingRoomNight.booking_room_id == booking_room.id)
        .order_by(BookingRoomNight.stay_date)
    ).all()

    assert [r.stay_date.day for r in rows] == [1, 2, 3, 4]
    assert [str(r.rate) for r in rows] == ["120.00", "140.00", "180.00", "220.00"]
    # Check-out day is NOT a night -- the half-open interval, end to end.
    assert SEP5 not in [r.stay_date for r in rows]
    # Room revenue is SUM(rate), never an apportioned stay total.
    assert sum(r.rate for r in rows) == Decimal("660.00")


# --- 8-10: night-set integrity --------------------------------------------------------


def test_08_night_uniqueness_is_enforced(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)
    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    booking_room = allocate_room(session, booking, room)
    price_nights(session, booking_room, RATES)
    session.commit()

    session.add(
        BookingRoomNight(
            booking_room_id=booking_room.id,
            hotel_id=hotel.id,
            check_in_date=SEP1,
            check_out_date=SEP5,
            stay_date=SEP1,  # already priced
            rate=Decimal("999.00"),
        )
    )
    with pytest.raises(IntegrityError, match="uq_booking_room_nights_room_stay_date"):
        session.commit()


def test_09_night_outside_the_stay_interval_is_rejected(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)
    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    booking_room = allocate_room(session, booking, room)

    session.add(
        BookingRoomNight(
            booking_room_id=booking_room.id,
            hotel_id=hotel.id,
            check_in_date=SEP1,
            check_out_date=SEP5,
            stay_date=SEP5,  # the check-out day is outside [check_in, check_out)
            rate=Decimal("120.00"),
        )
    )
    with pytest.raises(IntegrityError, match="stay_date_within_stay"):
        session.commit()


def test_10_incomplete_night_set_is_rejected_at_commit(session: Session) -> None:
    """The deferred constraint trigger: a 4-night stay may not commit with 3 night rows."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)
    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    booking_room = allocate_room(session, booking, room)
    price_nights(session, booking_room, RATES, skip={2})  # only 3 of 4 nights

    with pytest.raises(IntegrityError, match="night row"):
        session.commit()


def test_10b_deferred_trigger_permits_a_legitimate_build_order(session: Session) -> None:
    """A booking_room legitimately has zero nights for part of the transaction.

    This is the reason the trigger is DEFERRABLE INITIALLY DEFERRED rather than immediate:
    an immediate trigger would make correct code impossible to write.
    """
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)
    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)

    booking_room = allocate_room(session, booking, room)
    session.flush()  # zero night rows exist at this instant -- and that is fine
    assert (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(BookingRoomNight)
            .where(BookingRoomNight.booking_room_id == booking_room.id)
        )
        == 0
    )

    price_nights(session, booking_room, RATES)
    session.commit()  # only the final state is judged


def test_10c_deleting_one_night_is_rejected_at_commit(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)
    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    booking_room = allocate_room(session, booking, room)
    nights = price_nights(session, booking_room, RATES)
    session.commit()

    session.delete(nights[0])
    with pytest.raises(IntegrityError, match="night row"):
        session.commit()


# --- 11-13: the overlap guarantee -----------------------------------------------------


def test_11_overlapping_active_bookings_on_one_room_are_rejected(session: Session) -> None:
    """The exclusion constraint. This is the most important test in the suite."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)

    first = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    first_room = allocate_room(session, first, room)
    price_nights(session, first_room, RATES)
    session.commit()

    # Sep 3-7 overlaps Sep 1-5 on the same physical room.
    second = make_booking(
        session, hotel, guest, check_in=dt.date(2026, 9, 3), check_out=dt.date(2026, 9, 7)
    )

    # The rejection happens at INSERT, not at COMMIT. The exclusion constraint is NOT
    # deferrable -- unlike the night-completeness trigger in test_10, which is INITIALLY
    # DEFERRED and therefore only fires at commit. Immediate rejection is the stronger
    # behaviour: the conflicting row never reaches the table, even momentarily.
    with pytest.raises(IntegrityError, match="excl_booking_rooms_room_no_overlap") as excinfo:
        allocate_room(session, second, room)

    assert "ExclusionViolation" in str(excinfo.value)
    session.rollback()

    # The original booking is untouched and still holds the room.
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 1


def test_12_back_to_back_bookings_are_allowed(session: Session) -> None:
    """Half-open '[)': one guest checks out on the 5th, another checks in the same day."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)

    first = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    price_nights(session, allocate_room(session, first, room), RATES)
    session.commit()

    second = make_booking(session, hotel, guest, check_in=SEP5, check_out=dt.date(2026, 9, 8))
    price_nights(session, allocate_room(session, second, room), ["150.00", "150.00", "150.00"])
    session.commit()  # must NOT raise

    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 2


def test_13_cancelled_booking_releases_inventory(session: Session) -> None:
    """Cancelling cascades the status down to booking_rooms, dropping the row out of the
    partial exclusion index and freeing the room -- in the same transaction."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)

    first = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    first_room = allocate_room(session, first, room)
    price_nights(session, first_room, RATES)
    session.commit()

    first.status = "cancelled"
    first.cancelled_at = dt.datetime.now(dt.UTC)
    session.commit()

    # ON UPDATE CASCADE propagated the status to the allocation.
    session.refresh(first_room)
    assert first_room.booking_status == "cancelled"

    # The room is now free for the very same dates.
    replacement = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    price_nights(session, allocate_room(session, replacement, room), RATES)
    session.commit()  # must NOT raise


def test_13b_pending_bookings_do_not_hold_inventory(session: Session) -> None:
    """Approved decision 6: an abandoned checkout must not block a room."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)

    pending = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5, status="pending")
    price_nights(session, allocate_room(session, pending, room), RATES)
    session.commit()

    confirmed = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    price_nights(session, allocate_room(session, confirmed, room), RATES)
    session.commit()  # must NOT raise -- pending held nothing


# --- 14-17: payments, reviews, revenue ------------------------------------------------


def test_14_multiple_payments_for_one_booking(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)
    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    price_nights(session, allocate_room(session, booking, room), RATES)

    deposit = Payment(
        booking_id=booking.id,
        hotel_id=hotel.id,
        amount=Decimal("200.00"),
        currency="EUR",
        method="card",
        status="captured",
        paid_at=dt.datetime.now(dt.UTC),
    )
    balance = Payment(
        booking_id=booking.id,
        hotel_id=hotel.id,
        amount=Decimal("280.00"),
        currency="EUR",
        method="cash",
        status="captured",
        paid_at=dt.datetime.now(dt.UTC),
    )
    session.add_all([deposit, balance])
    session.commit()

    refund = Payment(
        booking_id=booking.id,
        hotel_id=hotel.id,
        kind="refund",
        amount=Decimal("50.00"),  # positive; direction lives in `kind`
        currency="EUR",
        method="card",
        status="captured",
        paid_at=dt.datetime.now(dt.UTC),
        refunded_payment_id=deposit.id,
    )
    session.add(refund)
    session.commit()

    net = session.scalar(
        sa.select(
            sa.func.sum(sa.case((Payment.kind == "charge", Payment.amount), else_=-Payment.amount))
        ).where(Payment.booking_id == booking.id)
    )
    assert net == Decimal("430.00")


def test_15_external_review_without_booking_is_allowed(session: Session) -> None:
    hotel = make_hotel(session)
    guest = make_guest(session, hotel)
    session.add(
        Review(
            hotel_id=hotel.id,
            guest_id=guest.id,
            booking_id=None,  # cannot be matched to a stay
            source="tripadvisor",
            external_review_id="TA-99001",
            rating=Decimal("4.00"),
            rating_scale=5,
            body="Lovely stay.",
            review_date=dt.date(2026, 9, 10),
        )
    )
    session.commit()

    review = session.scalars(sa.select(Review)).one()
    assert review.booking_id is None
    assert review.rating_normalized == Decimal("0.8000")  # generated column


def test_16_external_review_without_guest_is_allowed(session: Session) -> None:
    hotel = make_hotel(session)
    session.add(
        Review(
            hotel_id=hotel.id,
            guest_id=None,
            booking_id=None,
            source="booking_com",
            external_review_id="BC-4711",
            reviewer_name="Anonymous traveller",
            rating=Decimal("8.00"),
            rating_scale=10,  # a different scale entirely
            review_date=dt.date(2026, 9, 11),
        )
    )
    session.commit()

    review = session.scalars(sa.select(Review)).one()
    assert review.guest_id is None
    # 8/10 and 4/5 normalize to the same comparable figure.
    assert review.rating_normalized == Decimal("0.8000")


def test_16b_reimporting_the_same_external_review_is_idempotent(session: Session) -> None:
    hotel = make_hotel(session)
    for _ in range(1):
        session.add(
            Review(
                hotel_id=hotel.id,
                source="google",
                external_review_id="G-1",
                rating=Decimal("5.00"),
                review_date=dt.date(2026, 9, 12),
            )
        )
    session.commit()

    session.add(
        Review(
            hotel_id=hotel.id,
            source="google",
            external_review_id="G-1",  # same review, fetched again
            rating=Decimal("5.00"),
            review_date=dt.date(2026, 9, 12),
        )
    )
    with pytest.raises(IntegrityError, match="uq_reviews_source_external_review_id"):
        session.commit()


def test_17_non_room_revenue_without_booking_is_allowed(session: Session) -> None:
    """A non-resident eating in the restaurant generates revenue attached to no booking."""
    hotel = make_hotel(session)
    category = RevenueCategory(code="food_beverage", name="Food & Beverage")
    session.add(category)
    session.flush()

    session.add(
        Revenue(
            hotel_id=hotel.id,
            category_id=category.id,
            booking_id=None,
            revenue_date=dt.date(2026, 9, 2),
            amount=Decimal("64.50"),
            tax_amount=Decimal("15.48"),
            currency="EUR",
            description="Walk-in dinner",
        )
    )
    session.commit()

    entry = session.scalars(sa.select(Revenue)).one()
    assert entry.booking_id is None
    assert entry.amount == Decimal("64.50")


# --- 18-20: metrics, delete policies, tenant isolation --------------------------------


def test_18_daily_metric_uniqueness(session: Session) -> None:
    hotel = make_hotel(session)

    def metric() -> DailyHotelMetric:
        return DailyHotelMetric(
            hotel_id=hotel.id,
            metric_date=dt.date(2026, 9, 1),
            available_rooms=100,
            occupied_rooms=75,
            room_revenue=Decimal("11250.00"),
            other_revenue=Decimal("2000.00"),
            currency="EUR",
        )

    session.add(metric())
    session.commit()

    stored = session.scalars(sa.select(DailyHotelMetric)).one()
    assert stored.occupancy_rate == Decimal("0.7500")
    assert stored.adr == Decimal("150.00")
    assert stored.revpar == Decimal("112.50")
    assert stored.total_revenue == Decimal("13250.00")

    session.add(metric())
    with pytest.raises(IntegrityError, match="uq_daily_hotel_metrics_hotel_id_metric_date"):
        session.commit()


def test_18b_adr_is_null_not_zero_when_no_rooms_are_occupied(session: Session) -> None:
    """ADR is UNDEFINED at zero occupancy. Recording 0 would drag every average down and
    poison model training."""
    hotel = make_hotel(session)
    session.add(
        DailyHotelMetric(
            hotel_id=hotel.id,
            metric_date=dt.date(2026, 12, 25),
            available_rooms=100,
            occupied_rooms=0,
            room_revenue=Decimal("0.00"),
            currency="EUR",
        )
    )
    session.commit()

    stored = session.scalars(sa.select(DailyHotelMetric)).one()
    assert stored.adr is None
    assert stored.occupancy_rate == Decimal("0.0000")


def test_19_historical_records_are_protected_by_delete_policies(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)
    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    price_nights(session, allocate_room(session, booking, room), RATES)
    session.commit()

    # A room with stay history cannot be deleted -- RESTRICT, never CASCADE.
    with pytest.raises(IntegrityError):
        session.execute(sa.delete(Room).where(Room.id == room.id))
        session.commit()
    session.rollback()

    # Nor can a hotel with bookings.
    with pytest.raises(IntegrityError):
        session.execute(sa.delete(Hotel).where(Hotel.id == hotel.id))
        session.commit()
    session.rollback()

    # But deleting a booking DOES cascade to its allocations and their nights: those are
    # meaningless without it.
    session.execute(sa.delete(Booking).where(Booking.id == booking.id))
    session.commit()
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoom)) == 0
    assert session.scalar(sa.select(sa.func.count()).select_from(BookingRoomNight)) == 0


def test_20_multi_hotel_data_remains_isolated(session: Session) -> None:
    """Cross-tenant references must be structurally impossible, not merely unlikely."""
    hotel_a = make_hotel(session, slug="hotel-a")
    hotel_b = make_hotel(session, slug="hotel-b")
    type_a = make_room_type(session, hotel_a, code="DBL")
    type_b = make_room_type(session, hotel_b, code="DBL")  # same code, different hotel
    room_b = make_room(session, hotel_b, type_b, number="101")
    guest_a = make_guest(session, hotel_a)
    session.commit()

    # A room may not take another hotel's room type.
    session.add(Room(hotel_id=hotel_b.id, room_type_id=type_a.id, room_number="999"))
    with pytest.raises(IntegrityError, match="fk_rooms_room_type_id_hotel_id_room_types"):
        session.commit()
    session.rollback()

    # A booking may not take another hotel's guest.
    session.add(
        Booking(
            hotel_id=hotel_b.id,
            guest_id=guest_a.id,
            reference="BK-CROSS",
            check_in_date=SEP1,
            check_out_date=SEP5,
            status="confirmed",
            total_amount=Decimal("100.00"),
            currency="EUR",
        )
    )
    with pytest.raises(IntegrityError, match="fk_bookings_guest_id_hotel_id_guests"):
        session.commit()
    session.rollback()

    # A booking at hotel A may not allocate a room at hotel B.
    booking_a = make_booking(session, hotel_a, guest_a, check_in=SEP1, check_out=SEP5)
    session.add(
        BookingRoom(
            booking_id=booking_a.id,
            room_id=room_b.id,
            hotel_id=hotel_a.id,
            check_in_date=SEP1,
            check_out_date=SEP5,
            booking_status="confirmed",
        )
    )
    with pytest.raises(IntegrityError, match="fk_booking_rooms_room_id_hotel_id_rooms"):
        session.commit()
    session.rollback()

    # The same room number is fine at two different properties.
    make_room(session, hotel_a, type_a, number="101")
    session.commit()
    assert session.scalar(sa.select(sa.func.count()).select_from(Room)) == 2


# --- room revenue derivation (approved decision 21) -----------------------------------


def test_21_room_revenue_and_adr_derive_from_night_rows(session: Session) -> None:
    """Room revenue for a date is SUM(rate) over night rows -- no apportionment, and no
    contribution from the revenue ledger."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room_a = make_room(session, hotel, room_type, number="205")
    room_b = make_room(session, hotel, room_type, number="206")
    guest = make_guest(session, hotel)

    booking = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    price_nights(session, allocate_room(session, booking, room_a), RATES)
    price_nights(
        session,
        allocate_room(session, booking, room_b),
        ["100.00", "100.00", "100.00", "100.00"],
    )
    session.commit()

    for stay_date, expected_revenue, expected_adr in [
        (dt.date(2026, 9, 1), Decimal("220.00"), Decimal("110.00")),
        (dt.date(2026, 9, 3), Decimal("280.00"), Decimal("140.00")),
    ]:
        revenue, occupied = session.execute(
            sa.select(sa.func.sum(BookingRoomNight.rate), sa.func.count())
            .where(BookingRoomNight.hotel_id == hotel.id)
            .where(BookingRoomNight.stay_date == stay_date)
        ).one()
        assert revenue == expected_revenue
        assert revenue / occupied == expected_adr


def test_22_historical_overlap_detection_view_reports_conflicts(session: Session) -> None:
    """Approved decision 20: once checked out a stay stops blocking the room, so an
    overlapping historical stay becomes representable. It is DETECTED, not prevented."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="205")
    guest = make_guest(session, hotel)

    first = make_booking(session, hotel, guest, check_in=SEP1, check_out=SEP5)
    price_nights(session, allocate_room(session, first, room), RATES)
    session.commit()

    first.status = "checked_out"
    session.commit()  # releases inventory

    # The database now permits this overlapping historical record.
    second = make_booking(
        session, hotel, guest, check_in=dt.date(2026, 9, 3), check_out=dt.date(2026, 9, 7)
    )
    second_room = allocate_room(session, second, room)
    price_nights(session, second_room, ["100.00"] * 4)
    second.status = "checked_out"
    session.commit()

    rows = session.execute(sa.text("SELECT * FROM historical_room_overlaps")).mappings().all()
    assert len(rows) == 1
    assert rows[0]["room_id"] == room.id
    assert rows[0]["overlap_start"] == dt.date(2026, 9, 3)
    assert rows[0]["overlap_end"] == SEP5


# --- 23: membership, as the database enforces it (migration 0004) ---------------------
#
# Every rule below is the DATABASE's, not the application's. They are asserted here because a
# constraint that exists only in Python is a constraint that a migration, a fixture script or a
# psql session can walk straight through -- and access control is the last place to accept that.


def make_user(session: Session, email: str) -> User:
    user = User(
        email=email,
        password_hash="not-a-real-hash-this-row-never-authenticates",
        full_name="Schema Test",
    )
    session.add(user)
    session.flush()
    return user


def test_23a_one_membership_per_user_and_hotel(session: Session) -> None:
    """uq_user_hotels_user_id_hotel_id. Two rows would make "what is their role here?" a
    question with two answers, and nothing would say which one wins."""
    hotel = make_hotel(session, slug="membership-unique")
    user = make_user(session, "unique@example.test")
    session.add(UserHotel(user_id=user.id, hotel_id=hotel.id, role="viewer"))
    session.flush()

    session.add(UserHotel(user_id=user.id, hotel_id=hotel.id, role="owner"))

    with pytest.raises(IntegrityError) as raised:
        session.flush()
    assert sqlstate_of(raised.value) == "23505"


def test_23b_the_role_check_admits_exactly_four_values(session: Session) -> None:
    hotel = make_hotel(session, slug="membership-roles")

    for index, role in enumerate(["viewer", "staff", "manager", "owner"]):
        user = make_user(session, f"role-{role}@example.test")
        session.add(UserHotel(user_id=user.id, hotel_id=hotel.id, role=role))
        session.flush()
        assert index >= 0  # every one of the four was accepted


@pytest.mark.parametrize("role", ["admin", "Owner", "OWNER", "", "superuser", "guest"])
def test_23c_any_other_role_is_refused(session: Session, role: str) -> None:
    """ck_user_hotels_role_valid. Case matters: `Owner` is not `owner`, and a comparison
    somewhere else would quietly treat it as a role nobody has."""
    hotel = make_hotel(session, slug=f"membership-bad-{abs(hash(role)) % 10000}")
    user = make_user(session, f"bad-{abs(hash(role)) % 10000}@example.test")

    session.add(UserHotel(user_id=user.id, hotel_id=hotel.id, role=role))

    with pytest.raises(IntegrityError) as raised:
        session.flush()
    assert sqlstate_of(raised.value) == "23514"


def test_23d_deleting_a_user_removes_their_access(session: Session) -> None:
    """ON DELETE CASCADE. An account that is gone must not leave a grant behind that a
    recreated user id could inherit."""
    hotel = make_hotel(session, slug="membership-cascade")
    user = make_user(session, "cascade@example.test")
    session.add(UserHotel(user_id=user.id, hotel_id=hotel.id, role="owner"))
    session.commit()

    session.delete(user)
    session.commit()

    assert session.scalar(sa.select(sa.func.count()).select_from(UserHotel)) == 0
    assert session.get(Hotel, hotel.id) is not None, "the hotel must survive its member"


def test_23e_a_hotel_cannot_be_dropped_out_from_under_its_members(session: Session) -> None:
    """ON DELETE RESTRICT, and the reason HotelService clears memberships first.

    The database refuses; the service is what decides that access metadata may be cleared while
    operating history may not. Asserting the refusal here is what makes that a decision rather
    than an accident.
    """
    hotel = make_hotel(session, slug="membership-restrict")
    user = make_user(session, "restrict@example.test")
    session.add(UserHotel(user_id=user.id, hotel_id=hotel.id, role="owner"))
    session.commit()

    # RESTRICT is immediate, not DEFERRABLE: the statement itself raises, unlike the deferred
    # completeness triggers elsewhere in this file that only fire at COMMIT.
    with pytest.raises(IntegrityError) as raised:
        session.execute(sa.text("DELETE FROM hotels WHERE id = :id"), {"id": hotel.id})

    assert sqlstate_of(raised.value) == "23001"
    session.rollback()
    assert session.get(Hotel, hotel.id) is not None


def test_23f_the_updated_at_trigger_fires_on_a_role_change(session: Session) -> None:
    """The shared set_updated_at function, reused rather than copied."""
    hotel = make_hotel(session, slug="membership-trigger")
    user = make_user(session, "trigger@example.test")
    membership = UserHotel(user_id=user.id, hotel_id=hotel.id, role="viewer")
    session.add(membership)
    session.commit()
    before = membership.updated_at

    session.execute(
        sa.text("UPDATE user_hotels SET role = 'manager' WHERE id = :id"), {"id": membership.id}
    )
    session.commit()
    session.refresh(membership)

    assert membership.role == "manager"
    assert membership.updated_at > before


def test_23g_membership_carries_no_public_identifier(session: Session) -> None:
    """A membership is never addressed over HTTP, so it was given no public id: an identifier
    that exists is an identifier something will eventually expose."""
    columns = set(UserHotel.__table__.columns.keys())

    assert columns == {"id", "user_id", "hotel_id", "role", "created_at", "updated_at"}
