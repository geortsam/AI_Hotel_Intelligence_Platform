"""Booking and cancellation days are the hotel's calendar days, whatever the session says (F1).

``booked_at`` and ``cancelled_at`` are instants. For a hotel H an instant T belongs to the
hotel-local date ``(T AT TIME ZONE effective_timezone(H))::date``, where the effective timezone is
``hotels.timezone`` when PostgreSQL knows it as a named zone (``pg_timezone_names``) and UTC
otherwise. A range of hotel-local dates ``[date_from, date_to]`` is the instants from local
midnight of ``date_from`` (inclusive) to local midnight of ``date_to + 1`` (exclusive).

Every query below runs on a connection whose session ``TimeZone`` is set explicitly -- UTC,
Europe/Bucharest (this project's development server, whose offsets match Athens and once hid the
bug) and America/Los_Angeles -- and asserts that setting is in force, so a machine whose default
happens to match a hotel cannot make a regression pass. The expected dates are written out by
hand: they are the specification, not a computation that could share the bug.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.hotel import Hotel
from app.repositories.analytics import FALLBACK_TIMEZONE, AnalyticsRepository
from app.services.analytics import AnalyticsService
from app.services.intelligence import IntelligenceService
from tests.integration.conftest import (
    TEST_DATABASE_URL,
    authenticated_client,
    make_booking,
    make_guest,
    make_hotel,
    requires_postgres,
)

pytestmark = requires_postgres

UTC = dt.UTC
D = dt.date
SESSION_ZONES = ["UTC", "Europe/Bucharest", "America/Los_Angeles"]
WIDE = (D(2026, 1, 1), D(2026, 12, 31))


def at(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> dt.datetime:
    """An instant, in UTC."""
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=UTC)


@contextmanager
def session_in(engine: Engine, zone: str) -> Iterator[Session]:
    """A session on one connection whose ``TimeZone`` is *zone*, checked before use."""
    with engine.connect() as connection:
        connection.execute(sa.text("SELECT set_config('TimeZone', :zone, false)"), {"zone": zone})
        assert connection.execute(sa.text("SHOW TimeZone")).scalar_one() == zone
        with Session(bind=connection) as session:
            yield session


def hotel_in(session: Session, zone: str, slug: str) -> Hotel:
    hotel = make_hotel(session, slug=slug)
    hotel.timezone = zone
    session.flush()
    return hotel


def book(
    session: Session,
    hotel: Hotel,
    booked_at: dt.datetime,
    *,
    cancelled_at: dt.datetime | None = None,
) -> None:
    """One booking taken at *booked_at*, cancelled at *cancelled_at* if given."""
    guest = make_guest(session, hotel)
    booking = make_booking(
        session,
        hotel,
        guest,
        check_in=D(2027, 1, 10),
        check_out=D(2027, 1, 11),
        status="cancelled" if cancelled_at else "confirmed",
    )
    booking.booked_at = booked_at
    booking.cancelled_at = cancelled_at
    session.flush()


def created(engine: Engine, zone: str, hotel: Hotel, window: tuple[D, D] = WIDE) -> dict[D, int]:
    with session_in(engine, zone) as session:
        repository = AnalyticsRepository(session)
        business = repository.business_timezone(hotel.id)
        return repository.bookings_created_by_day(hotel.id, *window, zone=business)


def cancelled(engine: Engine, zone: str, hotel: Hotel, window: tuple[D, D] = WIDE) -> dict[D, int]:
    with session_in(engine, zone) as session:
        repository = AnalyticsRepository(session)
        business = repository.business_timezone(hotel.id)
        return repository.cancellations_by_day(hotel.id, *window, zone=business)


def scope_of(hotel: Hotel) -> Any:
    return SimpleNamespace(require_hotel=lambda _: hotel)


# ======================================================================================
# A. Hotel ahead of UTC: Europe/Athens, UTC+2 in March
# ======================================================================================


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_athens_bookings_fall_on_the_hotels_own_calendar_day(
    engine: Engine, session: Session, session_zone: str
) -> None:
    hotel = hotel_in(session, "Europe/Athens", "athens")
    book(session, hotel, at(2026, 3, 10, 21, 59, 59))  # 23:59:59 local: still the 10th
    book(session, hotel, at(2026, 3, 10, 22, 0, 0))  # 00:00:00 local: exactly the new day
    book(session, hotel, at(2026, 3, 10, 22, 30))  # 00:30 local, before UTC midnight
    book(session, hotel, at(2026, 3, 11, 0, 0, 0))  # UTC midnight, 02:00 local: nothing special
    session.commit()

    assert created(engine, session_zone, hotel) == {D(2026, 3, 10): 1, D(2026, 3, 11): 3}


# ======================================================================================
# B. Hotel behind UTC: America/New_York, UTC-4 after 8 March
# ======================================================================================


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_new_york_bookings_fall_on_the_hotels_own_calendar_day(
    engine: Engine, session: Session, session_zone: str
) -> None:
    hotel = hotel_in(session, "America/New_York", "new-york")
    book(session, hotel, at(2026, 3, 11, 0, 0, 0))  # UTC midnight, 20:00 local on the 10th
    book(session, hotel, at(2026, 3, 11, 1, 0))  # past UTC midnight, 21:00 local on the 10th
    book(session, hotel, at(2026, 3, 11, 3, 59, 59))  # 23:59:59 local: still the 10th
    book(session, hotel, at(2026, 3, 11, 4, 0, 0))  # 00:00:00 local: exactly the 11th
    session.commit()

    assert created(engine, session_zone, hotel) == {D(2026, 3, 10): 3, D(2026, 3, 11): 1}


# ======================================================================================
# C. One instant, several hotels
# ======================================================================================


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_the_same_instant_belongs_to_each_hotels_own_day(
    engine: Engine, session: Session, session_zone: str
) -> None:
    instant = at(2026, 3, 10, 23, 30)
    hotels = {
        zone: hotel_in(session, zone, f"same-{index}")
        for index, zone in enumerate(["Europe/Athens", "Asia/Tokyo", "America/New_York", "UTC"])
    }
    for hotel in hotels.values():
        book(session, hotel, instant)
    session.commit()

    days = {zone: list(created(engine, session_zone, hotel)) for zone, hotel in hotels.items()}

    assert days == {
        "Europe/Athens": [D(2026, 3, 11)],
        "Asia/Tokyo": [D(2026, 3, 11)],
        "America/New_York": [D(2026, 3, 10)],
        "UTC": [D(2026, 3, 10)],
    }


# ======================================================================================
# D. Daylight saving: IANA rules, never a fixed offset
# ======================================================================================


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_the_day_clocks_go_forward_is_23_hours_long(
    engine: Engine, session: Session, session_zone: str
) -> None:
    """Athens moves from UTC+2 to UTC+3 at 01:00Z on 29 March 2026: local 29 March runs from
    28 March 22:00Z to 29 March 21:00Z."""
    hotel = hotel_in(session, "Europe/Athens", "spring")
    book(session, hotel, at(2026, 3, 28, 21, 59, 59))  # 23:59:59 on the 28th (+2)
    book(session, hotel, at(2026, 3, 28, 22, 0, 0))  # 00:00:00 on the 29th (+2)
    book(session, hotel, at(2026, 3, 29, 20, 59, 59))  # 23:59:59 on the 29th (+3)
    book(session, hotel, at(2026, 3, 29, 21, 0, 0))  # 00:00:00 on the 30th (+3)
    session.commit()

    assert created(engine, session_zone, hotel) == {
        D(2026, 3, 28): 1,
        D(2026, 3, 29): 2,
        D(2026, 3, 30): 1,
    }


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_the_day_clocks_go_back_is_25_hours_long(
    engine: Engine, session: Session, session_zone: str
) -> None:
    """Athens moves from UTC+3 back to UTC+2 at 01:00Z on 25 October 2026: local 25 October
    runs from 24 October 21:00Z to 25 October 22:00Z."""
    hotel = hotel_in(session, "Europe/Athens", "autumn")
    book(session, hotel, at(2026, 10, 24, 20, 59, 59))  # 23:59:59 on the 24th (+3)
    book(session, hotel, at(2026, 10, 24, 21, 0, 0))  # 00:00:00 on the 25th (+3)
    book(session, hotel, at(2026, 10, 25, 21, 59, 59))  # 23:59:59 on the 25th (+2)
    book(session, hotel, at(2026, 10, 25, 22, 0, 0))  # 00:00:00 on the 26th (+2)
    session.commit()

    assert created(engine, session_zone, hotel) == {
        D(2026, 10, 24): 1,
        D(2026, 10, 25): 2,
        D(2026, 10, 26): 1,
    }


# ======================================================================================
# E. Range semantics: [local midnight of date_from, local midnight of date_to + 1)
# ======================================================================================


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_a_one_day_range_is_exactly_that_local_day(
    engine: Engine, session: Session, session_zone: str
) -> None:
    """The local 11th is [10th 22:00Z, 11th 22:00Z): four of these instants. A filter on the
    session's calendar would select a different set -- the UTC 11th holds only two of them, and
    so does Los Angeles' -- which is what makes the count prove where the bounds are."""
    hotel = hotel_in(session, "Europe/Athens", "range")
    edges = [
        at(2026, 3, 10, 21, 59, 59),  # the 10th: before the range
        at(2026, 3, 10, 22, 0, 0),  # local midnight starting the 11th: inside (inclusive)
        at(2026, 3, 10, 22, 30),  # the local 11th, still the UTC 10th: inside
        at(2026, 3, 10, 23, 30),  # the local 11th, still the UTC 10th: inside
        at(2026, 3, 11, 21, 59, 59),  # last second of the 11th: inside
        at(2026, 3, 11, 22, 0, 0),  # local midnight starting the 12th: outside (exclusive)
    ]
    for instant in edges:
        book(session, hotel, at(2026, 2, 1, 12), cancelled_at=instant)
        book(session, hotel, instant)
    session.commit()

    day = D(2026, 3, 11)
    with session_in(engine, session_zone) as reader:
        repository = AnalyticsRepository(reader)
        zone = repository.business_timezone(hotel.id)
        by_status = repository.booking_counts_by_status(hotel.id, day, day, zone=zone)
        cancellations = repository.cancellation_count(hotel.id, day, day, zone=zone)
        by_day = repository.bookings_created_by_day(hotel.id, day, day, zone=zone)
        cancelled_by_day = repository.cancellations_by_day(hotel.id, day, day, zone=zone)

    assert by_status == {"confirmed": 4}
    assert cancellations == 4
    assert by_day == {day: 4}
    assert cancelled_by_day == {day: 4}


# ======================================================================================
# F. Cancellations follow the same rule
# ======================================================================================


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_cancellations_fall_on_the_hotels_own_calendar_day(
    engine: Engine, session: Session, session_zone: str
) -> None:
    athens = hotel_in(session, "Europe/Athens", "cancel-athens")
    new_york = hotel_in(session, "America/New_York", "cancel-new-york")
    taken = at(2026, 2, 1, 12)
    book(session, athens, taken, cancelled_at=at(2026, 3, 10, 22, 30))  # 00:30 on the 11th
    book(session, athens, taken, cancelled_at=at(2026, 3, 10, 21, 59, 59))  # 23:59:59 on the 10th
    book(session, new_york, taken, cancelled_at=at(2026, 3, 11, 1, 0))  # 21:00 on the 10th
    session.commit()

    assert cancelled(engine, session_zone, athens) == {D(2026, 3, 10): 1, D(2026, 3, 11): 1}
    assert cancelled(engine, session_zone, new_york) == {D(2026, 3, 10): 1}
    # The booking itself was taken on 1 February, and stays counted there.
    assert created(engine, session_zone, athens) == {D(2026, 2, 1): 2}


# ======================================================================================
# G. The effective timezone: named zones only, UTC otherwise
# ======================================================================================


@pytest.mark.parametrize(
    ("declared", "effective"),
    [
        ("Europe/Athens", "Europe/Athens"),
        ("America/New_York", "America/New_York"),
        ("UTC", "UTC"),
        ("Mars/Olympus", "UTC"),  # unknown
        ("+02", "UTC"),  # a fixed offset is not a named zone
        ("europe/athens", "UTC"),  # names are exact
        ("", "UTC"),
    ],
)
def test_the_effective_timezone_is_a_postgresql_named_zone_or_utc(
    engine: Engine, session: Session, declared: str, effective: str
) -> None:
    hotel = hotel_in(session, declared, "declared")
    session.commit()
    with session_in(engine, "UTC") as reader:
        assert AnalyticsRepository(reader).business_timezone(hotel.id) == effective
    assert FALLBACK_TIMEZONE == "UTC"


@pytest.mark.parametrize("declared", ["Mars/Olympus", "+02"])
@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_an_unrecognised_timezone_buckets_in_utc_and_does_not_fail(
    engine: Engine, session: Session, session_zone: str, declared: str
) -> None:
    hotel = hotel_in(session, declared, "unknown-zone")
    book(session, hotel, at(2026, 3, 10, 23, 30))  # the 10th in UTC; the 11th in Athens
    book(session, hotel, at(2026, 2, 1, 12), cancelled_at=at(2026, 3, 11, 0, 30))
    session.commit()

    assert created(engine, session_zone, hotel) == {D(2026, 2, 1): 1, D(2026, 3, 10): 1}
    assert cancelled(engine, session_zone, hotel) == {D(2026, 3, 11): 1}


# ======================================================================================
# H. The analytics service: overview and daily series agree, day by day
# ======================================================================================


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_the_overview_and_the_daily_series_count_the_same_local_days(
    engine: Engine, session: Session, session_zone: str
) -> None:
    hotel = hotel_in(session, "Europe/Athens", "consistent")
    for instant in [
        at(2026, 3, 9, 21, 30),  # the 9th
        at(2026, 3, 9, 22, 30),  # the 10th
        at(2026, 3, 10, 22, 0),  # the 11th, at local midnight
        at(2026, 3, 11, 21, 59, 59),  # the 11th
        at(2026, 3, 12, 0, 0),  # the 12th (UTC midnight)
    ]:
        book(session, hotel, instant)
        book(session, hotel, at(2026, 2, 1, 12), cancelled_at=instant)
    session.commit()

    days = [D(2026, 3, 9) + dt.timedelta(days=n) for n in range(4)]
    with session_in(engine, session_zone) as reader:
        service = AnalyticsService(AnalyticsRepository(reader), scope_of(hotel))
        daily = service.daily(hotel.public_id, days[0], days[-1])
        overviews = [service.overview(hotel.public_id, day, day) for day in days]
        whole = service.overview(hotel.public_id, days[0], days[-1])

    expected = [1, 1, 2, 1]
    assert [row.bookings_created for row in daily.days] == expected
    assert [row.cancellations for row in daily.days] == expected
    assert [o.bookings_created.total for o in overviews] == expected
    assert [o.stay_flow.cancellations for o in overviews] == expected
    assert whole.bookings_created.total == sum(expected)
    assert whole.stay_flow.cancellations == sum(expected)


# ======================================================================================
# I. Intelligence: the booking trend and the booking-volume anomaly scan
# ======================================================================================


def intelligence(session: Session, hotel: Hotel) -> IntelligenceService:
    return IntelligenceService(
        AnalyticsRepository(session),
        scope_of(hotel),
        SimpleNamespace(periods=lambda _: []),  # type: ignore[arg-type]
    )


def take_on_local_days(session: Session, hotel: Hotel, counts: dict[D, int]) -> None:
    """*counts[d]* bookings taken at 00:30 Athens time on d -- 22:30Z on the day before, so a
    session-zone or UTC bucketing would move every one of them a day earlier."""
    for day, count in counts.items():
        for _ in range(count):
            book(
                session,
                hotel,
                dt.datetime.combine(day - dt.timedelta(days=1), dt.time(22, 30), UTC),
            )


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_the_booking_trend_reads_the_hotels_calendar_days(
    engine: Engine, session: Session, session_zone: str
) -> None:
    """Local counts [1, 1, 1 | 5, 5, 1, 1]: medians 1 and 3, increasing. Bucketed a day early
    they would be [1, 1, 5 | 5, 1, 1, 0]: medians 1 and 1, stable."""
    hotel = hotel_in(session, "Europe/Athens", "trend")
    window = [D(2026, 1, 5) + dt.timedelta(days=n) for n in range(7)]
    take_on_local_days(session, hotel, dict(zip(window, [1, 1, 1, 5, 5, 1, 1], strict=True)))
    session.commit()

    with session_in(engine, session_zone) as reader:
        trend = intelligence(reader, hotel).demand_trend(hotel.public_id, window[0], window[-1])

    assert (trend.earlier_median, trend.recent_median) == (1, 3)
    assert trend.direction == "increasing"


@pytest.mark.parametrize("session_zone", SESSION_ZONES)
def test_a_booking_volume_anomaly_is_dated_on_the_hotels_calendar_day(
    engine: Engine, session: Session, session_zone: str
) -> None:
    hotel = hotel_in(session, "Europe/Athens", "anomaly")
    window = [D(2026, 2, 2) + dt.timedelta(days=n) for n in range(14)]
    spike = D(2026, 2, 11)
    counts = {day: (8 if day == spike else 1 + n % 2) for n, day in enumerate(window)}
    take_on_local_days(session, hotel, counts)
    session.commit()

    with session_in(engine, session_zone) as reader:
        response = intelligence(reader, hotel).anomalies(hotel.public_id, window[0], window[-1])

    flagged = [a.date for a in response.anomalies if a.metric == "bookings_created"]
    assert flagged == [spike]


# ======================================================================================
# J. End to end, through the HTTP API, on a server whose sessions are not in the hotel's zone
# ======================================================================================


@pytest.fixture
def los_angeles_engine() -> Iterator[Engine]:
    """An engine whose every connection starts in America/Los_Angeles."""
    assert TEST_DATABASE_URL is not None
    engine = sa.create_engine(
        TEST_DATABASE_URL,
        poolclass=sa.pool.NullPool,
        connect_args={"options": "-c TimeZone=America/Los_Angeles"},
    )
    yield engine
    engine.dispose()


def test_the_api_reports_hotel_local_days_whatever_the_server_session_zone(
    engine: Engine, session: Session, los_angeles_engine: Engine
) -> None:
    """``session`` is requested for its teardown, which truncates what this test wrote."""
    with los_angeles_engine.connect() as probe:
        assert probe.execute(sa.text("SHOW TimeZone")).scalar_one() == "America/Los_Angeles"
    api = authenticated_client(los_angeles_engine, email="f1-api@example.test")
    ids: dict[str, str] = {}
    for slug, zone in [("api-athens", "Europe/Athens"), ("api-unknown", "Mars/Olympus")]:
        response = api.post(
            "/api/v1/hotels",
            json={
                "slug": slug,
                "name": slug,
                "address_line1": "1 Street",
                "city": "Athens",
                "country_code": "GR",
                "timezone": zone,
                "currency": "EUR",
            },
        )
        assert response.status_code == 201, response.text
        ids[zone] = str(response.json()["public_id"])

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as writer:
        for public_id in ids.values():
            hotel = writer.scalars(sa.select(Hotel).where(Hotel.public_id == public_id)).one()
            book(writer, hotel, at(2026, 3, 10, 22, 30))
        writer.commit()

    params = {"date_from": "2026-03-10", "date_to": "2026-03-11"}
    athens = api.get(f"/api/v1/hotels/{ids['Europe/Athens']}/analytics/daily", params=params)
    unknown = api.get(f"/api/v1/hotels/{ids['Mars/Olympus']}/analytics/daily", params=params)

    assert athens.status_code == 200 and unknown.status_code == 200
    assert [d["bookings_created"] for d in athens.json()["days"]] == [0, 1]  # the 11th
    assert [d["bookings_created"] for d in unknown.json()["days"]] == [1, 0]  # UTC: the 10th
