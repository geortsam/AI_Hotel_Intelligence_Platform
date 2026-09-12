"""Analytics against real PostgreSQL.

The centrepiece is ``the_join_multiplication_fixture``: one booking with 2 rooms x 4 nights,
several revenue lines, several expenses and a review. If any aggregate were computed by
joining two fan-out branches in one statement, room nights would be 8 but revenue would be
summed 8 times over -- and vice versa. Every KPI over that fixture is asserted independently
against a hand-computed number.

The second theme is currency. A hotel is deliberately given EUR, USD and JPY lines, and the
suite proves nothing is ever summed across them.

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

from app.models import Booking, DailyHotelMetric, Expense, Revenue, Review
from tests.integration.conftest import (
    authenticated_client,
    requires_postgres,
    seed_expense_category,
    seed_revenue_category,
)

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "analytics@example.test"

pytestmark = requires_postgres

#: The stay used by the join-multiplication fixture: four nights, 1-5 September.
STAY_IN = dt.date(2026, 9, 1)
STAY_OUT = dt.date(2026, 9, 5)
NIGHTS = [STAY_IN + dt.timedelta(days=n) for n in range(4)]
RANGE = {"date_from": "2026-09-01", "date_to": "2026-09-30"}


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


def analytics(hotel: str, report: str) -> str:
    return f"/api/v1/hotels/{hotel}/analytics/{report}"


# --- builders ----------------------------------------------------------------------------------


def make_categories(engine: Engine) -> None:
    """The vocabularies these reports group by.

    Seeded directly: from Stage 4.2 the catalogue write endpoints are refused to every caller,
    because a row shared by every hotel cannot be governed by a per-hotel role. Analytics only
    ever READS these, which is unaffected.
    """
    seed_revenue_category(engine, "FB", "Food and beverage")
    seed_revenue_category(engine, "ROOMS", "Rooms", is_room_revenue=True)
    seed_expense_category(engine, "UTILITIES", "Utilities")


def make_hotel(api: TestClient, slug: str = "hotel-a", *, rooms: int = 2) -> str:
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


def make_booking(
    api: TestClient,
    hotel: str,
    guest: str,
    *,
    room_numbers: list[str],
    check_in: dt.date = STAY_IN,
    check_out: dt.date = STAY_OUT,
    status: str = "confirmed",
    rate: str = "100.00",
    currency: str = "EUR",
    complimentary: bool = False,
) -> str:
    nights = [check_in + dt.timedelta(days=n) for n in range((check_out - check_in).days)]
    total = Decimal(rate) * len(nights) * len(room_numbers)
    # Stage 4.5.23: the server prices the nights, so a test that wants a particular rate
    # configures it rather than sending it. Bookings already made keep the rate they were
    # created at -- the snapshot property -- so several rates can still coexist here.
    configured = api.patch(
        f"/api/v1/hotels/{hotel}/room-types/DLX",
        json={"base_price": rate, "currency": currency},
    )
    assert configured.status_code == 200, configured.text
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(check_in),
            "check_out_date": str(check_out),
            "status": status,
            "total_amount": str(total),
            "currency": currency,
            "rooms": [
                {
                    "room_number": number,
                    "nights": [
                        {
                            "stay_date": str(night),
                            "is_complimentary": complimentary,
                        }
                        for night in nights
                    ],
                }
                for number in room_numbers
            ],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def post_revenue(
    api: TestClient,
    hotel: str,
    amount: str,
    *,
    date: str = "2026-09-02",
    category: str = "FB",
    currency: str = "EUR",
    booking: str | None = None,
) -> None:
    body: dict[str, object] = {
        "category_code": category,
        "revenue_date": date,
        "amount": amount,
        "currency": currency,
    }
    if booking:
        body["booking_public_id"] = booking
    response = api.post(f"/api/v1/hotels/{hotel}/revenue", json=body)
    assert response.status_code == 201, response.text


def post_expense(
    api: TestClient,
    hotel: str,
    amount: str,
    *,
    date: str = "2026-09-02",
    category: str = "UTILITIES",
    currency: str = "EUR",
) -> None:
    response = api.post(
        f"/api/v1/hotels/{hotel}/expenses",
        json={
            "category_code": category,
            "expense_date": date,
            "amount": amount,
            "currency": currency,
        },
    )
    assert response.status_code == 201, response.text


def post_review(
    api: TestClient,
    hotel: str,
    booking: str,
    *,
    rating: str = "4.00",
    scale: int = 5,
    published: bool = True,
    source: str = "direct",
    date: str = "2026-09-06",
) -> None:
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/review",
        json={
            "rating": rating,
            "rating_scale": scale,
            "review_date": date,
            "is_published": published,
            "source": source,
        },
    )
    assert response.status_code == 201, response.text


# --- the join-multiplication fixture -------------------------------------------------------------


@pytest.fixture
def multiplication(api: TestClient, engine: Engine) -> str:
    """ONE booking, TWO rooms, FOUR nights each, THREE revenue lines, TWO expenses, ONE review.

    Deliberately shaped so every wrong join produces a different wrong number:

    * room nights          = 2 x 4              = 8
    * room revenue         = 8 x 100.00         = 800.00
    * ledger F&B revenue   = 10 + 20 + 30       = 60.00
    * expenses             = 15 + 25            = 40.00
    * reviews              = 1

    A join of nights x revenue would report 24 revenue rows summing 480.00; a join of
    nights x reviews would report 8 reviews. Each is asserted separately below.
    """
    make_categories(engine)
    hotel = make_hotel(api, "multiplication", rooms=2)
    guest = make_guest(api, hotel)
    booking = make_booking(api, hotel, guest, room_numbers=["101", "102"])

    for amount in ("10.00", "20.00", "30.00"):
        post_revenue(api, hotel, amount, booking=booking)
    post_expense(api, hotel, "15.00")
    post_expense(api, hotel, "25.00", date="2026-09-03")
    post_review(api, hotel, booking)
    return hotel


def test_room_nights_are_not_multiplied_by_revenue_lines(
    api: TestClient, multiplication: str
) -> None:
    body = api.get(analytics(multiplication, "overview"), params=RANGE).json()

    assert body["occupancy"]["occupied_room_nights"] == 8
    assert body["occupancy"]["room_nights_sold"] == 8


def test_room_revenue_is_not_multiplied_by_rooms_or_nights(
    api: TestClient, multiplication: str
) -> None:
    """8 nights x 100.00 = 800.00. A nights x revenue join would give 2400.00."""
    body = api.get(analytics(multiplication, "overview"), params=RANGE).json()

    assert body["room_revenue"] == [
        {"currency": "EUR", "room_revenue": "800.00", "adr": "100.00", "revpar": "13.33"}
    ]


def test_ledger_revenue_is_not_multiplied_by_room_nights(
    api: TestClient, multiplication: str
) -> None:
    """60.00, not 60 x 8. This is the failure mode the whole fixture exists to catch."""
    body = api.get(analytics(multiplication, "overview"), params=RANGE).json()

    assert body["other_revenue"] == [{"currency": "EUR", "amount": "60.00"}]


def test_expenses_are_not_multiplied_by_anything(api: TestClient, multiplication: str) -> None:
    body = api.get(analytics(multiplication, "overview"), params=RANGE).json()

    assert body["total_expenses"] == [{"currency": "EUR", "amount": "40.00"}]


def test_reviews_are_not_multiplied_by_room_nights(api: TestClient, multiplication: str) -> None:
    body = api.get(analytics(multiplication, "overview"), params=RANGE).json()

    assert body["reviews"]["review_count"] == 1
    assert body["reviews"]["published_count"] == 1


def test_the_booking_is_counted_once_not_once_per_room(
    api: TestClient, multiplication: str
) -> None:
    body = api.get(analytics(multiplication, "overview"), params=RANGE).json()

    assert body["bookings_by_stay"]["total"] == 1
    assert body["bookings_by_stay"]["confirmed"] == 1
    assert body["stay_flow"]["arrivals"] == 1
    assert body["stay_flow"]["departures"] == 1


def test_the_net_result_composes_the_independent_figures(
    api: TestClient, multiplication: str
) -> None:
    """(800 room + 60 ledger) - 40 expenses = 820.00."""
    body = api.get(analytics(multiplication, "overview"), params=RANGE).json()

    assert body["net_operating_result"] == [{"currency": "EUR", "amount": "820.00"}]
    assert body["is_multi_currency"] is False


def test_the_daily_series_does_not_multiply_either(api: TestClient, multiplication: str) -> None:
    """Two rooms occupied on each of four nights, and the ledger lines land on their own
    dates rather than being smeared across the stay."""
    days = api.get(analytics(multiplication, "daily"), params=RANGE).json()["days"]
    by_date = {day["date"]: day for day in days}

    for night in NIGHTS:
        assert by_date[str(night)]["occupied_room_nights"] == 2, night
        assert by_date[str(night)]["room_revenue"][0]["room_revenue"] == "200.00"

    assert by_date["2026-09-02"]["other_revenue"] == [{"currency": "EUR", "amount": "60.00"}]
    assert by_date["2026-09-02"]["total_expenses"] == [{"currency": "EUR", "amount": "15.00"}]
    assert by_date["2026-09-03"]["total_expenses"] == [{"currency": "EUR", "amount": "25.00"}]


def test_the_daily_room_nights_sum_to_the_overview_total(
    api: TestClient, multiplication: str
) -> None:
    """The two endpoints must agree; they are computed by different queries."""
    overview = api.get(analytics(multiplication, "overview"), params=RANGE).json()
    days = api.get(analytics(multiplication, "daily"), params=RANGE).json()["days"]

    assert (
        sum(day["occupied_room_nights"] for day in days)
        == (overview["occupancy"]["occupied_room_nights"])
    )


def test_the_daily_revenue_sums_to_the_overview_total(api: TestClient, multiplication: str) -> None:
    overview = api.get(analytics(multiplication, "overview"), params=RANGE).json()
    days = api.get(analytics(multiplication, "daily"), params=RANGE).json()["days"]

    daily_room = sum(
        Decimal(bucket["room_revenue"]) for day in days for bucket in day["room_revenue"]
    )
    assert daily_room == Decimal(overview["room_revenue"][0]["room_revenue"])


# --- occupancy semantics --------------------------------------------------------------------------


@pytest.fixture
def occupancy_hotel(api: TestClient, engine: Engine) -> str:
    make_categories(engine)
    return make_hotel(api, "occupancy", rooms=2)


def test_occupancy_counts_nights_not_bookings(api: TestClient, occupancy_hotel: str) -> None:
    """One booking of four nights in one room is four room nights, not one."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"])

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["occupancy"]["occupied_room_nights"] == 4
    assert body["bookings_by_stay"]["total"] == 1


def test_a_pending_booking_holds_no_occupancy(api: TestClient, occupancy_hotel: str) -> None:
    """An abandoned checkout must never appear as an occupied room."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"], status="pending")

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["occupancy"]["occupied_room_nights"] == 0
    assert body["bookings_by_stay"]["pending"] == 1
    assert body["bookings_by_stay"]["total"] == 1


def test_a_checked_out_stay_still_counts_as_occupancy(
    api: TestClient, occupancy_hotel: str
) -> None:
    """OCCUPANCY_STATUSES is wider than the inventory-holding pair on purpose: a completed
    stay no longer blocks a room but certainly occupied it."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"], status="checked_out")

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["occupancy"]["occupied_room_nights"] == 4
    assert body["bookings_by_stay"]["checked_out"] == 1


def test_a_cancelled_booking_holds_no_occupancy(api: TestClient, occupancy_hotel: str) -> None:
    """Cancelling releases occupancy, and the two date columns answer different questions.

    Both windows are derived from the ROW rather than from the wall clock, and the stay is
    put a year out so the stay window provably cannot contain "now". An earlier version of
    this test used a fixed September window and `dt.date.today()`; it passed only because of
    where the calendar happened to be, and broke the day the month rolled over.
    """
    guest = make_guest(api, occupancy_hotel)
    stay_in, stay_out = dt.date(2027, 6, 1), dt.date(2027, 6, 5)
    booking = make_booking(
        api, occupancy_hotel, guest, room_numbers=["101"], check_in=stay_in, check_out=stay_out
    )
    api.patch(f"/api/v1/hotels/{occupancy_hotel}/bookings/{booking}", json={"status": "cancelled"})

    detail = api.get(f"/api/v1/hotels/{occupancy_hotel}/bookings/{booking}").json()
    cancelled_on = dt.datetime.fromisoformat(detail["cancelled_at"]).date()
    # The premise the assertions below rest on, checked rather than assumed.
    assert not stay_in <= cancelled_on <= stay_out

    over_the_stay = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": str(stay_in), "date_to": str(stay_out)},
    ).json()

    assert over_the_stay["occupancy"]["occupied_room_nights"] == 0  # released
    assert over_the_stay["bookings_by_stay"]["cancelled"] == 1  # still on the books
    # cancelled_at is when it HAPPENED, which is not inside the stay window.
    assert over_the_stay["stay_flow"]["cancellations"] == 0

    over_the_cancellation = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": str(cancelled_on), "date_to": str(cancelled_on)},
    ).json()

    assert over_the_cancellation["stay_flow"]["cancellations"] == 1
    assert over_the_cancellation["bookings_by_stay"]["total"] == 0


def test_back_to_back_stays_in_one_room_do_not_double_count(
    api: TestClient, occupancy_hotel: str
) -> None:
    """The second stay checks in on the day the first checks out. The night the first guest
    departs belongs to the second guest only -- the schema's CHECK requires
    stay_date < check_out_date, so a checkout day is never a night."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"])
    make_booking(
        api,
        occupancy_hotel,
        guest,
        room_numbers=["101"],
        check_in=STAY_OUT,
        check_out=STAY_OUT + dt.timedelta(days=2),
    )

    days = api.get(analytics(occupancy_hotel, "daily"), params=RANGE).json()["days"]
    by_date = {day["date"]: day["occupied_room_nights"] for day in days}

    assert by_date["2026-09-04"] == 1  # last night of the first stay
    assert by_date["2026-09-05"] == 1  # checkout day of the first, first night of the second
    assert by_date["2026-09-06"] == 1
    assert by_date["2026-09-07"] == 0  # the second stay's own checkout day


def test_two_rooms_on_the_same_night_are_two_room_nights(
    api: TestClient, occupancy_hotel: str
) -> None:
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101", "102"])

    days = api.get(analytics(occupancy_hotel, "daily"), params=RANGE).json()["days"]
    by_date = {day["date"]: day["occupied_room_nights"] for day in days}

    assert by_date["2026-09-01"] == 2


def test_the_occupancy_rate_uses_room_nights_not_bookings(
    api: TestClient, occupancy_hotel: str
) -> None:
    """2 rooms x 30 days = 60 available room nights; 4 occupied gives 0.0667."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"])

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()["occupancy"]

    assert body["available_room_nights"] == 60
    assert Decimal(body["occupancy_rate"]) == Decimal("0.0667")
    assert body["available_room_nights_basis"] == "current_active_rooms"


def test_a_hotel_with_no_rooms_has_an_undefined_occupancy_rate(
    api: TestClient, engine: Engine
) -> None:
    """Null, never zero: the NULLIF guard from the generated column."""
    make_categories(engine)
    hotel = make_hotel(api, "roomless", rooms=0)

    body = api.get(analytics(hotel, "overview"), params=RANGE).json()["occupancy"]

    assert body["available_room_nights"] == 0
    assert body["occupancy_rate"] is None


def test_a_complimentary_night_is_occupied_but_not_sold(
    api: TestClient, occupancy_hotel: str
) -> None:
    """It occupied the room, so occupancy counts it; it earned nothing, so ADR must not be
    dragged down by it."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"], rate="0.00", complimentary=True)

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["occupancy"]["occupied_room_nights"] == 4
    assert body["occupancy"]["room_nights_sold"] == 0
    assert body["occupancy"]["complimentary_room_nights"] == 4
    assert body["room_revenue"][0]["adr"] is None  # no sold nights: undefined, not zero


# --- ADR and RevPAR -------------------------------------------------------------------------------


def test_adr_divides_room_revenue_by_nights_sold(api: TestClient, occupancy_hotel: str) -> None:
    """4 nights at 150.00 = 600.00; ADR 150.00; RevPAR 600 / 60 available = 10.00."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"], rate="150.00")

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["room_revenue"] == [
        {"currency": "EUR", "room_revenue": "600.00", "adr": "150.00", "revpar": "10.00"}
    ]


def test_adr_comes_from_the_night_rate_not_the_booking_total(
    api: TestClient, occupancy_hotel: str
) -> None:
    """bookings.total_amount is a header figure a client supplies; the night rate is the
    per-night truth, and only it may drive ADR."""
    guest = make_guest(api, occupancy_hotel)
    api.patch(
        f"/api/v1/hotels/{occupancy_hotel}/room-types/DLX",
        json={"base_price": "100.00"},
    )
    api.post(
        f"/api/v1/hotels/{occupancy_hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": "BK-MISMATCH",
            "check_in_date": str(STAY_IN),
            "check_out_date": str(STAY_IN + dt.timedelta(days=2)),
            "status": "confirmed",
            # Deliberately unrelated to the nights below.
            "total_amount": "99999.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [
                        {"stay_date": "2026-09-01"},
                        {"stay_date": "2026-09-02"},
                    ],
                }
            ],
        },
    )

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["room_revenue"][0]["room_revenue"] == "200.00"  # 2 x 100, not 99999
    assert body["room_revenue"][0]["adr"] == "100.00"


def test_adr_ignores_the_room_types_list_price(api: TestClient, occupancy_hotel: str) -> None:
    """ADR reads the night rows, not the rate card.

    Since Stage 4.5.23 a night is priced FROM the list price, so the two agree at the
    moment of booking and the old form of this test could no longer tell them apart. The
    list price is therefore moved afterwards: ADR must still report what was contracted,
    not what the room type costs today.
    """
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"], rate="70.00")
    api.patch(
        f"/api/v1/hotels/{occupancy_hotel}/room-types/DLX",
        json={"base_price": "555.00"},
    )

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["room_revenue"][0]["adr"] == "70.00"


def test_adr_ignores_payments(api: TestClient, occupancy_hotel: str) -> None:
    """A payment is money moving, not revenue earned. Recording one must not shift ADR."""
    guest = make_guest(api, occupancy_hotel)
    booking = make_booking(api, occupancy_hotel, guest, room_numbers=["101"], rate="100.00")
    before = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    api.post(
        f"/api/v1/hotels/{occupancy_hotel}/bookings/{booking}/payments",
        json={"amount": "5000.00", "currency": "EUR", "method": "card"},
    )

    after = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()
    assert after["room_revenue"] == before["room_revenue"]
    assert after["net_operating_result"] == before["net_operating_result"]


def test_a_hotel_with_no_stays_reports_no_room_revenue(
    api: TestClient, occupancy_hotel: str
) -> None:
    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["room_revenue"] == []
    assert body["occupancy"]["occupied_room_nights"] == 0


# --- currency -------------------------------------------------------------------------------------


@pytest.fixture
def multi_currency(api: TestClient, engine: Engine) -> str:
    """EUR room revenue, USD and JPY ledger lines, and a GBP expense."""
    make_categories(engine)
    hotel = make_hotel(api, "multi-currency", rooms=1)
    guest = make_guest(api, hotel)
    make_booking(api, hotel, guest, room_numbers=["101"], rate="100.00", currency="EUR")
    post_revenue(api, hotel, "500.00", currency="USD")
    post_revenue(api, hotel, "70000.00", currency="JPY")
    post_expense(api, hotel, "80.00", currency="GBP")
    return hotel


def test_currencies_are_never_summed_together(api: TestClient, multi_currency: str) -> None:
    body = api.get(analytics(multi_currency, "overview"), params=RANGE).json()

    assert body["other_revenue"] == [
        {"currency": "JPY", "amount": "70000.00"},
        {"currency": "USD", "amount": "500.00"},
    ]
    assert body["total_expenses"] == [{"currency": "GBP", "amount": "80.00"}]
    assert body["is_multi_currency"] is True


def test_the_net_result_stays_within_each_currency(api: TestClient, multi_currency: str) -> None:
    """EUR earns 400 room revenue, USD 500, JPY 70000; GBP only spends 80. Nothing is
    converted, and the GBP expense appears as a negative GBP result rather than being
    folded into any other currency."""
    body = api.get(analytics(multi_currency, "overview"), params=RANGE).json()

    assert body["net_operating_result"] == [
        {"currency": "EUR", "amount": "400.00"},
        {"currency": "GBP", "amount": "-80.00"},
        {"currency": "JPY", "amount": "70000.00"},
        {"currency": "USD", "amount": "500.00"},
    ]


def test_no_response_field_holds_a_bare_monetary_scalar(
    api: TestClient, multi_currency: str
) -> None:
    """Every money field is a list of buckets, so a multi-currency hotel cannot produce a
    single misleading number."""
    body = api.get(analytics(multi_currency, "overview"), params=RANGE).json()

    for field in ("room_revenue", "other_revenue", "total_expenses", "net_operating_result"):
        assert isinstance(body[field], list), field
        for bucket in body[field]:
            assert "currency" in bucket, field


def test_the_hotels_own_currency_is_not_assumed(api: TestClient, multi_currency: str) -> None:
    """The hotel banks in EUR, but the JPY and USD lines are reported as themselves."""
    body = api.get(analytics(multi_currency, "overview"), params=RANGE).json()
    currencies = {bucket["currency"] for bucket in body["net_operating_result"]}

    assert currencies == {"EUR", "GBP", "JPY", "USD"}


def test_a_single_currency_hotel_is_not_flagged_multi_currency(
    api: TestClient, occupancy_hotel: str
) -> None:
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"])
    post_revenue(api, occupancy_hotel, "50.00")

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["is_multi_currency"] is False


def test_room_revenue_is_bucketed_by_the_bookings_currency(api: TestClient, engine: Engine) -> None:
    """booking_room_nights carries no currency of its own; it inherits the booking's."""
    make_categories(engine)
    hotel = make_hotel(api, "two-currency-stays", rooms=2)
    guest = make_guest(api, hotel)
    make_booking(api, hotel, guest, room_numbers=["101"], rate="100.00", currency="EUR")
    make_booking(api, hotel, guest, room_numbers=["102"], rate="200.00", currency="USD")

    body = api.get(analytics(hotel, "overview"), params=RANGE).json()

    assert body["room_revenue"] == [
        {"currency": "EUR", "room_revenue": "400.00", "adr": "100.00", "revpar": "6.67"},
        {"currency": "USD", "room_revenue": "800.00", "adr": "200.00", "revpar": "13.33"},
    ]


def test_the_daily_series_buckets_currency_per_day(api: TestClient, multi_currency: str) -> None:
    days = api.get(analytics(multi_currency, "daily"), params=RANGE).json()["days"]
    by_date = {day["date"]: day for day in days}

    assert by_date["2026-09-02"]["other_revenue"] == [
        {"currency": "JPY", "amount": "70000.00"},
        {"currency": "USD", "amount": "500.00"},
    ]


# --- the is_room_revenue split --------------------------------------------------------------------


def test_ledger_room_revenue_is_reported_separately_and_added_to_nothing(
    api: TestClient, occupancy_hotel: str
) -> None:
    """Approved decision 21 makes booking_room_nights the source of truth for room revenue.
    A ledger line flagged is_room_revenue is neither folded into other_revenue (which would
    defeat the flag) nor into room_revenue (which would double-count)."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"], rate="100.00")
    post_revenue(api, occupancy_hotel, "999.00", category="ROOMS")
    post_revenue(api, occupancy_hotel, "50.00", category="FB")

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["room_revenue"][0]["room_revenue"] == "400.00"  # from nights only
    assert body["other_revenue"] == [{"currency": "EUR", "amount": "50.00"}]  # FB only
    assert body["ledger_room_revenue"] == [{"currency": "EUR", "amount": "999.00"}]
    # 400 + 50 - 0; the flagged 999.00 is in neither side of the net.
    assert body["net_operating_result"] == [{"currency": "EUR", "amount": "450.00"}]


def test_the_category_breakdown_carries_the_flag_through(
    api: TestClient, occupancy_hotel: str
) -> None:
    post_revenue(api, occupancy_hotel, "999.00", category="ROOMS")
    post_revenue(api, occupancy_hotel, "50.00", category="FB")

    categories = api.get(analytics(occupancy_hotel, "revenue-by-category"), params=RANGE).json()[
        "categories"
    ]

    assert categories == [
        {
            "category_code": "FB",
            "is_room_revenue": False,
            "currency": "EUR",
            "amount": "50.00",
            "tax_amount": "0.00",
            "entry_count": 1,
        },
        {
            "category_code": "ROOMS",
            "is_room_revenue": True,
            "currency": "EUR",
            "amount": "999.00",
            "tax_amount": "0.00",
            "entry_count": 1,
        },
    ]


def test_the_expense_breakdown_carries_the_fixed_cost_flag(
    api: TestClient, occupancy_hotel: str, engine: Engine
) -> None:
    seed_expense_category(engine, "RENT", "Rent", is_fixed_cost=True)
    post_expense(api, occupancy_hotel, "1000.00", category="RENT")
    post_expense(api, occupancy_hotel, "40.00")

    categories = api.get(analytics(occupancy_hotel, "expenses-by-category"), params=RANGE).json()[
        "categories"
    ]

    assert [(c["category_code"], c["is_fixed_cost"]) for c in categories] == [
        ("RENT", True),
        ("UTILITIES", False),
    ]


def test_a_negative_ledger_line_reduces_the_total(api: TestClient, occupancy_hotel: str) -> None:
    """The ledger's correction mechanism must flow through to analytics rather than being
    filtered out."""
    post_revenue(api, occupancy_hotel, "100.00")
    post_revenue(api, occupancy_hotel, "-30.00")

    body = api.get(analytics(occupancy_hotel, "overview"), params=RANGE).json()

    assert body["other_revenue"] == [{"currency": "EUR", "amount": "70.00"}]


# --- date semantics -------------------------------------------------------------------------------


def test_the_range_is_inclusive_at_both_ends(api: TestClient, occupancy_hotel: str) -> None:
    post_revenue(api, occupancy_hotel, "10.00", date="2026-09-01")
    post_revenue(api, occupancy_hotel, "20.00", date="2026-09-05")
    post_revenue(api, occupancy_hotel, "40.00", date="2026-09-06")

    body = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": "2026-09-01", "date_to": "2026-09-05"},
    ).json()

    assert body["other_revenue"] == [{"currency": "EUR", "amount": "30.00"}]
    assert body["range"] == {"date_from": "2026-09-01", "date_to": "2026-09-05", "days": 5}


def test_a_same_day_range_is_one_day(api: TestClient, occupancy_hotel: str) -> None:
    post_revenue(api, occupancy_hotel, "10.00", date="2026-09-02")

    body = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": "2026-09-02", "date_to": "2026-09-02"},
    ).json()

    assert body["range"]["days"] == 1
    assert body["other_revenue"] == [{"currency": "EUR", "amount": "10.00"}]


def test_a_reversed_range_returns_422(api: TestClient, occupancy_hotel: str) -> None:
    response = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": "2026-09-30", "date_to": "2026-09-01"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_an_over_long_range_returns_422(api: TestClient, occupancy_hotel: str) -> None:
    response = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": "2026-01-01", "date_to": "2027-12-31"},
    )

    assert response.status_code == 422


def test_a_range_with_no_activity_returns_zeros_not_an_error(
    api: TestClient, occupancy_hotel: str
) -> None:
    body = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": "2030-01-01", "date_to": "2030-01-31"},
    ).json()

    assert body["bookings_created"]["total"] == 0
    assert body["bookings_by_stay"]["total"] == 0
    assert body["occupancy"]["occupied_room_nights"] == 0
    assert body["other_revenue"] == []
    assert body["reviews"]["average_rating_normalized"] is None


@pytest.mark.parametrize("report", ["overview", "daily", "reviews"])
def test_the_date_range_is_required(api: TestClient, occupancy_hotel: str, report: str) -> None:
    assert api.get(analytics(occupancy_hotel, report)).status_code == 422


def test_stay_dates_and_creation_dates_are_different_columns(
    api: TestClient, occupancy_hotel: str
) -> None:
    """A booking created today for a stay next year appears as created now and arriving
    then. Conflating the two would misreport both."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(
        api,
        occupancy_hotel,
        guest,
        room_numbers=["101"],
        check_in=dt.date(2027, 3, 1),
        check_out=dt.date(2027, 3, 3),
    )

    stay_window = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": "2027-03-01", "date_to": "2027-03-31"},
    ).json()

    assert stay_window["occupancy"]["occupied_room_nights"] == 2
    assert stay_window["stay_flow"]["arrivals"] == 1
    # The stay is served in that window, so the stay-dated count sees it...
    assert stay_window["bookings_by_stay"]["total"] == 1
    # ...but booked_at is today, not March 2027, so the creation-dated count does not.
    assert stay_window["bookings_created"]["total"] == 0


def test_a_departure_day_is_not_a_room_night(api: TestClient, occupancy_hotel: str) -> None:
    """ck_booking_room_nights_stay_date_within_stay requires stay_date < check_out_date."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"])

    days = api.get(analytics(occupancy_hotel, "daily"), params=RANGE).json()["days"]
    by_date = {day["date"]: day for day in days}

    assert by_date["2026-09-04"]["occupied_room_nights"] == 1
    assert by_date["2026-09-05"]["occupied_room_nights"] == 0
    assert by_date["2026-09-05"]["departures"] == 1


# --- the daily series -----------------------------------------------------------------------------


def test_the_series_is_gap_free_and_chronological(api: TestClient, occupancy_hotel: str) -> None:
    """A chart that silently omits empty days draws a misleading line."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"])

    days = api.get(analytics(occupancy_hotel, "daily"), params=RANGE).json()["days"]

    assert len(days) == 30
    dates = [day["date"] for day in days]
    assert dates == sorted(dates)
    assert dates[0] == "2026-09-01"
    assert dates[-1] == "2026-09-30"


def test_the_series_is_deterministic(api: TestClient, occupancy_hotel: str) -> None:
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101", "102"])
    post_revenue(api, occupancy_hotel, "10.00")

    first = api.get(analytics(occupancy_hotel, "daily"), params=RANGE).json()
    second = api.get(analytics(occupancy_hotel, "daily"), params=RANGE).json()

    assert first == second


def test_empty_days_are_zero_rows_not_nulls(api: TestClient, occupancy_hotel: str) -> None:
    days = api.get(analytics(occupancy_hotel, "daily"), params=RANGE).json()["days"]

    assert all(day["occupied_room_nights"] == 0 for day in days)
    assert all(day["room_revenue"] == [] for day in days)
    assert all(day["arrivals"] == 0 for day in days)


def test_the_daily_row_omits_booking_status_counts(api: TestClient, occupancy_hotel: str) -> None:
    """Status is current, not historical."""
    days = api.get(analytics(occupancy_hotel, "daily"), params=RANGE).json()["days"]

    for absent in ("confirmed", "cancelled", "pending", "checked_out"):
        assert absent not in days[0]


# --- reviews --------------------------------------------------------------------------------------


def test_ratings_are_normalized_across_scales(api: TestClient, engine: Engine) -> None:
    """8/10 and 4/5 are the same review quality. Averaging the raw numbers would not be."""
    make_categories(engine)
    hotel = make_hotel(api, "reviews", rooms=2)
    guest = make_guest(api, hotel)
    first = make_booking(api, hotel, guest, room_numbers=["101"])
    second = make_booking(
        api,
        hotel,
        guest,
        room_numbers=["102"],
        check_in=dt.date(2026, 9, 10),
        check_out=dt.date(2026, 9, 12),
    )
    post_review(api, hotel, first, rating="4.00", scale=5)
    post_review(api, hotel, second, rating="8.00", scale=10, source="booking_com")

    body = api.get(analytics(hotel, "reviews"), params=RANGE).json()

    assert body["totals"]["review_count"] == 2
    assert Decimal(body["totals"]["average_rating_normalized"]) == Decimal("0.8000")


def test_unpublished_reviews_are_counted_but_flagged(api: TestClient, engine: Engine) -> None:
    make_categories(engine)
    hotel = make_hotel(api, "unpublished", rooms=2)
    guest = make_guest(api, hotel)
    first = make_booking(api, hotel, guest, room_numbers=["101"])
    second = make_booking(
        api,
        hotel,
        guest,
        room_numbers=["102"],
        check_in=dt.date(2026, 9, 10),
        check_out=dt.date(2026, 9, 12),
    )
    post_review(api, hotel, first, published=True)
    post_review(api, hotel, second, published=False)

    body = api.get(analytics(hotel, "reviews"), params=RANGE).json()

    assert body["totals"]["review_count"] == 2
    assert body["totals"]["published_count"] == 1


def test_a_review_with_no_guest_and_no_booking_is_counted(
    api: TestClient, occupancy_hotel: str, session: Session
) -> None:
    """The schema permits it; excluding such rows would erase every harvested review."""
    hotel_id = session.scalars(
        sa.select(sa.text("id")).select_from(sa.text("hotels")).where(sa.text("slug='occupancy'"))
    ).one()
    session.add(
        Review(
            hotel_id=hotel_id,
            source="tripadvisor",
            external_review_id="ta-analytics-1",
            rating=Decimal("3.00"),
            rating_scale=5,
            review_date=dt.date(2026, 9, 6),
        )
    )
    session.commit()

    body = api.get(analytics(occupancy_hotel, "reviews"), params=RANGE).json()

    assert body["totals"]["review_count"] == 1
    assert Decimal(body["totals"]["average_rating_normalized"]) == Decimal("0.6000")


def test_the_rating_distribution_reports_every_bucket(api: TestClient, engine: Engine) -> None:
    """Empty buckets are present so a chart has a stable x axis."""
    make_categories(engine)
    hotel = make_hotel(api, "distribution", rooms=1)
    guest = make_guest(api, hotel)
    booking = make_booking(api, hotel, guest, room_numbers=["101"])
    post_review(api, hotel, booking, rating="5.00", scale=5)

    buckets = api.get(analytics(hotel, "reviews"), params=RANGE).json()["rating_distribution"]

    assert [b["bucket"] for b in buckets] == [1, 2, 3, 4, 5]
    assert buckets[4]["count"] == 1  # 5/5 = 1.0 lands in the top band
    assert sum(b["count"] for b in buckets) == 1


def test_reviews_are_broken_down_by_source(api: TestClient, engine: Engine) -> None:
    make_categories(engine)
    hotel = make_hotel(api, "sources", rooms=2)
    guest = make_guest(api, hotel)
    first = make_booking(api, hotel, guest, room_numbers=["101"])
    second = make_booking(
        api,
        hotel,
        guest,
        room_numbers=["102"],
        check_in=dt.date(2026, 9, 10),
        check_out=dt.date(2026, 9, 12),
    )
    post_review(api, hotel, first, source="direct")
    post_review(api, hotel, second, source="google", rating="2.00")

    by_source = api.get(analytics(hotel, "reviews"), params=RANGE).json()["by_source"]

    assert [row["source"] for row in by_source] == ["direct", "google"]
    assert by_source[0]["review_count"] == 1
    assert Decimal(by_source[1]["average_rating_normalized"]) == Decimal("0.4000")


def test_reviews_outside_the_range_are_excluded(api: TestClient, engine: Engine) -> None:
    make_categories(engine)
    hotel = make_hotel(api, "review-dates", rooms=1)
    guest = make_guest(api, hotel)
    booking = make_booking(api, hotel, guest, room_numbers=["101"])
    post_review(api, hotel, booking, date="2026-10-15")

    body = api.get(analytics(hotel, "reviews"), params=RANGE).json()

    assert body["totals"]["review_count"] == 0
    assert body["totals"]["average_rating_normalized"] is None


# --- daily_hotel_metrics is untouched -------------------------------------------------------------


def test_analytics_never_populates_the_snapshot_table(
    api: TestClient, multiplication: str, session: Session
) -> None:
    """It is an empty table awaiting a job that does not exist. A GET must not fill it."""
    for report in ("overview", "daily", "revenue-by-category", "expenses-by-category", "reviews"):
        assert api.get(analytics(multiplication, report), params=RANGE).status_code == 200

    assert session.scalar(sa.select(sa.func.count()).select_from(DailyHotelMetric)) == 0


def test_analytics_mutates_no_operational_data(
    api: TestClient, multiplication: str, session: Session
) -> None:
    """Read-only, proven by counting every table analytics touches before and after."""
    tables = (Booking, Revenue, Expense, Review)
    before = [session.scalar(sa.select(sa.func.count()).select_from(t)) for t in tables]

    for report in ("overview", "daily", "revenue-by-category", "expenses-by-category", "reviews"):
        api.get(analytics(multiplication, report), params=RANGE)

    session.expire_all()
    after = [session.scalar(sa.select(sa.func.count()).select_from(t)) for t in tables]
    assert before == after


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_analytics_endpoints_reject_every_mutating_verb(
    api: TestClient, occupancy_hotel: str, method: str
) -> None:
    call = getattr(api, method)
    url = analytics(occupancy_hotel, "overview")

    response = call(url) if method == "delete" else call(url, json={})

    assert response.status_code == 405


# --- tenant isolation -----------------------------------------------------------------------------


@pytest.fixture
def two_hotels(api: TestClient, engine: Engine) -> tuple[str, str]:
    """Equivalent data in two hotels, so every figure must be attributable to exactly one."""
    make_categories(engine)
    hotel_a = make_hotel(api, "hotel-a", rooms=2)
    hotel_b = make_hotel(api, "hotel-b", rooms=4)

    guest_a = make_guest(api, hotel_a, "Alpha")
    guest_b = make_guest(api, hotel_b, "Beta")
    booking_a = make_booking(api, hotel_a, guest_a, room_numbers=["101"], rate="100.00")
    booking_b = make_booking(api, hotel_b, guest_b, room_numbers=["101", "102"], rate="300.00")

    post_revenue(api, hotel_a, "10.00")
    post_revenue(api, hotel_b, "999.00")
    post_expense(api, hotel_a, "5.00")
    post_expense(api, hotel_b, "888.00")
    post_review(api, hotel_a, booking_a, rating="5.00")
    post_review(api, hotel_b, booking_b, rating="1.00")
    return hotel_a, hotel_b


def test_every_overview_figure_is_isolated(api: TestClient, two_hotels: tuple[str, str]) -> None:
    hotel_a, hotel_b = two_hotels

    a = api.get(analytics(hotel_a, "overview"), params=RANGE).json()
    b = api.get(analytics(hotel_b, "overview"), params=RANGE).json()

    assert a["occupancy"]["occupied_room_nights"] == 4
    assert b["occupancy"]["occupied_room_nights"] == 8
    assert a["room_revenue"][0]["room_revenue"] == "400.00"
    assert b["room_revenue"][0]["room_revenue"] == "2400.00"
    assert a["other_revenue"] == [{"currency": "EUR", "amount": "10.00"}]
    assert b["other_revenue"] == [{"currency": "EUR", "amount": "999.00"}]
    assert a["total_expenses"] == [{"currency": "EUR", "amount": "5.00"}]
    assert b["total_expenses"] == [{"currency": "EUR", "amount": "888.00"}]
    assert a["occupancy"]["available_room_nights"] == 60  # 2 rooms x 30 days
    assert b["occupancy"]["available_room_nights"] == 120  # 4 rooms x 30 days


def test_review_analytics_are_isolated(api: TestClient, two_hotels: tuple[str, str]) -> None:
    hotel_a, hotel_b = two_hotels

    a = api.get(analytics(hotel_a, "reviews"), params=RANGE).json()
    b = api.get(analytics(hotel_b, "reviews"), params=RANGE).json()

    assert Decimal(a["totals"]["average_rating_normalized"]) == Decimal("1.0000")
    assert Decimal(b["totals"]["average_rating_normalized"]) == Decimal("0.2000")
    assert a["totals"]["review_count"] == 1
    assert b["totals"]["review_count"] == 1


def test_the_daily_series_is_isolated(api: TestClient, two_hotels: tuple[str, str]) -> None:
    hotel_a, hotel_b = two_hotels

    a = api.get(analytics(hotel_a, "daily"), params=RANGE).json()["days"]
    b = api.get(analytics(hotel_b, "daily"), params=RANGE).json()["days"]
    by_date_a = {day["date"]: day for day in a}
    by_date_b = {day["date"]: day for day in b}

    assert by_date_a["2026-09-01"]["occupied_room_nights"] == 1
    assert by_date_b["2026-09-01"]["occupied_room_nights"] == 2


def test_category_breakdowns_are_isolated(api: TestClient, two_hotels: tuple[str, str]) -> None:
    hotel_a, hotel_b = two_hotels

    a = api.get(analytics(hotel_a, "revenue-by-category"), params=RANGE).json()["categories"]
    b = api.get(analytics(hotel_b, "expenses-by-category"), params=RANGE).json()["categories"]

    assert a == [
        {
            "category_code": "FB",
            "is_room_revenue": False,
            "currency": "EUR",
            "amount": "10.00",
            "tax_amount": "0.00",
            "entry_count": 1,
        }
    ]
    assert b[0]["amount"] == "888.00"


def test_each_response_names_the_hotel_it_describes(
    api: TestClient, two_hotels: tuple[str, str]
) -> None:
    hotel_a, hotel_b = two_hotels

    for report in ("overview", "daily", "revenue-by-category", "expenses-by-category", "reviews"):
        assert api.get(analytics(hotel_a, report), params=RANGE).json()["hotel_public_id"] == (
            hotel_a
        )
        assert api.get(analytics(hotel_b, report), params=RANGE).json()["hotel_public_id"] == (
            hotel_b
        )


@pytest.mark.parametrize(
    "report", ["overview", "daily", "revenue-by-category", "expenses-by-category", "reviews"]
)
def test_an_unknown_hotel_returns_404(api: TestClient, report: str) -> None:
    response = api.get(analytics(str(uuid.uuid4()), report), params=RANGE)

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


def test_a_malformed_hotel_identifier_returns_422(api: TestClient) -> None:
    assert api.get(analytics("not-a-uuid", "overview"), params=RANGE).status_code == 422


# --- response hygiene -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "report", ["overview", "daily", "revenue-by-category", "expenses-by-category", "reviews"]
)
def test_no_response_exposes_an_internal_id(
    api: TestClient, multiplication: str, report: str
) -> None:
    body = api.get(analytics(multiplication, report), params=RANGE).json()

    for forbidden in ("id", "hotel_id", "category_id", "booking_id", "room_id", "guest_id"):
        assert forbidden not in body
    assert uuid.UUID(body["hotel_public_id"])


def test_errors_use_the_shared_envelope(api: TestClient) -> None:
    body = api.get(analytics(str(uuid.uuid4()), "overview"), params=RANGE).json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_the_overview_contract_is_exactly_as_declared(api: TestClient, multiplication: str) -> None:
    body = api.get(analytics(multiplication, "overview"), params=RANGE).json()

    assert set(body) == {
        "hotel_public_id",
        "range",
        "bookings_created",
        "bookings_by_stay",
        "stay_flow",
        "occupancy",
        "room_revenue",
        "other_revenue",
        "ledger_room_revenue",
        "total_expenses",
        "net_operating_result",
        "is_multi_currency",
        "reviews",
    }


def test_the_two_booking_counts_answer_different_questions(
    api: TestClient, occupancy_hotel: str
) -> None:
    """Created-in-range and staying-in-range are different sets, and both are reported.

    The stay is next March; the booking is made today. A window over the stay sees it under
    bookings_by_stay and not under bookings_created, and a window over today sees the
    reverse. Reporting only one would show zero for half the questions a dashboard asks.
    """
    guest = make_guest(api, occupancy_hotel)
    booking = make_booking(
        api,
        occupancy_hotel,
        guest,
        room_numbers=["101"],
        check_in=dt.date(2027, 3, 1),
        check_out=dt.date(2027, 3, 3),
    )
    # Derived from the row's OWN booked_at rather than the wall clock: a run that straddles
    # midnight would otherwise stamp the booking on one date and query the next.
    detail = api.get(f"/api/v1/hotels/{occupancy_hotel}/bookings/{booking}").json()
    created_on = dt.datetime.fromisoformat(detail["booked_at"]).date()

    stay_window = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": "2027-03-01", "date_to": "2027-03-31"},
    ).json()
    creation_window = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": str(created_on), "date_to": str(created_on)},
    ).json()

    assert stay_window["bookings_by_stay"]["confirmed"] == 1
    assert stay_window["bookings_created"]["total"] == 0
    assert creation_window["bookings_created"]["confirmed"] == 1
    assert creation_window["bookings_by_stay"]["total"] == 0


def test_the_stay_overlap_predicate_is_half_open(api: TestClient, occupancy_hotel: str) -> None:
    """Matching daterange(check_in, check_out, '[)') -- the same semantics the schema's own
    exclusion constraint uses. A booking departing on date_from does not overlap."""
    guest = make_guest(api, occupancy_hotel)
    make_booking(api, occupancy_hotel, guest, room_numbers=["101"])  # 09-01 .. 09-05

    arrives_on_last_day = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": "2026-08-20", "date_to": "2026-09-01"},
    ).json()
    departs_on_first_day = api.get(
        analytics(occupancy_hotel, "overview"),
        params={"date_from": "2026-09-05", "date_to": "2026-09-30"},
    ).json()

    assert arrives_on_last_day["bookings_by_stay"]["total"] == 1
    assert departs_on_first_day["bookings_by_stay"]["total"] == 0


def test_tax_amounts_are_formatted_like_every_other_monetary_figure(
    api: TestClient, occupancy_hotel: str
) -> None:
    """Two decimal places throughout: a breakdown that reported "0" beside "50.00" would be
    inconsistent money formatting in the same payload."""
    post_revenue(api, occupancy_hotel, "50.00")

    category = api.get(analytics(occupancy_hotel, "revenue-by-category"), params=RANGE).json()[
        "categories"
    ][0]

    assert category["amount"] == "50.00"
    assert category["tax_amount"] == "0.00"
