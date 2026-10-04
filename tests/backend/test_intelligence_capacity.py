"""The statistical occupancy forecast's capacity rule, without a database (audit findings D1, D2).

``docs/ml-design.md`` §2: a prediction above the hotel's active rooms is reduced to them and
flagged ``capacity_clamped``, because the schema itself asserts occupied <= available. These tests
hold every occupancy figure the forecast publishes to that rule -- the prediction, both interval
bounds (D1), and the occupancy-outlook finding that summarises them (D2) -- at and around the
boundary.

Only the statistical OCCUPANCY forecast. The learned DEMAND model's output is unconstrained by
contract and reported beside ``exceeds_capacity`` (``tests/integration/test_ml_serving_api.py``);
historical occupancy keeps its current-inventory basis
(``tests/integration/test_analytics_api.py``). Neither is touched here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.ml.timeseries import Observation, forecast_series
from app.repositories.analytics import OccupancyCounts
from app.schemas.intelligence import Insight, OccupancyForecastPoint
from app.services.intelligence import IntelligenceService

START = dt.date(2026, 3, 2)
TRAINING = 28
WINDOW = (START, START + dt.timedelta(days=TRAINING - 1))
HORIZON = (START + dt.timedelta(days=TRAINING), START + dt.timedelta(days=TRAINING + 6))


def nightly(base: int, *, alternate: bool = False) -> dict[dt.date, int]:
    """Occupied nights for every training day: *base*, or *base* and *base*+1 alternating."""
    return {
        START + dt.timedelta(days=n): base + (n % 2 if alternate else 0) for n in range(TRAINING)
    }


class FakeAnalytics:
    def __init__(self, history: dict[dt.date, int], rooms: int) -> None:
        self.history = history
        self.rooms = rooms

    def occupied_nights_by_day(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, OccupancyCounts]:
        return {
            day: OccupancyCounts(occupied=n, sold=n, complimentary=0)
            for day, n in self.history.items()
            if date_from <= day <= date_to
        }

    def active_room_count(self, hotel_id: int) -> int:
        return self.rooms

    def business_timezone(self, hotel_id: int) -> str:
        return "UTC"

    def bookings_created_by_day(self, *args: Any, **kwargs: Any) -> dict[dt.date, int]:
        return {}

    def room_revenue_by_day(self, *args: Any) -> dict[dt.date, list[Any]]:
        return {}


HOTEL = SimpleNamespace(id=1, public_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))


def service(history: dict[dt.date, int], rooms: int) -> IntelligenceService:
    return IntelligenceService(
        FakeAnalytics(history, rooms),  # type: ignore[arg-type]
        SimpleNamespace(require_hotel=lambda _: HOTEL),  # type: ignore[arg-type]
        SimpleNamespace(periods=lambda _: [WINDOW]),  # type: ignore[arg-type]
    )


def first_point(history: dict[dt.date, int], rooms: int) -> OccupancyForecastPoint:
    return service(history, rooms).occupancy_forecast(HOTEL.public_id, *HORIZON, TRAINING).points[0]


def outlook(history: dict[dt.date, int], rooms: int) -> dict[str, str]:
    insights = service(history, rooms).insights(HOTEL.public_id, *WINDOW, 7).insights
    found: Insight = next(i for i in insights if i.type == "occupancy_outlook")
    return {m.name: m.value for m in found.supporting_metrics}


def raw_first(history: dict[dt.date, int]) -> Any:
    """What the model itself produced for the first horizon day, before any rendering."""
    observations = [Observation(date=d, value=Decimal(n)) for d, n in sorted(history.items())]
    days = [HORIZON[0] + dt.timedelta(days=n) for n in range(7)]
    return forecast_series(observations, days)[0]


def assert_coherent(point: OccupancyForecastPoint, rooms: int) -> None:
    """Every published occupancy figure is one the hotel can hold, and the interval contains
    the prediction it is the interval of."""
    assert point.predicted_room_nights is not None
    assert point.interval_lower is not None and point.interval_upper is not None
    for figure in (point.predicted_room_nights, point.interval_lower, point.interval_upper):
        assert figure <= rooms
    assert point.interval_lower <= point.predicted_room_nights <= point.interval_upper


# --- D1: the interval is held to capacity with the prediction -----------------------------------


def test_one_room_above_capacity_clamps_the_point_and_its_interval() -> None:
    """Four rooms a night of history, one room left: the point was clamped, and its interval
    of [4, 4] used to be published around it -- an interval that excluded its own point."""
    point = first_point(nightly(4), rooms=1)

    assert point.capacity_clamped is True
    assert (point.predicted_room_nights, point.interval_lower, point.interval_upper) == (
        Decimal("1.0000"),
        Decimal("1.0000"),
        Decimal("1.0000"),
    )
    assert point.predicted_occupancy_rate == Decimal("1.0000")
    assert_coherent(point, rooms=1)


def test_a_wide_interval_above_capacity_is_held_to_it() -> None:
    history = nightly(4, alternate=True)
    raw = raw_first(history)
    assert raw.lower > 3, "the raw interval lies wholly above capacity, so this proves something"

    point = first_point(history, rooms=3)

    assert point.capacity_clamped is True
    assert (point.interval_lower, point.interval_upper) == (Decimal("3.0000"), Decimal("3.0000"))
    assert_coherent(point, rooms=3)


def test_exactly_at_capacity_is_not_clamped() -> None:
    """A prediction equal to capacity is a possible occupancy: the comparison is strict."""
    point = first_point(nightly(4), rooms=4)

    assert point.capacity_clamped is False
    assert point.predicted_room_nights == Decimal("4.0000")
    assert (point.interval_lower, point.interval_upper) == (Decimal("4.0000"), Decimal("4.0000"))
    assert point.predicted_occupancy_rate == Decimal("1.0000")


def test_an_upper_bound_above_capacity_is_held_to_it_while_the_point_is_not_clamped() -> None:
    """Three or four of four rooms: the point fits, but the raw upper bound claimed nights the
    hotel cannot sell. Only that bound moves; the point, the lower bound and the flag do not."""
    history = nightly(3, alternate=True)
    raw = raw_first(history)
    assert raw.upper > 4 >= raw.value

    point = first_point(history, rooms=4)

    assert point.capacity_clamped is False
    assert point.predicted_room_nights == Decimal("3.5000")
    assert point.interval_lower == raw.lower.quantize(Decimal("0.0001"))
    assert point.interval_upper == Decimal("4.0000")
    assert_coherent(point, rooms=4)


def test_a_forecast_within_capacity_is_published_unchanged() -> None:
    history = nightly(2, alternate=True)
    raw = raw_first(history)

    point = first_point(history, rooms=4)

    assert point.capacity_clamped is False
    assert point.predicted_room_nights == raw.value.quantize(Decimal("0.0001"))
    assert point.interval_lower == raw.lower.quantize(Decimal("0.0001"))
    assert point.interval_upper == raw.upper.quantize(Decimal("0.0001"))


def test_a_hotel_with_no_rooms_publishes_zero_and_no_rate() -> None:
    point = first_point(nightly(4), rooms=0)

    assert point.capacity_clamped is True
    assert (point.predicted_room_nights, point.interval_lower, point.interval_upper) == (
        Decimal("0.0000"),
        Decimal("0.0000"),
        Decimal("0.0000"),
    )
    assert point.predicted_occupancy_rate is None


@pytest.mark.parametrize(
    ("history", "rooms"),
    [
        (nightly(4), 1),
        (nightly(4, alternate=True), 3),
        (nightly(3, alternate=True), 4),
        (nightly(2, alternate=True), 4),
        (nightly(4), 4),
    ],
)
def test_every_horizon_day_is_coherent(history: dict[dt.date, int], rooms: int) -> None:
    points = service(history, rooms).occupancy_forecast(HOTEL.public_id, *HORIZON, TRAINING).points
    for point in points:
        assert_coherent(point, rooms)


# --- D2: the outlook summarises the forecast as published ----------------------------------------


def test_the_outlook_never_reports_more_than_capacity() -> None:
    """The finding used to sum the unclamped days: 28 room nights against 7, "400.0%"."""
    figures = outlook(nightly(4), rooms=1)

    assert Decimal(figures["predicted_room_nights"]) == 7
    assert figures["available_room_nights"] == "7"
    assert figures["predicted_occupancy_rate"] == "1.0000"


@pytest.mark.parametrize(
    ("history", "rooms"),
    [
        (nightly(4), 1),
        (nightly(4, alternate=True), 3),
        (nightly(3, alternate=True), 4),
        (nightly(2, alternate=True), 4),
    ],
)
def test_the_outlook_is_the_sum_of_the_days_the_endpoint_publishes(
    history: dict[dt.date, int], rooms: int
) -> None:
    """``_occupancy_outlook_insight`` says it is built from the same forecast the endpoint
    returns; its total is therefore that forecast's days, added up."""
    points = service(history, rooms).occupancy_forecast(HOTEL.public_id, *HORIZON, TRAINING).points
    published = sum((p.predicted_room_nights or Decimal(0) for p in points), Decimal(0))

    figures = outlook(history, rooms)

    assert Decimal(figures["predicted_room_nights"]) == published
    assert Decimal(figures["predicted_occupancy_rate"]) <= 1


def test_the_outlook_within_capacity_is_unchanged() -> None:
    figures = outlook(nightly(2, alternate=True), rooms=4)

    assert Decimal(figures["predicted_room_nights"]) == Decimal("17.5")
    assert figures["predicted_occupancy_rate"] == "0.6250"


def test_the_outlook_for_a_hotel_with_no_rooms_has_no_rate() -> None:
    figures = outlook(nightly(4), rooms=0)

    assert Decimal(figures["predicted_room_nights"]) == 0
    assert figures["available_room_nights"] == "0"
    assert figures["predicted_occupancy_rate"] == "undefined"
