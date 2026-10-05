"""Arrivals and departures count bookings that occupy a room, nothing else (Issue F4).

A booking is an arrival -- or a departure -- in a range when its ``check_in_date`` (or
``check_out_date``) falls in it AND its status is in ``OCCUPANCY_STATUSES``: confirmed, checked in
or checked out, the same set that makes a night occupied. A cancelled booking and a no-show never
arrived, and a pending one was never committed. Before this, every status counted, so a booking
cancelled within the range was both an arrival and a cancellation.

``bookings_by_stay`` is deliberately unchanged: it is a breakdown by status of every booking whose
stay overlaps the range, and its total counts them all.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models.enums import BookingStatus
from app.models.hotel import Hotel
from app.repositories.analytics import AnalyticsRepository
from app.services.analytics import AnalyticsService
from tests.integration.conftest import make_booking, make_guest, make_hotel, requires_postgres

pytestmark = requires_postgres

D = dt.date
CHECK_IN, CHECK_OUT = D(2026, 3, 10), D(2026, 3, 12)
WINDOW = (D(2026, 3, 1), D(2026, 3, 31))
COUNTED = {"confirmed", "checked_in", "checked_out"}


def every_status(session: Session, hotel: Hotel) -> None:
    """One booking in each of the six statuses, all staying 10-12 March."""
    for status in BookingStatus:
        make_booking(
            session,
            hotel,
            make_guest(session, hotel),
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            status=status.value,
        )


def service(session: Session, hotel: Hotel) -> AnalyticsService:
    scope: Any = SimpleNamespace(require_hotel=lambda _: hotel)
    return AnalyticsService(AnalyticsRepository(session), scope)


@pytest.fixture
def hotel(session: Session) -> Hotel:
    hotel = make_hotel(session, slug="stay-flow")
    every_status(session, hotel)
    session.commit()
    return hotel


def test_the_six_statuses_are_the_ones_this_file_covers() -> None:
    assert {status.value for status in BookingStatus} == COUNTED | {
        "pending",
        "cancelled",
        "no_show",
    }


def test_only_bookings_that_occupy_a_room_arrive_and_depart(session: Session, hotel: Hotel) -> None:
    overview = service(session, hotel).overview(hotel.public_id, *WINDOW)

    assert overview.stay_flow.arrivals == len(COUNTED)
    assert overview.stay_flow.departures == len(COUNTED)


def test_the_daily_series_counts_the_same_bookings(session: Session, hotel: Hotel) -> None:
    analytics = service(session, hotel)
    days = {row.date: row for row in analytics.daily(hotel.public_id, *WINDOW).days}

    assert days[CHECK_IN].arrivals == len(COUNTED)
    assert days[CHECK_OUT].departures == len(COUNTED)
    assert sum(row.arrivals for row in days.values()) == len(COUNTED)
    assert sum(row.departures for row in days.values()) == len(COUNTED)


@pytest.mark.parametrize("status", sorted(COUNTED))
def test_each_counted_status_is_an_arrival_and_a_departure(session: Session, status: str) -> None:
    hotel = make_hotel(session, slug=f"counted-{status.replace('_', '-')}")
    make_booking(
        session,
        hotel,
        make_guest(session, hotel),
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        status=status,
    )
    session.commit()

    flow = service(session, hotel).overview(hotel.public_id, *WINDOW).stay_flow

    assert (flow.arrivals, flow.departures) == (1, 1)


@pytest.mark.parametrize("status", ["pending", "cancelled", "no_show"])
def test_a_booking_that_never_occupied_a_room_is_neither(session: Session, status: str) -> None:
    hotel = make_hotel(session, slug=f"uncounted-{status.replace('_', '-')}")
    make_booking(
        session,
        hotel,
        make_guest(session, hotel),
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        status=status,
    )
    session.commit()

    analytics = service(session, hotel)
    flow = analytics.overview(hotel.public_id, *WINDOW).stay_flow
    days = analytics.daily(hotel.public_id, *WINDOW).days

    assert (flow.arrivals, flow.departures) == (0, 0)
    assert all(row.arrivals == 0 and row.departures == 0 for row in days)


def test_a_booking_cancelled_in_the_range_is_a_cancellation_and_not_an_arrival(
    session: Session,
) -> None:
    hotel = make_hotel(session, slug="cancelled-in-range")
    booking = make_booking(
        session,
        hotel,
        make_guest(session, hotel),
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        status="cancelled",
    )
    booking.cancelled_at = dt.datetime(2026, 3, 5, 12, tzinfo=dt.UTC)
    session.commit()

    flow = service(session, hotel).overview(hotel.public_id, *WINDOW).stay_flow

    assert flow.cancellations == 1
    assert (flow.arrivals, flow.departures) == (0, 0)


def test_bookings_by_stay_still_counts_every_status(session: Session, hotel: Hotel) -> None:
    """The breakdown is unchanged on purpose: it answers "which bookings overlap", by status."""
    stay = service(session, hotel).overview(hotel.public_id, *WINDOW).bookings_by_stay

    assert stay.total == len(BookingStatus)
    for status in BookingStatus:
        assert getattr(stay, status.value) == 1, status


def test_a_date_outside_the_range_is_still_not_counted(session: Session, hotel: Hotel) -> None:
    flow = (
        service(session, hotel).overview(hotel.public_id, D(2026, 3, 11), D(2026, 3, 11)).stay_flow
    )

    assert (flow.arrivals, flow.departures) == (0, 0)
