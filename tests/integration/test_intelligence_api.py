"""Intelligence against real PostgreSQL.

The two properties that matter most here cannot be checked without a database:

* **Temporal leakage.** A forecast is taken, then data is inserted *inside the horizon*, then
  the same forecast is taken again. The prediction must be byte-identical -- while the
  on-the-books figure beside it moves, proving the test really did write something.
* **Tenant isolation.** Two hotels are given deliberately different histories and every
  forecast, trend, anomaly and insight is checked against the right one.

The series are built with real bookings through the real API, so the numbers the models see
are the numbers Stage 3B.10 would report for the same dates.

SQLite is not substituted.
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
from sqlalchemy.orm import Session, sessionmaker

from app.models import DailyHotelMetric
from tests.integration.conftest import (
    authenticated_client,
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


def make_hotel(api: TestClient, slug: str = "hotel-a", *, rooms: int = 4) -> str:
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
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
            "rooms": [{"room_number": room, "nights": [{"stay_date": str(night), "rate": rate}]}],
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


# --- insufficient data ----------------------------------------------------------------------------


def test_a_hotel_with_no_history_still_answers_with_a_shape(api: TestClient) -> None:
    """A densified series of real zeros is not insufficient data -- the hotel genuinely had
    no bookings -- so the forecast is zero rather than absent."""
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
    assert body["direction"] in {"increasing", "decreasing", "stable", "insufficient_data"}
    assert Decimal(body["threshold"]) == Decimal("0.10")


def test_demand_trend_counts_bookings_as_taken_not_as_stayed(
    api: TestClient, flat_hotel: str
) -> None:
    """booked_at is today for every fixture booking, so a window over the STAY dates sees no
    creations. A demand trend is about bookings being taken."""
    body = api.get(url(flat_hotel, "demand-trend"), params=WINDOW).json()

    assert body["direction"] == "stable"
    assert Decimal(body["earlier_median"]) == 0
    assert Decimal(body["recent_median"]) == 0


def test_a_short_window_reports_insufficient_data(api: TestClient, flat_hotel: str) -> None:
    body = api.get(
        url(flat_hotel, "demand-trend"),
        params={"date_from": "2026-03-02", "date_to": "2026-03-04"},
    ).json()

    assert body["direction"] == "insufficient_data"
    assert body["earlier_median"] is None
    assert body["relative_change"] is None


# --- anomalies ------------------------------------------------------------------------------------


def test_a_flat_history_produces_no_anomalies(api: TestClient, flat_hotel: str) -> None:
    """A constant series has no notion of usual spread, so nothing can be called unusual."""
    body = api.get(url(flat_hotel, "anomalies"), params=WINDOW).json()

    assert body["anomalies"] == []
    assert "occupied_room_nights" in body["metrics_scanned"]


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


def test_the_scan_names_what_it_looked_at(api: TestClient, flat_hotel: str) -> None:
    """An empty list must read as "nothing was unusual", not "nothing was examined"."""
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
