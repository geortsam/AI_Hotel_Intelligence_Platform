"""The demand pipeline against a real PostgreSQL, where the leakage claims can be attacked.

The pure rules are covered in ``tests/backend/test_ml_dataset.py``. What needs a database is
everything that depends on SQL actually behaving: the moving per-row cutoff, ``booked_at`` and
``cancelled_at`` reconstruction, capacity from ``created_at``, and tenant isolation across two
hotels that exist at the same time in the same tables.

**All data here is test fixture data**, created inside the disposable database this suite is
given and rolled back with it. Nothing touches a demo or shared database -- the Stage 5.17 guard
refuses that before a single statement runs.

The two tests worth reading first:

* :func:`test_a_booking_created_after_the_cutoff_is_invisible_to_that_date`
* :func:`test_one_hotels_bookings_never_reach_another_hotels_dataset`
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.ml.dataset import InsufficientDataError
from app.models.booking import Booking
from app.models.guest import Guest
from app.models.hotel import Hotel
from app.models.room import Room
from app.repositories.ml_demand import MlDemandRepository
from app.services.ml_dataset import MlDatasetService
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

# Applied explicitly rather than inherited: a marker defined in a conftest does not propagate
# to test modules, and without it the fixtures raise instead of skipping honestly.
pytestmark = requires_postgres

#: A fixed anchor so every expectation below is arithmetic rather than "whatever today is".
ANCHOR = dt.date(2026, 5, 1)


def utc(day: dt.date, hour: int = 12) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour), tzinfo=dt.UTC)


def build_stay(
    session: Session,
    hotel: Hotel,
    room: Room,
    guest: Guest,
    *,
    check_in: dt.date,
    nights: int,
    booked_at: dt.datetime,
    status: str = "checked_out",
    cancelled_at: dt.datetime | None = None,
) -> Booking:
    """One booking, one room, one night row per night -- with `booked_at` under our control.

    The shared `make_booking` helper stamps `booked_at` with `now()`, which is exactly the fact
    these tests need to vary, so it is set explicitly afterwards.
    """
    check_out = check_in + dt.timedelta(days=nights)
    booking = make_booking(
        session, hotel, guest, check_in=check_in, check_out=check_out, status=status
    )
    booking.booked_at = booked_at
    if cancelled_at is not None:
        booking.cancelled_at = cancelled_at
    session.flush()
    booking_room = allocate_room(session, booking, room)
    price_nights(session, booking_room, ["100.00"] * nights)
    return booking


@pytest.fixture
def pipeline(session: Session) -> MlDatasetService:
    return MlDatasetService(MlDemandRepository(session))


# --- the target, against real rows --------------------------------------------------------------


def test_the_target_counts_occupied_room_nights_for_that_date(
    session: Session, pipeline: MlDatasetService
) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    room_a = make_room(session, hotel, room_type, number="101")
    room_b = make_room(session, hotel, room_type, number="102")

    booked = utc(ANCHOR - dt.timedelta(days=30))
    build_stay(session, hotel, room_a, guest, check_in=ANCHOR, nights=3, booked_at=booked)
    build_stay(session, hotel, room_b, guest, check_in=ANCHOR, nights=1, booked_at=booked)

    demand = MlDemandRepository(session).demand_by_date(
        hotel.id, ANCHOR, ANCHOR + dt.timedelta(days=5)
    )
    assert demand[ANCHOR] == 2, "two rooms occupied on the first night"
    assert demand[ANCHOR + dt.timedelta(days=1)] == 1
    assert demand[ANCHOR + dt.timedelta(days=2)] == 1
    assert ANCHOR + dt.timedelta(days=3) not in demand, "check-out day is not a night"


def test_a_cancelled_booking_is_not_realised_demand(
    session: Session, pipeline: MlDatasetService
) -> None:
    """`cancelled` is outside OCCUPANCY_STATUSES, which the target filter imports rather than
    restates."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    room = make_room(session, hotel, room_type, number="201")

    build_stay(
        session,
        hotel,
        room,
        guest,
        check_in=ANCHOR,
        nights=2,
        booked_at=utc(ANCHOR - dt.timedelta(days=20)),
        status="cancelled",
        cancelled_at=utc(ANCHOR - dt.timedelta(days=5)),
    )
    demand = MlDemandRepository(session).demand_by_date(
        hotel.id, ANCHOR, ANCHOR + dt.timedelta(days=3)
    )
    assert demand == {}


# --- THE leakage tests ----------------------------------------------------------------------------


def test_a_booking_created_after_the_cutoff_is_invisible_to_that_date(
    session: Session,
) -> None:
    """Two bookings for the same night: one booked well before the cutoff, one booked after it.

    On-the-books at the cutoff must see exactly one. If it sees two, the feature is reading the
    future and every model trained on it would be scored against information it could not have
    had.
    """
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    early_room = make_room(session, hotel, room_type, number="301")
    late_room = make_room(session, hotel, room_type, number="302")

    stay_date = ANCHOR
    # horizon 1 -> cutoff instant is midnight UTC starting `stay_date`.
    build_stay(
        session,
        hotel,
        early_room,
        guest,
        check_in=stay_date,
        nights=1,
        booked_at=utc(stay_date - dt.timedelta(days=10)),
    )
    build_stay(
        session,
        hotel,
        late_room,
        guest,
        check_in=stay_date,
        nights=1,
        booked_at=utc(stay_date, hour=6),
    )  # same day, AFTER the cutoff

    repository = MlDemandRepository(session)
    assert repository.demand_by_date(hotel.id, stay_date, stay_date)[stay_date] == 2, (
        "both stays are realised demand"
    )
    on_books = repository.on_books_room_nights_by_date(hotel.id, stay_date, stay_date, 1)
    assert on_books.get(stay_date) == 1, (
        "the booking created after the cutoff leaked into the on-the-books feature"
    )


def test_a_booking_cancelled_after_the_cutoff_still_counts_at_the_cutoff(
    session: Session,
) -> None:
    """At the cutoff it was live. Removing it retrospectively would be leakage in the other
    direction -- using knowledge of a future cancellation."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    room = make_room(session, hotel, room_type, number="401")

    stay_date = ANCHOR
    build_stay(
        session,
        hotel,
        room,
        guest,
        check_in=stay_date,
        nights=1,
        booked_at=utc(stay_date - dt.timedelta(days=20)),
        status="cancelled",
        cancelled_at=utc(stay_date, hour=9),  # cancelled AFTER the cutoff
    )
    on_books = MlDemandRepository(session).on_books_room_nights_by_date(
        hotel.id, stay_date, stay_date, 1
    )
    assert on_books.get(stay_date) == 1


def test_a_booking_cancelled_before_the_cutoff_does_not_count(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    room = make_room(session, hotel, room_type, number="501")

    stay_date = ANCHOR
    build_stay(
        session,
        hotel,
        room,
        guest,
        check_in=stay_date,
        nights=1,
        booked_at=utc(stay_date - dt.timedelta(days=20)),
        status="cancelled",
        cancelled_at=utc(stay_date - dt.timedelta(days=3)),
    )
    on_books = MlDemandRepository(session).on_books_room_nights_by_date(
        hotel.id, stay_date, stay_date, 1
    )
    assert on_books.get(stay_date) is None


def test_a_longer_horizon_moves_the_cutoff_and_sees_less(session: Session) -> None:
    """The same data, asked at two horizons. The earlier cutoff must see strictly less."""
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    room_a = make_room(session, hotel, room_type, number="601")
    room_b = make_room(session, hotel, room_type, number="602")

    stay_date = ANCHOR
    build_stay(
        session,
        hotel,
        room_a,
        guest,
        check_in=stay_date,
        nights=1,
        booked_at=utc(stay_date - dt.timedelta(days=30)),
    )
    build_stay(
        session,
        hotel,
        room_b,
        guest,
        check_in=stay_date,
        nights=1,
        booked_at=utc(stay_date - dt.timedelta(days=3)),
    )

    repository = MlDemandRepository(session)
    at_one_day = repository.on_books_room_nights_by_date(hotel.id, stay_date, stay_date, 1)
    at_seven_days = repository.on_books_room_nights_by_date(hotel.id, stay_date, stay_date, 7)
    assert at_one_day.get(stay_date) == 2
    assert at_seven_days.get(stay_date) == 1, "the 3-days-ahead booking is not knowable a week out"


# --- capacity -------------------------------------------------------------------------------------


def test_capacity_counts_only_rooms_that_existed_at_the_cutoff(session: Session) -> None:
    hotel = make_hotel(session)
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    old_room = make_room(session, hotel, room_type, number="701")
    new_room = make_room(session, hotel, room_type, number="702")

    stay_date = ANCHOR
    old_room.created_at = utc(stay_date - dt.timedelta(days=60))
    new_room.created_at = utc(stay_date + dt.timedelta(days=10))  # created AFTER the target
    session.flush()

    build_stay(
        session,
        hotel,
        old_room,
        guest,
        check_in=stay_date,
        nights=1,
        booked_at=utc(stay_date - dt.timedelta(days=10)),
    )

    rooms = MlDemandRepository(session).rooms_existing_by_date(hotel.id, stay_date, stay_date, 1)
    assert rooms[stay_date] == 1, "a room created after the target date leaked into its capacity"


# --- tenant isolation -----------------------------------------------------------------------------


def test_one_hotels_bookings_never_reach_another_hotels_dataset(session: Session) -> None:
    """Two hotels, same dates, wildly different demand. Each dataset must see only its own."""
    quiet = make_hotel(session, slug="ml-quiet")
    busy = make_hotel(session, slug="ml-busy")

    for hotel, room_count in ((quiet, 1), (busy, 4)):
        room_type = make_room_type(session, hotel)
        guest = make_guest(session, hotel)
        for index in range(room_count):
            room = make_room(session, hotel, room_type, number=f"{800 + index}")
            build_stay(
                session,
                hotel,
                room,
                guest,
                check_in=ANCHOR,
                nights=2,
                booked_at=utc(ANCHOR - dt.timedelta(days=15)),
            )

    repository = MlDemandRepository(session)
    quiet_demand = repository.demand_by_date(quiet.id, ANCHOR, ANCHOR + dt.timedelta(days=2))
    busy_demand = repository.demand_by_date(busy.id, ANCHOR, ANCHOR + dt.timedelta(days=2))

    assert quiet_demand[ANCHOR] == 1
    assert busy_demand[ANCHOR] == 4

    service = MlDatasetService(repository)
    quiet_dataset = service.build_for_hotel(quiet.id, quiet.public_id)
    assert quiet_dataset.hotel_public_ids == (quiet.public_id,)
    assert all(row.target_room_nights == 1 for row in quiet_dataset.rows)
    assert busy.public_id not in quiet_dataset.hotel_public_ids


def test_a_multi_hotel_build_keeps_each_hotels_rows_separate(session: Session) -> None:
    first = make_hotel(session, slug="ml-first")
    second = make_hotel(session, slug="ml-second")
    for hotel, nights in ((first, 3), (second, 2)):
        room_type = make_room_type(session, hotel)
        guest = make_guest(session, hotel)
        room = make_room(session, hotel, room_type, number="900")
        build_stay(
            session,
            hotel,
            room,
            guest,
            check_in=ANCHOR,
            nights=nights,
            booked_at=utc(ANCHOR - dt.timedelta(days=15)),
        )

    service = MlDatasetService(MlDemandRepository(session))
    dataset = service.build_for_hotels([(first.id, first.public_id), (second.id, second.public_id)])

    by_hotel: dict[object, list[dt.date]] = {}
    for row in dataset.rows:
        by_hotel.setdefault(row.hotel_public_id, []).append(row.target_date)
    assert len(by_hotel[first.public_id]) == 3
    assert len(by_hotel[second.public_id]) == 2


# --- pipeline behaviour ---------------------------------------------------------------------------


def test_a_hotel_with_no_occupied_nights_fails_explicitly(session: Session) -> None:
    """An empty dataset would be carried on with. An exception is not."""
    hotel = make_hotel(session, slug="ml-empty")
    service = MlDatasetService(MlDemandRepository(session))
    with pytest.raises(InsufficientDataError, match="no occupied room nights"):
        service.build_for_hotel(hotel.id, hotel.public_id)


def test_the_pipeline_is_deterministic_across_repeated_builds(session: Session) -> None:
    hotel = make_hotel(session, slug="ml-deterministic")
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    room = make_room(session, hotel, room_type, number="950")
    build_stay(
        session,
        hotel,
        room,
        guest,
        check_in=ANCHOR,
        nights=10,
        booked_at=utc(ANCHOR - dt.timedelta(days=40)),
    )

    service = MlDatasetService(MlDemandRepository(session))
    first = service.build_for_hotel(hotel.id, hotel.public_id)
    second = service.build_for_hotel(hotel.id, hotel.public_id)

    assert [(r.target_date, dict(r.features), r.target_room_nights) for r in first.rows] == [
        (r.target_date, dict(r.features), r.target_room_nights) for r in second.rows
    ]
    assert first.dataset_version == second.dataset_version == "v1"
    assert first.feature_version == second.feature_version == "v1"


def test_the_dataset_carries_its_contract_and_no_internal_identifier(session: Session) -> None:
    hotel = make_hotel(session, slug="ml-contract")
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    room = make_room(session, hotel, room_type, number="960")
    build_stay(
        session,
        hotel,
        room,
        guest,
        check_in=ANCHOR,
        nights=4,
        booked_at=utc(ANCHOR - dt.timedelta(days=40)),
    )

    dataset = MlDatasetService(MlDemandRepository(session)).build_for_hotel(
        hotel.id, hotel.public_id
    )
    assert set(dataset.rows[0].features) == set(dataset.feature_names)
    for row in dataset.rows:
        # Structural, not a substring search: the internal key is a small integer, and small
        # integers legitimately appear as feature VALUES (day_of_month, month, ...). What must
        # be absent is a field carrying it.
        assert row.hotel_public_id == hotel.public_id
        assert not hasattr(row, "hotel_id")
        assert not any(name.endswith("_id") or name == "id" for name in row.features)
    assert dataset.hotel_public_ids == (hotel.public_id,)


def test_lags_reach_back_before_the_first_target_date(session: Session) -> None:
    """The extraction window is wider than the row window, so the earliest rows still have
    their history instead of arriving empty because of where the query started."""
    hotel = make_hotel(session, slug="ml-lookback")
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    room = make_room(session, hotel, room_type, number="970")
    start = ANCHOR - dt.timedelta(days=40)
    build_stay(
        session,
        hotel,
        room,
        guest,
        check_in=start,
        nights=45,
        booked_at=utc(start - dt.timedelta(days=60)),
    )

    service = MlDatasetService(MlDemandRepository(session))
    dataset = service.build_for_hotel(
        hotel.id, hotel.public_id, date_from=ANCHOR, date_to=ANCHOR + dt.timedelta(days=3)
    )
    assert dataset.rows, "expected rows in the requested window"
    first = dataset.rows[0]
    assert first.target_date == ANCHOR
    assert first.features["demand_lag_28"] == 1, (
        "the lag fell outside the extraction window rather than reading real history"
    )


def test_the_report_describes_what_was_built(session: Session) -> None:
    hotel = make_hotel(session, slug="ml-report")
    room_type = make_room_type(session, hotel)
    guest = make_guest(session, hotel)
    room = make_room(session, hotel, room_type, number="980")
    build_stay(
        session,
        hotel,
        room,
        guest,
        check_in=ANCHOR,
        nights=12,
        booked_at=utc(ANCHOR - dt.timedelta(days=40)),
    )

    service = MlDatasetService(MlDemandRepository(session))
    dataset = service.build_for_hotel(hotel.id, hotel.public_id)
    report = service.report(dataset)
    assert report.rows == 12
    assert report.hotels == 1
    assert report.date_range == (ANCHOR, ANCHOR + dt.timedelta(days=11))
