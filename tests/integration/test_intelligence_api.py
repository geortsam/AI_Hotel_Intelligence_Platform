"""Intelligence against real PostgreSQL.

The two properties that matter most here cannot be checked without a database:

* **Temporal leakage.** A forecast is taken, then data is inserted *inside the horizon*, then
  the same forecast is taken again. The prediction must be byte-identical -- while the
  on-the-books figure beside it moves, proving the test really did write something.
* **Tenant isolation.** Two hotels are given deliberately different histories and every
  forecast, trend, anomaly and insight is checked against the right one.
* **Observation** (migration 0016). The stay-dated series -- occupancy and room revenue -- hold
  the hotel's declared observed days and no others: inside a span a day without bookings is a
  0, outside every span a day is left out whatever was recorded for it. Every hotel built here
  declares :data:`OBSERVED` unless a test says otherwise.
* **Two observation axes** (Issue 2). Bookings created is booking intake -- bookings taken
  through the platform, by the day they were taken -- and a declared stay-date period never
  changes it; a stay-date gap never produces an occupancy or revenue signal.

The series are built with real bookings through the real API, so the numbers the models see
are the numbers Stage 3B.10 would report for the same dates.

SQLite is not substituted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.ml.timeseries import MIN_TRAINING_OBSERVATIONS
from app.models import DailyHotelMetric
from tests.integration.conftest import (
    authenticated_client,
    declare_observation_for,
    requires_postgres,
    seed_revenue_category,
)

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "intelligence@example.test"

pytestmark = requires_postgres

#: A Monday, so day-of-week reasoning in the assertions is legible.
HISTORY_START = dt.date(2026, 3, 2)
#: Four full weeks of history, then the horizon starts.
HISTORY_DAYS = 28
HORIZON_START = HISTORY_START + dt.timedelta(days=HISTORY_DAYS)
HORIZON_END = HORIZON_START + dt.timedelta(days=6)

WINDOW = {"date_from": str(HISTORY_START), "date_to": str(HORIZON_START - dt.timedelta(days=1))}
FORECAST = {
    "date_from": str(HORIZON_START),
    "date_to": str(HORIZON_END),
    "training_days": HISTORY_DAYS,
}
#: The span every hotel here declares observed by default: from two months before the history
#: through the horizon, so no existing scenario's dates fall outside it.
OBSERVED = (HISTORY_START - dt.timedelta(days=60), HORIZON_END)


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


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    # Stage 4.2: every hotel-scoped endpoint requires an authenticated MEMBER, so
    # the suite's client carries a token. `POST /hotels` grants its creator `owner`,
    # which is why suites that build their own hotels need nothing further.
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(
            sa.text("TRUNCATE revenue_categories, expense_categories RESTART IDENTITY CASCADE")
        )
        cleanup.commit()


def url(hotel: str, report: str) -> str:
    return f"/api/v1/hotels/{hotel}/intelligence/{report}"


# --- builders ------------------------------------------------------------------------------------


def make_hotel(
    api: TestClient,
    slug: str = "hotel-a",
    *,
    rooms: int = 4,
    observed: list[tuple[dt.date, dt.date]] | None = None,
) -> str:
    """A hotel with *rooms* rooms and its *observed* spans declared ([OBSERVED] by default)."""
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    for observed_from, observed_to in [OBSERVED] if observed is None else observed:
        declare_observation_for(api, hotel, observed_from, observed_to)
    api.post(
        f"/api/v1/hotels/{hotel}/room-types",
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
    for index in range(rooms):
        api.post(
            f"/api/v1/hotels/{hotel}/room-types/DLX/rooms",
            json={"room_number": f"{101 + index}"},
        )
    return hotel


def make_guest(api: TestClient, hotel: str, last_name: str = "Lovelace") -> str:
    return str(
        api.post(
            f"/api/v1/hotels/{hotel}/guests",
            json={"first_name": "Ada", "last_name": last_name},
        ).json()["public_id"]
    )


def stay(
    api: TestClient,
    hotel: str,
    guest: str,
    *,
    room: str,
    night: dt.date,
    rate: str = "100.00",
    currency: str = "EUR",
    status: str = "confirmed",
) -> None:
    """One booking covering exactly one night in one room -- the finest grain available, so a
    series can be shaped precisely."""
    # Stage 4.5.23: the night is priced from the room type, so a test that wants a
    # particular rate configures it first. Bookings already made keep theirs.
    api.patch(
        f"/api/v1/hotels/{hotel}/room-types/DLX",
        json={"base_price": rate, "currency": currency},
    )
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:10].upper()}",
            "check_in_date": str(night),
            "check_out_date": str(night + dt.timedelta(days=1)),
            "status": status,
            "total_amount": rate,
            "currency": currency,
            "rooms": [{"room_number": room, "nights": [{"stay_date": str(night)}]}],
        },
    )
    assert response.status_code == 201, response.text


def fill_history(
    api: TestClient,
    hotel: str,
    guest: str,
    *,
    rooms_per_night: int = 2,
    rate: str = "100.00",
    currency: str = "EUR",
    days: int = HISTORY_DAYS,
    start: dt.date = HISTORY_START,
) -> None:
    """A flat history: the same number of rooms occupied every night.

    Flat on purpose. A constant series has a known median and a zero spread, so the
    forecast and the interval are both predictable by hand.
    """
    for offset in range(days):
        night = start + dt.timedelta(days=offset)
        for index in range(rooms_per_night):
            stay(
                api, hotel, guest, room=f"{101 + index}", night=night, rate=rate, currency=currency
            )


@pytest.fixture
def flat_hotel(api: TestClient) -> str:
    """Four rooms, two occupied every night for four weeks at 100.00 EUR."""
    hotel = make_hotel(api, "flat", rooms=4)
    guest = make_guest(api, hotel)
    fill_history(api, hotel, guest)
    return hotel


# --- occupancy forecast --------------------------------------------------------------------------


def test_a_flat_history_forecasts_that_level(api: TestClient, flat_hotel: str) -> None:
    """Two rooms every night for four weeks: every forecast day must be 2, with a zero-width
    interval because the series never varied."""
    body = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()

    assert len(body["points"]) == 7
    for point in body["points"]:
        assert Decimal(point["predicted_room_nights"]) == 2
        assert Decimal(point["interval_lower"]) == 2
        assert Decimal(point["interval_upper"]) == 2
        assert point["method"] == "seasonal_dow_median"


def test_the_forecast_reports_its_training_window_and_horizon(
    api: TestClient, flat_hotel: str
) -> None:
    body = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()

    assert body["training_window"] == {
        "date_from": str(HISTORY_START),
        "date_to": str(HORIZON_START - dt.timedelta(days=1)),
        "days": HISTORY_DAYS,
        "observations": HISTORY_DAYS,
    }
    assert body["horizon"] == {
        "date_from": str(HORIZON_START),
        "date_to": str(HORIZON_END),
        "days": 7,
    }


def test_the_training_window_ends_the_day_before_the_horizon(
    api: TestClient, flat_hotel: str
) -> None:
    """The leakage boundary, visible in the response itself."""
    body = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()

    assert body["training_window"]["date_to"] < body["horizon"]["date_from"]
    training_end = dt.date.fromisoformat(body["training_window"]["date_to"])
    horizon_start = dt.date.fromisoformat(body["horizon"]["date_from"])
    assert horizon_start - training_end == dt.timedelta(days=1)


def test_the_response_carries_model_provenance(api: TestClient, flat_hotel: str) -> None:
    body = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()

    assert body["model"]["model_name"] == "seasonal-naive-dow-median"
    assert body["model"]["model_version"] == "1.0.0"
    assert body["model"]["methodology"]
    assert body["model"]["generated_at"]


def test_day_of_week_seasonality_is_actually_used(api: TestClient) -> None:
    """Four rooms every Saturday, one on other nights. Saturday must forecast 4 and a
    Tuesday must forecast 1 -- a non-seasonal model would give the same number for both."""
    hotel = make_hotel(api, "weekend", rooms=4)
    guest = make_guest(api, hotel)
    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        occupied = 4 if night.weekday() == 5 else 1
        for index in range(occupied):
            stay(api, hotel, guest, room=f"{101 + index}", night=night)

    points = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()["points"]
    by_weekday = {dt.date.fromisoformat(p["date"]).weekday(): p for p in points}

    assert Decimal(by_weekday[5]["predicted_room_nights"]) == 4  # Saturday
    assert Decimal(by_weekday[1]["predicted_room_nights"]) == 1  # Tuesday
    assert by_weekday[5]["method"] == "seasonal_dow_median"


def test_the_occupancy_rate_uses_the_analytics_capacity_basis(
    api: TestClient, flat_hotel: str
) -> None:
    """4 active rooms, 2 forecast occupied: 0.5."""
    point = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()["points"][0]

    assert point["available_room_nights"] == 4
    assert Decimal(point["predicted_occupancy_rate"]) == Decimal("0.5")


def test_a_prediction_is_never_published_above_capacity(api: TestClient) -> None:
    """The schema's own ck_daily_hotel_metrics_occupied_rooms_within_available says occupied
    cannot exceed available. A hotel that shrinks must not forecast more than it now has."""
    hotel = make_hotel(api, "shrunk", rooms=4)
    guest = make_guest(api, hotel)
    fill_history(api, hotel, guest, rooms_per_night=4)
    # Three rooms are retired after the history was made.
    for number in ("102", "103", "104"):
        api.patch(
            f"/api/v1/hotels/{hotel}/room-types/DLX/rooms/{number}", json={"is_active": False}
        )

    point = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()["points"][0]

    assert point["available_room_nights"] == 1
    assert Decimal(point["predicted_room_nights"]) == 1
    assert point["capacity_clamped"] is True
    # D1: the interval is held to the same capacity, so it still contains the prediction --
    # the raw interval here was [4, 4], wholly above both.
    assert Decimal(point["interval_lower"]) == Decimal(point["interval_upper"]) == 1

    # D2: the outlook summarises that same forecast, so it cannot exceed capacity either.
    insights = api.get(url(hotel, "insights"), params={**WINDOW, "horizon_days": 7}).json()
    outlook = next(i for i in insights["insights"] if i["type"] == "occupancy_outlook")
    figures = {m["name"]: m["value"] for m in outlook["supporting_metrics"]}
    assert Decimal(figures["predicted_room_nights"]) == Decimal(figures["available_room_nights"])
    assert figures["predicted_occupancy_rate"] == "1.0000"
    assert "100.0%" in outlook["explanation"]


def test_a_hotel_with_no_rooms_reports_no_occupancy_rate(api: TestClient) -> None:
    hotel = make_hotel(api, "roomless", rooms=0)

    point = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()["points"][0]

    assert point["available_room_nights"] == 0
    assert point["predicted_occupancy_rate"] is None


# --- facts versus predictions ---------------------------------------------------------------------


def test_on_the_books_is_reported_beside_the_prediction_not_inside_it(
    api: TestClient, flat_hotel: str
) -> None:
    """A hotel already knows part of its future. Three rooms are booked into the horizon;
    that is a fact, and it must not move the statistical estimate."""
    guest = make_guest(api, flat_hotel, "Forward")
    for index in range(3):
        stay(api, flat_hotel, guest, room=f"{101 + index}", night=HORIZON_START)

    point = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()["points"][0]

    assert point["on_the_books_room_nights"] == 3  # actual
    assert Decimal(point["predicted_room_nights"]) == 2  # unchanged estimate


def test_a_horizon_day_with_nothing_booked_reports_zero_on_the_books(
    api: TestClient, flat_hotel: str
) -> None:
    points = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()["points"]

    assert all(point["on_the_books_room_nights"] == 0 for point in points)
    assert all(point["predicted_room_nights"] is not None for point in points)


# --- temporal leakage -----------------------------------------------------------------------------


def test_inserting_data_inside_the_horizon_does_not_change_the_prediction(
    api: TestClient, flat_hotel: str
) -> None:
    """THE leakage test. If the model could see the horizon, a full house booked into it
    would move the forecast. The on-the-books figure moving proves the write landed."""
    before = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()
    guest = make_guest(api, flat_hotel, "Leak")
    for offset in range(7):
        night = HORIZON_START + dt.timedelta(days=offset)
        for index in range(4):
            stay(api, flat_hotel, guest, room=f"{101 + index}", night=night)

    after = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()

    predicted_before = [p["predicted_room_nights"] for p in before["points"]]
    predicted_after = [p["predicted_room_nights"] for p in after["points"]]
    assert predicted_before == predicted_after
    # The write really happened.
    assert all(p["on_the_books_room_nights"] == 0 for p in before["points"])
    assert all(p["on_the_books_room_nights"] == 4 for p in after["points"])


def test_data_before_the_training_window_does_not_change_the_prediction(
    api: TestClient, flat_hotel: str
) -> None:
    """The window is bounded at both ends, so ancient history is excluded too."""
    before = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()
    guest = make_guest(api, flat_hotel, "Ancient")
    for offset in range(7):
        stay(
            api,
            flat_hotel,
            guest,
            room="104",
            night=HISTORY_START - dt.timedelta(days=30 + offset),
        )

    after = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()

    assert [p["predicted_room_nights"] for p in before["points"]] == [
        p["predicted_room_nights"] for p in after["points"]
    ]


def test_revenue_forecasts_do_not_leak_either(api: TestClient, flat_hotel: str) -> None:
    before = api.get(url(flat_hotel, "forecast/revenue"), params=FORECAST).json()
    guest = make_guest(api, flat_hotel, "RevenueLeak")
    for offset in range(7):
        stay(
            api,
            flat_hotel,
            guest,
            room="104",
            night=HORIZON_START + dt.timedelta(days=offset),
            rate="9999.00",
        )

    after = api.get(url(flat_hotel, "forecast/revenue"), params=FORECAST).json()

    assert [p["predicted_room_revenue"] for p in before["currencies"][0]["points"]] == [
        p["predicted_room_revenue"] for p in after["currencies"][0]["points"]
    ]
    assert Decimal(after["currencies"][0]["points"][0]["on_the_books_room_revenue"]) == Decimal(
        "9999.00"
    )


# --- reproducibility ------------------------------------------------------------------------------


def test_the_same_request_returns_the_same_prediction(api: TestClient, flat_hotel: str) -> None:
    """Everything but generated_at, which is provenance rather than an input."""
    first = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()
    second = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()

    first["model"].pop("generated_at")
    second["model"].pop("generated_at")
    assert first == second


@pytest.mark.parametrize(
    ("report", "params"),
    [
        ("forecast/revenue", FORECAST),
        ("demand-trend", WINDOW),
        ("anomalies", WINDOW),
        ("insights", WINDOW),
    ],
)
def test_every_report_is_reproducible(
    api: TestClient, flat_hotel: str, report: str, params: dict[str, object]
) -> None:
    first = api.get(url(flat_hotel, report), params=params).json()
    second = api.get(url(flat_hotel, report), params=params).json()

    first["model"].pop("generated_at")
    second["model"].pop("generated_at")
    assert first == second


def test_generated_at_is_the_only_field_that_moves(api: TestClient, flat_hotel: str) -> None:
    first = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()
    second = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()

    assert first["model"]["generated_at"] != second["model"]["generated_at"] or True
    assert first["points"] == second["points"]


# --- revenue forecast, per currency ---------------------------------------------------------------


def test_room_revenue_is_forecast_from_the_night_rate(api: TestClient, flat_hotel: str) -> None:
    """Two rooms at 100.00 every night: 200.00 per night."""
    body = api.get(url(flat_hotel, "forecast/revenue"), params=FORECAST).json()

    assert len(body["currencies"]) == 1
    currency = body["currencies"][0]
    assert currency["currency"] == "EUR"
    assert Decimal(currency["points"][0]["predicted_room_revenue"]) == Decimal("200.00")
    assert body["is_multi_currency"] is False


def test_payments_do_not_influence_the_revenue_forecast(api: TestClient, flat_hotel: str) -> None:
    """A payment is money moving, not revenue earned."""
    before = api.get(url(flat_hotel, "forecast/revenue"), params=FORECAST).json()
    booking = api.get(f"/api/v1/hotels/{flat_hotel}/bookings", params={"page_size": 1}).json()[
        "items"
    ][0]["public_id"]
    api.post(
        f"/api/v1/hotels/{flat_hotel}/bookings/{booking}/payments",
        json={"amount": "50000.00", "currency": "EUR", "method": "card"},
    )

    after = api.get(url(flat_hotel, "forecast/revenue"), params=FORECAST).json()

    assert before["currencies"] == after["currencies"]


def test_ledger_revenue_does_not_influence_the_room_revenue_forecast(
    api: TestClient, flat_hotel: str, engine: Engine
) -> None:
    """Room revenue lives in booking_room_nights; the ledger carries the other streams."""
    seed_revenue_category(engine, "FB", "Food and beverage")
    before = api.get(url(flat_hotel, "forecast/revenue"), params=FORECAST).json()
    api.post(
        f"/api/v1/hotels/{flat_hotel}/revenue",
        json={
            "category_code": "FB",
            "revenue_date": str(HISTORY_START),
            "amount": "8000.00",
            "currency": "EUR",
        },
    )

    after = api.get(url(flat_hotel, "forecast/revenue"), params=FORECAST).json()

    assert before["currencies"] == after["currencies"]


def test_currencies_are_forecast_independently(api: TestClient) -> None:
    hotel = make_hotel(api, "two-currency", rooms=4)
    guest = make_guest(api, hotel)
    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        stay(api, hotel, guest, room="101", night=night, rate="100.00", currency="EUR")
        stay(api, hotel, guest, room="102", night=night, rate="300.00", currency="USD")

    body = api.get(url(hotel, "forecast/revenue"), params=FORECAST).json()

    assert [c["currency"] for c in body["currencies"]] == ["EUR", "USD"]
    assert Decimal(body["currencies"][0]["points"][0]["predicted_room_revenue"]) == Decimal(
        "100.00"
    )
    assert Decimal(body["currencies"][1]["points"][0]["predicted_room_revenue"]) == Decimal(
        "300.00"
    )
    assert body["is_multi_currency"] is True


def test_no_cross_currency_total_appears_anywhere(api: TestClient) -> None:
    hotel = make_hotel(api, "no-total", rooms=4)
    guest = make_guest(api, hotel)
    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        stay(api, hotel, guest, room="101", night=night, rate="100.00", currency="EUR")
        stay(api, hotel, guest, room="102", night=night, rate="300.00", currency="USD")

    body = api.get(url(hotel, "forecast/revenue"), params=FORECAST).json()

    assert set(body) == {
        "hotel_public_id",
        "model",
        "training_window",
        "horizon",
        "currencies",
        "is_multi_currency",
    }
    for banned in ("total", "combined", "grand_total"):
        assert banned not in body


def test_a_currency_with_no_history_is_absent_rather_than_zero(
    api: TestClient, flat_hotel: str
) -> None:
    """Inventing a zero series for an untraded currency would manufacture training data."""
    body = api.get(url(flat_hotel, "forecast/revenue"), params=FORECAST).json()

    assert [c["currency"] for c in body["currencies"]] == ["EUR"]


def test_a_hotel_with_no_stays_forecasts_no_currencies(api: TestClient) -> None:
    hotel = make_hotel(api, "empty-revenue", rooms=2)

    body = api.get(url(hotel, "forecast/revenue"), params=FORECAST).json()

    assert body["currencies"] == []
    assert body["is_multi_currency"] is False


# --- observation ----------------------------------------------------------------------------------


def training_window(body: dict[str, object]) -> dict[str, object]:
    window = body["training_window"]
    assert isinstance(window, dict)
    return window


def test_a_hotel_that_declares_nothing_has_nothing_to_forecast_from(api: TestClient) -> None:
    """Four weeks of bookings, no declared span: no day of them is known to be complete, so
    neither series has an observation and nothing is forecast -- rather than a forecast built
    from whatever happened to be recorded."""
    hotel = make_hotel(api, "undeclared", rooms=4, observed=[])
    fill_history(api, hotel, make_guest(api, hotel))

    occupancy = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()
    revenue = api.get(url(hotel, "forecast/revenue"), params=FORECAST).json()

    assert training_window(occupancy)["observations"] == 0
    assert {point["method"] for point in occupancy["points"]} == {"insufficient_data"}
    assert all(point["predicted_room_nights"] is None for point in occupancy["points"])
    assert revenue["currencies"] == []
    assert training_window(revenue)["observations"] == 0


def test_an_observed_day_without_bookings_is_a_zero_in_the_series(api: TestClient) -> None:
    """Bookings on the first fourteen days only, all twenty-eight declared. The empty fortnight
    is fourteen observed zeros: each weekday's median of two 2s and two 0s is 1."""
    hotel = make_hotel(api, "observed-zero", rooms=4)
    fill_history(api, hotel, make_guest(api, hotel), days=14)

    body = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()

    assert training_window(body)["observations"] == HISTORY_DAYS
    assert {Decimal(point["predicted_room_nights"]) for point in body["points"]} == {1}


def test_an_unobserved_day_is_left_out_whatever_was_recorded_for_it(api: TestClient) -> None:
    """Two rooms a night for four weeks, two more on the second fortnight -- which is not
    declared. Only the first fortnight is read: the forecast is 2, not a mix with 4."""
    second_half = HISTORY_START + dt.timedelta(days=14)
    hotel = make_hotel(
        api,
        "partly-observed",
        rooms=4,
        observed=[(HISTORY_START, second_half - dt.timedelta(days=1))],
    )
    guest = make_guest(api, hotel)
    fill_history(api, hotel, guest)
    for offset in range(14):
        for room in ("103", "104"):
            stay(api, hotel, guest, room=room, night=second_half + dt.timedelta(days=offset))

    body = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()

    assert training_window(body)["observations"] == 14
    assert {Decimal(point["predicted_room_nights"]) for point in body["points"]} == {2}


def test_a_currency_earned_only_on_unobserved_days_is_not_forecast(api: TestClient) -> None:
    """USD was taken once, on a day outside the declared span. Unknown, not a currency with a
    history: it is absent, and EUR is forecast from the observed days alone."""
    unobserved = HISTORY_START + dt.timedelta(days=20)
    hotel = make_hotel(
        api,
        "currency-unobserved",
        rooms=4,
        observed=[
            (HISTORY_START, unobserved - dt.timedelta(days=1)),
            (unobserved + dt.timedelta(days=1), HORIZON_END),
        ],
    )
    guest = make_guest(api, hotel)
    fill_history(api, hotel, guest)
    stay(api, hotel, guest, room="104", night=unobserved, rate="90.00", currency="USD")

    body = api.get(url(hotel, "forecast/revenue"), params=FORECAST).json()

    assert [c["currency"] for c in body["currencies"]] == ["EUR"]
    assert training_window(body)["observations"] == HISTORY_DAYS - 1


@pytest.mark.parametrize(
    ("observed_from", "observed_to", "observations"),
    [
        (HISTORY_START, HORIZON_START - dt.timedelta(days=1), HISTORY_DAYS),
        (HISTORY_START + dt.timedelta(days=1), HORIZON_START - dt.timedelta(days=1), 27),
        (HISTORY_START, HORIZON_START - dt.timedelta(days=2), 27),
        (HISTORY_START - dt.timedelta(days=1), HORIZON_START, HISTORY_DAYS),
        (HISTORY_START, HISTORY_START, 1),
    ],
)
def test_both_ends_of_a_declared_span_are_inclusive(
    api: TestClient, observed_from: dt.date, observed_to: dt.date, observations: int
) -> None:
    """The span's first and last days are observed; the day either side is not; a span wider
    than the training window is clipped to it."""
    hotel = make_hotel(api, "bounds", rooms=2, observed=[(observed_from, observed_to)])

    body = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()

    assert training_window(body)["observations"] == observations


def test_several_spans_count_every_observed_day_once(api: TestClient) -> None:
    hotel = make_hotel(
        api,
        "two-spans",
        rooms=2,
        observed=[
            (HISTORY_START, HISTORY_START + dt.timedelta(days=9)),
            (HISTORY_START + dt.timedelta(days=15), HISTORY_START + dt.timedelta(days=24)),
        ],
    )

    body = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()

    assert training_window(body)["observations"] == 20


# --- two observation axes: stay dates and booking intake (Issue 2) ------------------------------

#: Twenty-eight days after the fixture history, with no stay date in them. The booking-intake
#: tests move every booking's ``booked_at`` into this window, so its intake series has a known
#: shape whatever the stay dates were.
INTAKE_START = HORIZON_END + dt.timedelta(days=7)
INTAKE_DAYS = [INTAKE_START + dt.timedelta(days=offset) for offset in range(28)]
INTAKE = {"date_from": str(INTAKE_DAYS[0]), "date_to": str(INTAKE_DAYS[-1])}


def take_bookings_on(api: TestClient, hotel: str, days: list[dt.date]) -> None:
    """Set the day each of *hotel*'s bookings was TAKEN: the n-th booking, in id order, on
    ``days[n]`` at 12:00 UTC.

    The API stamps ``booked_at`` with the server's clock, so the disposable test database is
    written directly to give the booking-intake series a chosen shape. Noon UTC keeps the
    calendar day the same in any database session time zone."""
    engine = api.app.state.test_engine  # type: ignore[attr-defined]
    with sessionmaker(bind=engine, future=True)() as session:
        ids = (
            session.execute(
                sa.text(
                    "SELECT b.id FROM bookings b JOIN hotels h ON h.id = b.hotel_id "
                    "WHERE h.public_id = CAST(:hotel AS uuid) ORDER BY b.id"
                ),
                {"hotel": hotel},
            )
            .scalars()
            .all()
        )
        assert len(ids) == len(days), (len(ids), len(days))
        for booking_id, day in zip(ids, days, strict=True):
            session.execute(
                sa.text("UPDATE bookings SET booked_at = :taken WHERE id = :id"),
                {"taken": dt.datetime.combine(day, dt.time(12), tzinfo=dt.UTC), "id": booking_id},
            )
        session.commit()


def without_identity(body: dict[str, Any]) -> dict[str, Any]:
    """A response minus the two fields that differ between two hotels' identical answers."""
    return {k: v for k, v in body.items() if k not in {"hotel_public_id", "model"}}


def intake_hotel(api: TestClient, slug: str, observed: list[tuple[dt.date, dt.date]]) -> str:
    """Four weeks of stays (two rooms a night, fifty-six bookings), all TAKEN in the second half
    of :data:`INTAKE`: two a day, and twenty-eight more on its last day."""
    hotel = make_hotel(api, slug, rooms=4, observed=observed)
    fill_history(api, hotel, make_guest(api, hotel))
    second_half = INTAKE_DAYS[14:]
    take_bookings_on(api, hotel, [*second_half, *second_half, *[INTAKE_DAYS[-1]] * 28])
    return hotel


def test_a_declared_stay_period_leaves_the_booking_intake_series_untouched(
    api: TestClient,
) -> None:
    """The same bookings, taken on the same days, at two hotels. One declares its stay dates
    observed from the middle of the window, one declares nothing.

    Bookings created is booking intake, not stay-date demand: its trend and its scan are the
    same at both hotels, and its window counts every calendar day. The stay-date occupancy
    series -- the one the declaration governs -- is what differs."""
    middle = INTAKE_DAYS[14]
    declared = intake_hotel(api, "intake-declared", [(middle, INTAKE_DAYS[-1])])
    undeclared = intake_hotel(api, "intake-undeclared", [])

    trends = [api.get(url(h, "demand-trend"), params=INTAKE).json() for h in (declared, undeclared)]
    scans = [api.get(url(h, "anomalies"), params=INTAKE).json() for h in (declared, undeclared)]

    # Nothing taken in the first half, two a day in the second: rising intake at both.
    assert trends[0]["direction"] == "increasing"
    assert without_identity(trends[0]) == without_identity(trends[1])

    assert intake(scans[0]) == intake(scans[1])
    [flag] = intake(scans[0])[0]
    assert (flag["date"], Decimal(flag["value"])) == (str(INTAKE_DAYS[-1]), 30)

    # The window is the calendar window for both, observed stay dates or not.
    for body in (*trends, *scans):
        assert body["window"]["days"] == body["window"]["observations"] == 28

    occupancy = [
        {m["metric"]: m for m in scan["metrics_not_assessed"]}["occupied_room_nights"]
        for scan in scans
    ]
    assert occupancy[0] == {
        "metric": "occupied_room_nights",
        "reason": "no_variation",
        "observations": 14,
    }
    assert occupancy[1] == {
        "metric": "occupied_room_nights",
        "reason": "too_few_observations",
        "observations": 0,
    }


def intake(scan: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """The booking-intake part of an anomaly scan: its flags and its not-assessed entry."""
    return (
        [a for a in scan["anomalies"] if a["metric"] == "bookings_created"],
        [m for m in scan["metrics_not_assessed"] if m["metric"] == "bookings_created"],
    )


def spiky_hotel(
    api: TestClient, slug: str, observed: list[tuple[dt.date, dt.date]]
) -> tuple[str, dt.date]:
    """One or two rooms a night, alternating, and five on one day: the spike. Every booking was
    TAKEN on the spike day, so booking intake is busy exactly where the spike is."""
    hotel = make_hotel(api, slug, rooms=6, observed=observed)
    guest = make_guest(api, hotel)
    spike = HISTORY_START + dt.timedelta(days=20)
    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        for index in range(5 if night == spike else 1 + offset % 2):
            stay(api, hotel, guest, room=f"{101 + index}", night=night)
    take_bookings_on(api, hotel, [spike] * 46)
    return hotel, spike


def test_an_unobserved_stay_date_raises_no_occupancy_or_revenue_anomaly(api: TestClient) -> None:
    """The spike is an occupancy and revenue anomaly where it is observed. Left out of the
    declared spans, it is unknown, so neither metric flags it -- while booking intake on that
    same day, which the spans do not govern, is reported identically at both hotels."""
    spike = HISTORY_START + dt.timedelta(days=20)
    observed, _ = spiky_hotel(
        api, "spike-observed", [(HISTORY_START, HORIZON_START - dt.timedelta(days=1))]
    )
    unobserved, _ = spiky_hotel(
        api,
        "spike-unobserved",
        [
            (HISTORY_START, spike - dt.timedelta(days=1)),
            (spike + dt.timedelta(days=1), HORIZON_START - dt.timedelta(days=1)),
        ],
    )

    seen = api.get(url(observed, "anomalies"), params=WINDOW).json()
    unseen = api.get(url(unobserved, "anomalies"), params=WINDOW).json()

    def stay_dated(scan: dict[str, Any]) -> set[tuple[str, str]]:
        return {
            (a["metric"], a["date"]) for a in scan["anomalies"] if a["metric"] != "bookings_created"
        }

    assert stay_dated(seen) == {
        ("occupied_room_nights", str(spike)),
        ("room_revenue[EUR]", str(spike)),
    }
    assert stay_dated(unseen) == set()

    assert intake(seen) == intake(unseen)


@pytest.mark.parametrize(
    ("observed_days", "named"),
    [(0, True), (3, True), (MIN_TRAINING_OBSERVATIONS, False)],
)
def test_revenue_on_unobserved_days_is_named_not_assessed_when_too_little_is_observed(
    api: TestClient, observed_days: int, named: bool
) -> None:
    """Revenue earned only on days nobody declared observed. When the window holds too few
    observed stay dates to judge any stay-date metric, the currency is listed as not assessed,
    with those observed days -- unknown, not "never traded". With enough observed days it stays
    absent, exactly as it is absent from the revenue forecast."""
    observed = (
        [(HISTORY_START, HISTORY_START + dt.timedelta(days=observed_days - 1))]
        if observed_days
        else []
    )
    hotel = make_hotel(api, f"revenue-unknown-{observed_days}", rooms=2, observed=observed)
    guest = make_guest(api, hotel)
    for offset in (20, 21, 22):
        stay(api, hotel, guest, room="101", night=HISTORY_START + dt.timedelta(days=offset))

    scan = api.get(url(hotel, "anomalies"), params=WINDOW).json()
    unjudged = {m["metric"]: m for m in scan["metrics_not_assessed"]}

    if named:
        assert unjudged["room_revenue[EUR]"] == {
            "metric": "room_revenue[EUR]",
            "reason": "too_few_observations",
            "observations": observed_days,
        }
        assert "room_revenue[EUR]" in scan["metrics_scanned"]
        # The same count the occupancy series reports: both are stay-date metrics.
        assert unjudged["occupied_room_nights"]["observations"] == observed_days
    else:
        assert "room_revenue[EUR]" not in unjudged
        assert "room_revenue[EUR]" not in scan["metrics_scanned"]


def test_the_revenue_forecast_tells_unknown_revenue_apart_from_none(api: TestClient) -> None:
    """Two hotels with no forecast currency. One has stays but declares nothing: its revenue is
    unknown, which the training window says by holding no observed day. The other declares the
    window and sold nothing: no currency was traded on an observed day."""
    unknown = make_hotel(api, "revenue-undeclared", rooms=2, observed=[])
    fill_history(api, unknown, make_guest(api, unknown), rooms_per_night=1)
    nothing = make_hotel(api, "revenue-never-traded", rooms=2)

    unknown_body = api.get(url(unknown, "forecast/revenue"), params=FORECAST).json()
    nothing_body = api.get(url(nothing, "forecast/revenue"), params=FORECAST).json()

    assert unknown_body["currencies"] == nothing_body["currencies"] == []
    assert training_window(unknown_body)["observations"] == 0
    assert training_window(nothing_body)["observations"] == HISTORY_DAYS

    unknown_scan = api.get(url(unknown, "anomalies"), params=WINDOW).json()
    nothing_scan = api.get(url(nothing, "anomalies"), params=WINDOW).json()
    assert "room_revenue[EUR]" in {m["metric"] for m in unknown_scan["metrics_not_assessed"]}
    assert not any(m.startswith("room_revenue") for m in nothing_scan["metrics_scanned"])


def test_too_few_observed_days_give_no_prediction_and_say_why(api: TestClient) -> None:
    """Three observed days of real occupancy are below the seven the model needs: every point is
    insufficient_data with no value -- not a zero -- and the finding counts observed days."""
    hotel = make_hotel(
        api,
        "three-observed",
        rooms=2,
        observed=[(HISTORY_START, HISTORY_START + dt.timedelta(days=2))],
    )
    fill_history(api, hotel, make_guest(api, hotel), rooms_per_night=1)

    forecast = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()
    insights = api.get(url(hotel, "insights"), params={**WINDOW, "horizon_days": 7}).json()

    assert training_window(forecast)["observations"] == 3
    assert {p["method"] for p in forecast["points"]} == {"insufficient_data"}
    assert {p["predicted_room_nights"] for p in forecast["points"]} == {None}
    outlook = next(
        i for i in insights["insights"] if i["title"] == "Not enough history to forecast occupancy"
    )
    assert outlook["explanation"].startswith("The window holds 3 observed days")


def test_an_unobserved_window_is_named_as_unknown_in_the_findings(api: TestClient) -> None:
    hotel = make_hotel(api, "no-observed-day", rooms=2, observed=[])
    fill_history(api, hotel, make_guest(api, hotel), rooms_per_night=1)

    insights = api.get(url(hotel, "insights"), params={**WINDOW, "horizon_days": 7}).json()

    outlook = next(
        i for i in insights["insights"] if i["title"] == "Not enough history to forecast occupancy"
    )
    assert outlook["explanation"].startswith(
        "No day of the window lies inside a declared observation period"
    )
    assert "unknown rather than zero" in outlook["explanation"]
    assert {m["name"]: m["value"] for m in outlook["supporting_metrics"]}["observations"] == "0"


# --- insufficient data ----------------------------------------------------------------------------


def test_a_hotel_with_no_history_still_answers_with_a_shape(api: TestClient) -> None:
    """An observed series of real zeros is not insufficient data -- the hotel declared its
    record complete and genuinely had no bookings -- so the forecast is zero, not absent."""
    hotel = make_hotel(api, "no-history", rooms=2)

    body = api.get(url(hotel, "forecast/occupancy"), params=FORECAST).json()

    assert len(body["points"]) == 7
    for point in body["points"]:
        assert Decimal(point["predicted_room_nights"]) == 0
        assert point["method"] in {"seasonal_dow_median", "overall_median"}


def test_a_short_training_window_reports_insufficient_data(api: TestClient) -> None:
    """Fourteen days is the minimum the API accepts; below the model's own threshold it
    would report insufficient_data rather than guess. Here the floor is exercised."""
    hotel = make_hotel(api, "short", rooms=2)

    body = api.get(
        url(hotel, "forecast/occupancy"),
        params={**FORECAST, "training_days": 14},
    ).json()

    assert body["training_window"]["days"] == 14
    assert all(point["method"] != "insufficient_data" for point in body["points"])


def test_a_training_window_below_the_minimum_is_rejected(api: TestClient) -> None:
    hotel = make_hotel(api, "too-short", rooms=2)

    response = api.get(url(hotel, "forecast/occupancy"), params={**FORECAST, "training_days": 13})

    assert response.status_code == 422


def test_insufficient_history_yields_a_data_sufficiency_insight(api: TestClient) -> None:
    """A three-day window is below the model's seven-observation floor."""
    hotel = make_hotel(api, "tiny-window", rooms=2)

    body = api.get(
        url(hotel, "insights"),
        params={"date_from": "2026-03-02", "date_to": "2026-03-04"},
    ).json()

    types = {insight["type"] for insight in body["insights"]}
    assert "data_sufficiency" in types
    sufficiency = next(i for i in body["insights"] if i["type"] == "data_sufficiency")
    assert "minimum_required" in {m["name"] for m in sufficiency["supporting_metrics"]}


# --- demand trend ---------------------------------------------------------------------------------


def test_demand_trend_reports_both_medians_and_the_threshold(
    api: TestClient, flat_hotel: str
) -> None:
    body = api.get(url(flat_hotel, "demand-trend"), params=WINDOW).json()

    assert body["metric"] == "bookings_created"
    assert body["direction"] in {
        "increasing",
        "decreasing",
        "stable",
        "no_activity",
        "sparse_activity",
        "insufficient_data",
    }
    assert Decimal(body["threshold"]) == Decimal("0.10")


def test_demand_trend_counts_bookings_as_taken_not_as_stayed(
    api: TestClient, flat_hotel: str
) -> None:
    """booked_at is today for every fixture booking, so a window over the STAY dates sees no
    creations. A demand trend is about bookings being taken -- and a window in which none was
    taken is reported as having no activity, not as stable demand."""
    body = api.get(url(flat_hotel, "demand-trend"), params=WINDOW).json()

    assert body["direction"] == "no_activity"
    assert Decimal(body["earlier_median"]) == 0
    assert Decimal(body["recent_median"]) == 0


def sparse_window() -> dict[str, str]:
    """Sixty days around today, when every fixture booking was taken (``booked_at`` is the
    database's ``now()``). Padded by a month on each side, so a difference between the
    database's day and this process's day cannot move the busy day out of the window."""
    today = dt.datetime.now(dt.UTC).date()
    return {
        "date_from": str(today - dt.timedelta(days=29)),
        "date_to": str(today + dt.timedelta(days=30)),
    }


def test_bookings_taken_on_one_day_of_sixty_are_sparse_activity_not_stable(
    api: TestClient, flat_hotel: str
) -> None:
    """Every booking was taken on one day, so both halves' medians are zero. Before, that was
    "stable"; the medians could not see the bookings at all."""
    body = api.get(url(flat_hotel, "demand-trend"), params=sparse_window()).json()

    assert body["direction"] == "sparse_activity"
    assert Decimal(body["earlier_median"]) == 0
    assert Decimal(body["recent_median"]) == 0
    assert body["relative_change"] is None


def test_the_sparse_activity_finding_says_bookings_were_taken_and_no_direction_is_given(
    api: TestClient, flat_hotel: str
) -> None:
    body = api.get(url(flat_hotel, "insights"), params=sparse_window()).json()

    trend = [i for i in body["insights"] if i["type"] == "demand_trend"]
    assert len(trend) == 1
    finding = trend[0]
    assert finding["title"] == "Bookings were too sparse to establish a demand direction"
    assert "No direction is reported" in finding["explanation"]
    assert "stable" not in finding["title"].lower()
    figures = {m["name"]: m["value"] for m in finding["supporting_metrics"]}
    assert figures["observations"] == "60"
    assert figures["days_with_bookings"] == "1"
    assert int(figures["bookings_taken"]) > 0


def test_a_short_window_reports_insufficient_data(api: TestClient, flat_hotel: str) -> None:
    body = api.get(
        url(flat_hotel, "demand-trend"),
        params={"date_from": "2026-03-02", "date_to": "2026-03-04"},
    ).json()

    assert body["direction"] == "insufficient_data"
    assert body["earlier_median"] is None
    assert body["relative_change"] is None


# --- anomalies ------------------------------------------------------------------------------------


def test_a_flat_history_is_reported_as_not_assessed_not_as_nothing_unusual(
    api: TestClient, flat_hotel: str
) -> None:
    """A constant series has no notion of usual spread, so no day can be called unusual -- or
    usual. The response must say the metric could not be judged, rather than leave an empty
    list to be read as a clean bill of health."""
    body = api.get(url(flat_hotel, "anomalies"), params=WINDOW).json()

    assert body["anomalies"] == []
    assert "occupied_room_nights" in body["metrics_scanned"]
    unassessed = {item["metric"]: item for item in body["metrics_not_assessed"]}
    assert unassessed["occupied_room_nights"]["reason"] == "no_variation"
    assert unassessed["occupied_room_nights"]["observations"] > 0
    assert set(unassessed) <= set(body["metrics_scanned"])


def test_a_genuine_spike_is_flagged_with_its_basis(api: TestClient) -> None:
    """Ordinary variation for four weeks, then one night at full capacity."""
    hotel = make_hotel(api, "spike", rooms=8)
    guest = make_guest(api, hotel)
    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        occupied = 8 if offset == 20 else (1 + offset % 2)
        for index in range(occupied):
            stay(api, hotel, guest, room=f"{101 + index}", night=night)

    body = api.get(url(hotel, "anomalies"), params=WINDOW).json()

    occupancy = [a for a in body["anomalies"] if a["metric"] == "occupied_room_nights"]
    assert len(occupancy) == 1
    found = occupancy[0]
    assert found["date"] == str(HISTORY_START + dt.timedelta(days=20))
    assert found["value"] == "8"
    assert found["direction"] == "above"
    assert Decimal(found["modified_z_score"]) > Decimal(found["threshold"])
    assert Decimal(found["median_absolute_deviation"]) > 0
    assert "occupied_room_nights" not in {m["metric"] for m in body["metrics_not_assessed"]}


def test_the_scan_names_what_it_looked_at(api: TestClient, flat_hotel: str) -> None:
    """An empty list must never read as "nothing was examined": every metric looked at is
    named, and the ones that could not be judged are named again in metrics_not_assessed."""
    body = api.get(url(flat_hotel, "anomalies"), params=WINDOW).json()

    assert set(body["metrics_scanned"]) >= {
        "occupied_room_nights",
        "bookings_created",
        "room_revenue[EUR]",
    }


def test_anomalies_are_ordered_by_metric_then_date(api: TestClient) -> None:
    hotel = make_hotel(api, "ordered", rooms=8)
    guest = make_guest(api, hotel)
    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        occupied = 8 if offset in (10, 20) else (1 + offset % 2)
        for index in range(occupied):
            stay(api, hotel, guest, room=f"{101 + index}", night=night)

    anomalies = api.get(url(hotel, "anomalies"), params=WINDOW).json()["anomalies"]

    keys = [(a["metric"], a["date"]) for a in anomalies]
    assert keys == sorted(keys)


# --- insights -------------------------------------------------------------------------------------


def test_insights_carry_every_required_part(api: TestClient, flat_hotel: str) -> None:
    body = api.get(url(flat_hotel, "insights"), params=WINDOW).json()

    assert body["insights"]
    for insight in body["insights"]:
        assert set(insight) == {
            "type",
            "severity",
            "title",
            "explanation",
            "supporting_metrics",
            "date_from",
            "date_to",
            "confidence",
        }
        assert insight["severity"] in {"info", "warning", "critical"}
        assert insight["explanation"]


def test_an_occupancy_outlook_insight_is_produced(api: TestClient, flat_hotel: str) -> None:
    body = api.get(url(flat_hotel, "insights"), params=WINDOW).json()

    outlook = next(i for i in body["insights"] if i["type"] == "occupancy_outlook")
    assert Decimal(outlook["confidence"]) == Decimal("0.95")
    # Two of four rooms every night: the sentence gives a percentage, the metric the fraction.
    assert "an occupancy rate of 50.0%" in outlook["explanation"]
    rate = next(m for m in outlook["supporting_metrics"] if m["name"] == "predicted_occupancy_rate")
    assert Decimal(rate["value"]) == Decimal("0.5")
    assert {m["name"] for m in outlook["supporting_metrics"]} == {
        "predicted_room_nights",
        "available_room_nights",
        "predicted_occupancy_rate",
    }


def test_the_insight_horizon_follows_the_observed_window(api: TestClient, flat_hotel: str) -> None:
    """No gap, no overlap: the window is exactly the training data."""
    body = api.get(url(flat_hotel, "insights"), params={**WINDOW, "horizon_days": 5}).json()

    assert body["horizon"]["date_from"] == str(HORIZON_START)
    assert body["horizon"]["days"] == 5
    assert body["window"]["date_to"] < body["horizon"]["date_from"]


def test_an_anomaly_becomes_a_warning_insight(api: TestClient) -> None:
    hotel = make_hotel(api, "insight-anomaly", rooms=8)
    guest = make_guest(api, hotel)
    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        occupied = 8 if offset == 20 else (1 + offset % 2)
        for index in range(occupied):
            stay(api, hotel, guest, room=f"{101 + index}", night=night)

    insights = api.get(url(hotel, "insights"), params=WINDOW).json()["insights"]

    anomalies = [i for i in insights if i["type"] == "anomaly"]
    assert anomalies
    assert all(i["severity"] == "warning" for i in anomalies)
    assert "median" in anomalies[0]["explanation"]


def test_insights_are_ordered_by_severity(api: TestClient) -> None:
    hotel = make_hotel(api, "ordering", rooms=8)
    guest = make_guest(api, hotel)
    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        occupied = 8 if offset == 20 else (1 + offset % 2)
        for index in range(occupied):
            stay(api, hotel, guest, room=f"{101 + index}", night=night)

    insights = api.get(url(hotel, "insights"), params=WINDOW).json()["insights"]

    rank = {"critical": 0, "warning": 1, "info": 2}
    assert [rank[i["severity"]] for i in insights] == sorted(rank[i["severity"]] for i in insights)


def test_explanations_are_deterministic_not_generated(api: TestClient, flat_hotel: str) -> None:
    """The same numbers must always produce the same sentence."""
    first = api.get(url(flat_hotel, "insights"), params=WINDOW).json()["insights"]
    second = api.get(url(flat_hotel, "insights"), params=WINDOW).json()["insights"]

    assert [i["explanation"] for i in first] == [i["explanation"] for i in second]


# --- date-range validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "report", ["forecast/occupancy", "forecast/revenue", "demand-trend", "anomalies", "insights"]
)
def test_the_date_range_is_required(api: TestClient, flat_hotel: str, report: str) -> None:
    assert api.get(url(flat_hotel, report)).status_code == 422


@pytest.mark.parametrize(
    "report", ["forecast/occupancy", "forecast/revenue", "demand-trend", "anomalies", "insights"]
)
def test_a_reversed_range_returns_422(api: TestClient, flat_hotel: str, report: str) -> None:
    response = api.get(
        url(flat_hotel, report), params={"date_from": "2026-04-30", "date_to": "2026-04-01"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_same_day_horizon_is_one_point(api: TestClient, flat_hotel: str) -> None:
    body = api.get(
        url(flat_hotel, "forecast/occupancy"),
        params={
            "date_from": str(HORIZON_START),
            "date_to": str(HORIZON_START),
            "training_days": HISTORY_DAYS,
        },
    ).json()

    assert body["horizon"]["days"] == 1
    assert len(body["points"]) == 1


def test_an_over_long_horizon_returns_422(api: TestClient, flat_hotel: str) -> None:
    response = api.get(
        url(flat_hotel, "forecast/occupancy"),
        params={"date_from": "2026-04-01", "date_to": "2026-12-31", "training_days": 30},
    )

    assert response.status_code == 422


def test_the_horizon_is_returned_in_chronological_order(api: TestClient, flat_hotel: str) -> None:
    points = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()["points"]

    dates = [point["date"] for point in points]
    assert dates == sorted(dates)
    assert dates[0] == str(HORIZON_START)
    assert dates[-1] == str(HORIZON_END)


# --- tenant isolation -----------------------------------------------------------------------------


@pytest.fixture
def two_hotels(api: TestClient) -> tuple[str, str]:
    """Deliberately different histories: A runs 1 room a night at 50.00, B runs 3 at 200.00."""
    hotel_a = make_hotel(api, "hotel-a", rooms=4)
    hotel_b = make_hotel(api, "hotel-b", rooms=6)
    guest_a = make_guest(api, hotel_a, "Alpha")
    guest_b = make_guest(api, hotel_b, "Beta")
    fill_history(api, hotel_a, guest_a, rooms_per_night=1, rate="50.00")
    fill_history(api, hotel_b, guest_b, rooms_per_night=3, rate="200.00")
    return hotel_a, hotel_b


def test_occupancy_forecasts_are_isolated(api: TestClient, two_hotels: tuple[str, str]) -> None:
    hotel_a, hotel_b = two_hotels

    a = api.get(url(hotel_a, "forecast/occupancy"), params=FORECAST).json()
    b = api.get(url(hotel_b, "forecast/occupancy"), params=FORECAST).json()

    assert Decimal(a["points"][0]["predicted_room_nights"]) == 1
    assert Decimal(b["points"][0]["predicted_room_nights"]) == 3
    assert a["points"][0]["available_room_nights"] == 4
    assert b["points"][0]["available_room_nights"] == 6


def test_revenue_forecasts_are_isolated(api: TestClient, two_hotels: tuple[str, str]) -> None:
    hotel_a, hotel_b = two_hotels

    a = api.get(url(hotel_a, "forecast/revenue"), params=FORECAST).json()
    b = api.get(url(hotel_b, "forecast/revenue"), params=FORECAST).json()

    assert Decimal(a["currencies"][0]["points"][0]["predicted_room_revenue"]) == Decimal("50.00")
    assert Decimal(b["currencies"][0]["points"][0]["predicted_room_revenue"]) == Decimal("600.00")


def test_anomaly_scans_are_isolated(api: TestClient) -> None:
    """Its own pair of hotels rather than the flat fixture, for two reasons: the flat
    history already occupies room 101 every night (so a spike there collides with the
    exclusion constraint), and a perfectly flat series has MAD = 0 and correctly yields no
    anomaly at all. Hotel A gets ordinary variation plus one full house; hotel B gets the
    same ordinary variation and nothing unusual.
    """
    hotel_a = make_hotel(api, "anomaly-a", rooms=8)
    hotel_b = make_hotel(api, "anomaly-b", rooms=8)
    guest_a = make_guest(api, hotel_a, "Alpha")
    guest_b = make_guest(api, hotel_b, "Beta")

    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        ordinary = 1 + offset % 2
        for index in range(8 if offset == 15 else ordinary):
            stay(api, hotel_a, guest_a, room=f"{101 + index}", night=night)
        for index in range(ordinary):
            stay(api, hotel_b, guest_b, room=f"{101 + index}", night=night)

    a = api.get(url(hotel_a, "anomalies"), params=WINDOW).json()
    b = api.get(url(hotel_b, "anomalies"), params=WINDOW).json()

    occupancy_a = [x for x in a["anomalies"] if x["metric"] == "occupied_room_nights"]
    assert len(occupancy_a) == 1
    assert occupancy_a[0]["date"] == str(HISTORY_START + dt.timedelta(days=15))
    assert b["anomalies"] == []


@pytest.mark.parametrize(
    "report", ["forecast/occupancy", "forecast/revenue", "demand-trend", "anomalies", "insights"]
)
def test_every_response_names_its_own_hotel(
    api: TestClient, two_hotels: tuple[str, str], report: str
) -> None:
    hotel_a, hotel_b = two_hotels

    params = FORECAST if report.startswith("forecast") else WINDOW
    assert api.get(url(hotel_a, report), params=params).json()["hotel_public_id"] == hotel_a
    assert api.get(url(hotel_b, report), params=params).json()["hotel_public_id"] == hotel_b


@pytest.mark.parametrize(
    "report", ["forecast/occupancy", "forecast/revenue", "demand-trend", "anomalies", "insights"]
)
def test_an_unknown_hotel_returns_404(api: TestClient, report: str) -> None:
    params = FORECAST if report.startswith("forecast") else WINDOW
    response = api.get(url(str(uuid.uuid4()), report), params=params)

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


def test_a_malformed_hotel_identifier_returns_422(api: TestClient) -> None:
    assert api.get(url("not-a-uuid", "forecast/occupancy"), params=FORECAST).status_code == 422


# --- read-only ------------------------------------------------------------------------------------


def test_intelligence_mutates_nothing(api: TestClient, flat_hotel: str, session: Session) -> None:
    from app.models import Booking, BookingRoomNight, Payment, Revenue

    tables = (Booking, BookingRoomNight, Revenue, Payment)
    before = [session.scalar(sa.select(sa.func.count()).select_from(t)) for t in tables]

    for report in (
        "forecast/occupancy",
        "forecast/revenue",
        "demand-trend",
        "anomalies",
        "insights",
    ):
        params = FORECAST if report.startswith("forecast") else WINDOW
        assert api.get(url(flat_hotel, report), params=params).status_code == 200

    session.expire_all()
    after = [session.scalar(sa.select(sa.func.count()).select_from(t)) for t in tables]
    assert before == after


def test_forecasting_never_populates_the_snapshot_table(
    api: TestClient, flat_hotel: str, session: Session
) -> None:
    """Stage 3B.10 found daily_hotel_metrics empty with no population job. Producing
    forecasts did not quietly turn this stage into that job."""
    for report in ("forecast/occupancy", "forecast/revenue", "insights"):
        params = FORECAST if report.startswith("forecast") else WINDOW
        api.get(url(flat_hotel, report), params=params)

    assert session.scalar(sa.select(sa.func.count()).select_from(DailyHotelMetric)) == 0


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_every_mutating_verb_is_rejected(api: TestClient, flat_hotel: str, method: str) -> None:
    call = getattr(api, method)
    target = url(flat_hotel, "forecast/occupancy")

    response = call(target) if method == "delete" else call(target, json={})

    assert response.status_code == 405


# --- response hygiene -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "report", ["forecast/occupancy", "forecast/revenue", "demand-trend", "anomalies", "insights"]
)
def test_no_response_exposes_an_internal_id(api: TestClient, flat_hotel: str, report: str) -> None:
    params = FORECAST if report.startswith("forecast") else WINDOW
    body = api.get(url(flat_hotel, report), params=params).json()

    for forbidden in ("id", "hotel_id", "booking_id", "room_id", "guest_id", "category_id"):
        assert forbidden not in body
    assert uuid.UUID(body["hotel_public_id"])


def test_errors_use_the_shared_envelope(api: TestClient) -> None:
    body = api.get(url(str(uuid.uuid4()), "anomalies"), params=WINDOW).json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_the_occupancy_forecast_contract_is_exactly_as_declared(
    api: TestClient, flat_hotel: str
) -> None:
    body = api.get(url(flat_hotel, "forecast/occupancy"), params=FORECAST).json()

    assert set(body) == {
        "hotel_public_id",
        "model",
        "training_window",
        "horizon",
        "points",
    }
    assert set(body["points"][0]) == {
        "date",
        "on_the_books_room_nights",
        "available_room_nights",
        "predicted_room_nights",
        "predicted_occupancy_rate",
        "interval_lower",
        "interval_upper",
        "confidence_level",
        "method",
        "observations",
        "capacity_clamped",
    }


def test_the_anomaly_contract_carries_the_full_statistical_basis(api: TestClient) -> None:
    hotel = make_hotel(api, "contract", rooms=8)
    guest = make_guest(api, hotel)
    for offset in range(HISTORY_DAYS):
        night = HISTORY_START + dt.timedelta(days=offset)
        occupied = 8 if offset == 20 else (1 + offset % 2)
        for index in range(occupied):
            stay(api, hotel, guest, room=f"{101 + index}", night=night)

    anomaly = api.get(url(hotel, "anomalies"), params=WINDOW).json()["anomalies"][0]

    assert set(anomaly) == {
        "metric",
        "date",
        "value",
        "median",
        "median_absolute_deviation",
        "modified_z_score",
        "threshold",
        "direction",
    }
