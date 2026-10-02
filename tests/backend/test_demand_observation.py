"""Observed zero versus unobserved: the declared-period contract, without a database.

Migration 0016 makes observation explicit. A date is observed only inside a declared
``ObservationPeriod`` (both ends inclusive); there an absent count is a real 0, and everywhere
else a date's demand is unknown -- ``None`` to every lag and window that reaches it, whatever was
recorded for it. These tests pin that rule where it is written (:mod:`app.ml.dataset`), where the
model's serving reads it (:func:`app.ml.serving.build_feature_values`), and where an operator
declares it (:class:`app.services.demand_observation.DemandObservationService`).

The database half -- the exclusion and CHECK constraints, the operator command, the live
extractors -- is in ``tests/integration/test_demand_observation.py`` and the dataset, serving and
intelligence integration suites.
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.ml.dataset import (
    DatasetContractError,
    ObservationPeriod,
    lag_features,
    observed_days,
    observed_demand,
    rolling_mean_features,
)
from app.ml.serving import InsufficientFeatureHistoryError, build_feature_values
from app.services.demand_observation import OVERLAP_CONSTRAINT, DemandObservationService

BASE = dt.date(2026, 3, 1)


def day(offset: int) -> dt.date:
    return BASE + dt.timedelta(days=offset)


# --- the period ---------------------------------------------------------------------------------


def test_a_period_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(DatasetContractError, match="cannot end"):
        ObservationPeriod(day(5), day(4))


def test_a_single_day_period_is_valid_and_includes_exactly_that_day() -> None:
    period = ObservationPeriod(day(3), day(3))

    assert period.includes(day(3))
    assert not period.includes(day(2))
    assert not period.includes(day(4))


def test_both_ends_of_a_period_are_inclusive() -> None:
    period = ObservationPeriod(day(0), day(9))

    assert [period.includes(day(n)) for n in (-1, 0, 9, 10)] == [False, True, True, False]


def test_observed_days_are_the_declared_days_clipped_to_the_range() -> None:
    periods = [ObservationPeriod(day(0), day(9))]

    assert observed_days(periods, day(-5), day(20)) == tuple(day(n) for n in range(10))
    assert observed_days(periods, day(3), day(5)) == (day(3), day(4), day(5))
    assert observed_days(periods, day(9), day(9)) == (day(9),)
    assert observed_days(periods, day(10), day(20)) == ()
    assert observed_days(periods, day(-5), day(-1)) == ()


def test_several_periods_are_a_union_in_date_order() -> None:
    periods = [ObservationPeriod(day(7), day(8)), ObservationPeriod(day(0), day(2))]

    assert observed_days(periods, day(0), day(10)) == (day(0), day(1), day(2), day(7), day(8))


def test_no_period_observes_nothing() -> None:
    assert observed_days([], day(0), day(100)) == ()
    assert observed_demand({day(1): 4}, [], day(0), day(10)) == {}


# --- the series ---------------------------------------------------------------------------------


def test_an_observed_day_with_bookings_keeps_its_count() -> None:
    demand = observed_demand({day(1): 4}, [ObservationPeriod(day(0), day(2))], day(0), day(2))

    assert demand[day(1)] == 4


def test_an_observed_day_without_bookings_is_zero() -> None:
    demand = observed_demand({day(1): 4}, [ObservationPeriod(day(0), day(2))], day(0), day(2))

    assert demand == {day(0): 0, day(1): 4, day(2): 0}


def test_an_unobserved_day_is_absent_even_when_nights_were_recorded_for_it() -> None:
    """A recorded count outside every period cannot show it is the whole count."""
    recorded = {day(n): 3 for n in range(-2, 5)}
    demand = observed_demand(recorded, [ObservationPeriod(day(0), day(2))], day(-2), day(4))

    assert sorted(demand) == [day(0), day(1), day(2)]
    assert day(-1) not in demand and day(3) not in demand


# --- lags and windows ---------------------------------------------------------------------------


def lagged(demand: dict[dt.date, int], target: dt.date, lag: int) -> int | None:
    return lag_features(demand, target, 1, [lag])[f"demand_lag_{lag}"]


def test_a_lag_over_an_observed_zero_is_zero() -> None:
    demand = observed_demand({}, [ObservationPeriod(day(0), day(10))], day(0), day(10))

    assert lagged(demand, day(8), 7) == 0


def test_a_lag_over_an_unobserved_day_is_none() -> None:
    demand = observed_demand({day(0): 5}, [ObservationPeriod(day(1), day(10))], day(0), day(10))

    assert lagged(demand, day(7), 7) is None
    assert lagged(demand, day(8), 7) == 0


def test_a_lag_one_day_inside_either_end_is_known_and_one_day_outside_is_not() -> None:
    periods = [ObservationPeriod(day(10), day(20))]
    demand = observed_demand({}, periods, day(0), day(30))

    assert lagged(demand, day(11), 1) == 0  # reads day 10, the first declared day
    assert lagged(demand, day(10), 1) is None  # reads day 9
    assert lagged(demand, day(21), 1) == 0  # reads day 20, the last declared day
    assert lagged(demand, day(22), 1) is None  # reads day 21


def test_a_rolling_mean_includes_observed_zeros() -> None:
    recorded = {day(n): 7 for n in range(0, 7, 2)}  # 7 on days 0, 2, 4, 6
    demand = observed_demand(recorded, [ObservationPeriod(day(0), day(6))], day(0), day(6))

    mean = rolling_mean_features(demand, day(7), 1, [7])["demand_rolling_mean_7"]

    assert mean == pytest.approx(4.0)  # 28 / 7, the three zeros counted


def test_a_rolling_mean_crossing_an_unobserved_day_is_none() -> None:
    periods = [ObservationPeriod(day(0), day(2)), ObservationPeriod(day(4), day(6))]
    demand = observed_demand({day(n): 1 for n in range(7)}, periods, day(0), day(6))

    assert rolling_mean_features(demand, day(7), 1, [7])["demand_rolling_mean_7"] is None
    assert rolling_mean_features(demand, day(7), 1, [3])["demand_rolling_mean_3"] == 1.0


# --- serving ------------------------------------------------------------------------------------

TARGET = dt.date(2026, 6, 1)


def served(periods: list[ObservationPeriod]) -> dict[str, float]:
    recorded = {TARGET - dt.timedelta(days=offset): 3 for offset in (7, 28)}
    window_from, window_to = TARGET - dt.timedelta(days=28), TARGET - dt.timedelta(days=7)
    return build_feature_values(observed_demand(recorded, periods, window_from, window_to), TARGET)


def test_serving_accepts_an_observed_zero_lag() -> None:
    """Nothing recorded fourteen days back, and that day is declared: a 0, scored."""
    features = served(
        [ObservationPeriod(TARGET - dt.timedelta(days=28), TARGET - dt.timedelta(days=7))]
    )

    assert features["demand_lag_14"] == 0.0
    assert features["demand_lag_7"] == features["demand_lag_28"] == 3.0


def test_serving_refuses_an_unobserved_lag() -> None:
    """The same records with the fourteen-day lag left undeclared: unknown, so refused."""
    lag_14 = TARGET - dt.timedelta(days=14)
    periods = [
        ObservationPeriod(TARGET - dt.timedelta(days=28), lag_14 - dt.timedelta(days=1)),
        ObservationPeriod(lag_14 + dt.timedelta(days=1), TARGET - dt.timedelta(days=7)),
    ]

    with pytest.raises(InsufficientFeatureHistoryError, match="1 of 3"):
        served(periods)


def test_serving_refuses_a_recorded_lag_that_is_not_declared() -> None:
    """Rows recorded for every lag day, but nothing declared: no lag is known."""
    with pytest.raises(InsufficientFeatureHistoryError, match="3 of 3"):
        served([])


# --- the declaring service ----------------------------------------------------------------------

HOTEL = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeHotels:
    def __init__(self, timezone: str = "Europe/Athens") -> None:
        self.hotel = SimpleNamespace(id=7, public_id=HOTEL, timezone=timezone)

    def get_by_public_id(self, public_id: uuid.UUID) -> Any:
        return self.hotel if public_id == HOTEL else None


class FakePeriods:
    def __init__(self, spans: list[tuple[dt.date, dt.date]] | None = None) -> None:
        self.spans = list(spans or [])
        self.refuse_with: str | None = None

    def periods(self, hotel_id: int) -> list[tuple[dt.date, dt.date]]:
        assert hotel_id == 7
        return sorted(self.spans)

    def add(self, hotel_id: int, observed_from: dt.date, observed_to: dt.date) -> None:
        assert hotel_id == 7
        if self.refuse_with is not None:
            orig = SimpleNamespace(diag=SimpleNamespace(constraint_name=self.refuse_with))
            raise IntegrityError("INSERT", {}, orig)  # type: ignore[arg-type]
        self.spans.append((observed_from, observed_to))

    def remove(self, hotel_id: int, observed_from: dt.date, observed_to: dt.date) -> int:
        if (observed_from, observed_to) in self.spans:
            self.spans.remove((observed_from, observed_to))
            return 1
        return 0


def service(
    periods: FakePeriods | None = None,
    *,
    timezone: str = "Europe/Athens",
    now: dt.datetime = NOW,
) -> tuple[DemandObservationService, FakeSession, FakePeriods]:
    session, store = FakeSession(), periods or FakePeriods()
    built = DemandObservationService(
        session,  # type: ignore[arg-type]
        FakeHotels(timezone),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    return built, session, store


def test_a_declared_span_is_stored_and_committed() -> None:
    built, session, store = service()

    assert built.declare(HOTEL, dt.date(2026, 1, 1), dt.date(2026, 9, 30)) is True

    assert store.spans == [(dt.date(2026, 1, 1), dt.date(2026, 9, 30))]
    assert session.commits == 1
    assert built.spans(HOTEL) == [ObservationPeriod(dt.date(2026, 1, 1), dt.date(2026, 9, 30))]


def test_redeclaring_an_identical_span_changes_nothing() -> None:
    built, session, store = service(FakePeriods([(dt.date(2026, 1, 1), dt.date(2026, 1, 31))]))

    assert built.declare(HOTEL, dt.date(2026, 1, 1), dt.date(2026, 1, 31)) is False

    assert len(store.spans) == 1
    assert session.commits == 0


def test_a_reversed_span_is_refused_and_nothing_is_written() -> None:
    built, session, store = service()

    with pytest.raises(ValidationError, match="end before it starts"):
        built.declare(HOTEL, dt.date(2026, 2, 2), dt.date(2026, 2, 1))

    assert store.spans == [] and session.commits == 0


def test_the_last_declarable_day_is_the_hotels_yesterday() -> None:
    """12:00 UTC on 2 October is 15:00 in Athens: yesterday is 1 October, today is refused."""
    built, _, _ = service()

    assert built.declare(HOTEL, dt.date(2026, 9, 1), dt.date(2026, 10, 1)) is True
    with pytest.raises(ValidationError, match="today"):
        built.declare(HOTEL, dt.date(2026, 10, 2), dt.date(2026, 10, 2))
    with pytest.raises(ValidationError, match="today"):
        built.declare(HOTEL, dt.date(2026, 10, 3), dt.date(2026, 10, 9))


def test_today_is_the_hotels_own_calendar_date() -> None:
    """23:30 UTC on 1 October is already 2 October in Auckland, so 1 October has ended there --
    and has not ended in Los Angeles."""
    late = dt.datetime(2026, 10, 1, 23, 30, tzinfo=dt.UTC)
    auckland, _, _ = service(timezone="Pacific/Auckland", now=late)
    los_angeles, _, _ = service(timezone="America/Los_Angeles", now=late)

    assert auckland.declare(HOTEL, dt.date(2026, 10, 1), dt.date(2026, 10, 1)) is True
    with pytest.raises(ValidationError, match="today"):
        los_angeles.declare(HOTEL, dt.date(2026, 10, 1), dt.date(2026, 10, 1))


def test_an_unknown_stored_zone_falls_back_to_utc() -> None:
    built, _, _ = service(timezone="Not/AZone")

    assert built.declare(HOTEL, dt.date(2026, 10, 1), dt.date(2026, 10, 1)) is True
    with pytest.raises(ValidationError, match=r"today \(2026-10-02\)"):
        built.declare(HOTEL, dt.date(2026, 10, 2), dt.date(2026, 10, 2))


def test_an_overlapping_span_is_a_conflict_and_is_rolled_back() -> None:
    store = FakePeriods()
    store.refuse_with = OVERLAP_CONSTRAINT
    built, session, _ = service(store)

    with pytest.raises(ConflictError, match="shares a date"):
        built.declare(HOTEL, dt.date(2026, 1, 1), dt.date(2026, 1, 31))

    assert session.rollbacks == 1 and session.commits == 0


def test_any_other_integrity_failure_is_the_generic_conflict() -> None:
    store = FakePeriods()
    store.refuse_with = "some_other_constraint"
    built, session, _ = service(store)

    with pytest.raises(ConflictError) as raised:
        built.declare(HOTEL, dt.date(2026, 1, 1), dt.date(2026, 1, 31))

    assert "shares a date" not in raised.value.message
    assert session.rollbacks == 1


def test_withdrawing_removes_exactly_that_span() -> None:
    spans = [
        (dt.date(2026, 1, 1), dt.date(2026, 1, 31)),
        (dt.date(2026, 3, 1), dt.date(2026, 3, 9)),
    ]
    built, session, store = service(FakePeriods(spans))

    built.withdraw(HOTEL, dt.date(2026, 1, 1), dt.date(2026, 1, 31))

    assert store.spans == [(dt.date(2026, 3, 1), dt.date(2026, 3, 9))]
    assert session.commits == 1


def test_withdrawing_a_span_that_is_not_declared_is_not_found() -> None:
    built, session, _ = service(FakePeriods([(dt.date(2026, 1, 1), dt.date(2026, 1, 31))]))

    with pytest.raises(NotFoundError):
        built.withdraw(HOTEL, dt.date(2026, 1, 1), dt.date(2026, 1, 30))

    assert session.commits == 0


def test_an_unknown_hotel_is_not_found_for_every_command() -> None:
    built, _, _ = service()
    other = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    with pytest.raises(NotFoundError):
        built.spans(other)
    with pytest.raises(NotFoundError):
        built.declare(other, dt.date(2026, 1, 1), dt.date(2026, 1, 2))
    with pytest.raises(NotFoundError):
        built.withdraw(other, dt.date(2026, 1, 1), dt.date(2026, 1, 2))
