"""A checked-in guest who leaves early takes the nights they did not stay with them (Issue H2).

Before H2 the only way to let a departing guest go was ``checked_in -> checked_out``. That
releases the room -- ``checked_out`` holds no inventory -- but kept every unstayed night, and
``checked_out`` counts as occupied: the nights went on counting as occupancy and revenue, stayed
owed in reconciliation, and were counted twice once the room was sold again.

``POST .../stay/departure`` records the departure date as the new check-out and removes every
night on or after it, in the transaction that checks the guest out. Nights are half-open
``[check_in, check_out)``, so the night of the departure date is not stayed. A plain check-out
before the planned day is refused. Every "today" here is the hotel's own (Europe/Athens),
read through the booking service's clock, which these tests fix at an instant.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import app.services.booking as booking_module
from app.models.enums import AuditAction
from app.repositories.ml_demand import MlDemandRepository
from app.services.audit import AuditTrail
from tests.integration.conftest import authenticated_client, requires_postgres
from tests.integration.test_analytics_api import make_categories

pytestmark = requires_postgres

SUITE_EMAIL = "early-departure@example.test"
D = dt.date

#: 22:30 UTC on 20 September is 01:30 on 21 September in Athens (UTC+3 in summer): the hotel's
#: today is the 21st while UTC's is still the 20th.
INSTANT = dt.datetime(2026, 9, 20, 22, 30, tzinfo=dt.UTC)
TODAY = D(2026, 9, 21)
CHECK_IN, CHECK_OUT, DEPARTURE = D(2026, 9, 15), D(2026, 9, 25), D(2026, 9, 20)
RATE = Decimal("120.00")
RANGE = {"date_from": "2026-09-01", "date_to": "2026-09-30"}


def days(first: dt.date, last_exclusive: dt.date) -> list[dt.date]:
    return [first + dt.timedelta(days=n) for n in range((last_exclusive - first).days)]


# --- setup -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def hotel_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every request's booking service reads this instant as "now"."""
    monkeypatch.setattr(booking_module, "utc_now", lambda: INSTANT)


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


@pytest.fixture
def hotel(api: TestClient, engine: Engine) -> str:
    make_categories(engine)
    hotel = str(
        api.post(
            "/api/v1/hotels",
            json={
                "slug": "early-departure",
                "name": "Early Departure",
                "address_line1": "1 Ermou",
                "city": "Athens",
                "country_code": "GR",
                "timezone": "Europe/Athens",
                "currency": "EUR",
            },
        ).json()["public_id"]
    )
    created = api.post(
        f"/api/v1/hotels/{hotel}/room-types",
        json={
            "code": "DLX",
            "name": "Deluxe",
            "max_occupancy": 4,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": str(RATE),
            "currency": "EUR",
        },
    )
    assert created.status_code == 201, created.text
    for number in ("101", "102"):
        room = api.post(
            f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": number}
        )
        assert room.status_code == 201, room.text
    return hotel


def book(
    api: TestClient,
    hotel: str,
    check_in: dt.date = CHECK_IN,
    check_out: dt.date = CHECK_OUT,
    *,
    status: str = "checked_in",
    room: str = "101",
    complimentary: frozenset[dt.date] = frozenset(),
    total: str = "1200.00",
) -> str:
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(check_in),
            "check_out_date": str(check_out),
            "status": status,
            "total_amount": total,
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": room,
                    "nights": [
                        {"stay_date": str(day), "is_complimentary": day in complimentary}
                        for day in days(check_in, check_out)
                    ],
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def url(hotel: str, booking: str, tail: str = "") -> str:
    return f"/api/v1/hotels/{hotel}/bookings/{booking}{tail}"


def depart(api: TestClient, hotel: str, booking: str, day: dt.date = DEPARTURE) -> Any:
    return api.post(url(hotel, booking, "/stay/departure"), json={"departure_date": str(day)})


def preview(api: TestClient, hotel: str, booking: str, day: dt.date | None = None) -> Any:
    params = {} if day is None else {"departure_date": str(day)}
    return api.get(url(hotel, booking, "/stay/departure"), params=params)


def charge(api: TestClient, hotel: str, booking: str, amount: str, currency: str = "EUR") -> None:
    response = api.post(
        url(hotel, booking, "/payments"),
        json={
            "amount": amount,
            "currency": currency,
            "method": "card",
            "status": "captured",
            "paid_at": INSTANT.isoformat(),
        },
    )
    assert response.status_code == 201, response.text


def refused(response: Any, starts: str) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT"
    assert response.json()["error"]["message"].startswith(starts), response.json()


def stay_dates(engine: Engine, booking: str) -> list[dt.date]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                sa.text(
                    "SELECT n.stay_date FROM booking_room_nights n "
                    "JOIN booking_rooms r ON r.id = n.booking_room_id "
                    "JOIN bookings b ON b.id = r.booking_id "
                    "WHERE b.public_id = :b ORDER BY n.stay_date"
                ),
                {"b": booking},
            ).scalars()
        )


def audit_events(engine: Engine, booking: str) -> list[tuple[str, dict[str, Any]]]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT action, details FROM audit_events WHERE resource_reference = :b ORDER BY id"
            ),
            {"b": booking},
        ).all()
    return [(action, details) for action, details in rows]


def overview(api: TestClient, hotel: str) -> dict[str, Any]:
    response = api.get(f"/api/v1/hotels/{hotel}/analytics/overview", params=RANGE)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- the departure itself -------------------------------------------------------------------


def test_an_early_departure_keeps_the_nights_stayed_and_removes_the_rest(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    """15 -> 25 September, gone on the 20th: the 15th-19th stay, the 20th-24th leave."""
    booking = book(api, hotel)

    response = depart(api, hotel, booking)

    assert response.status_code == 200, response.text
    body = response.json()["booking"]
    assert body["status"] == "checked_out"
    assert body["check_in_date"] == str(CHECK_IN)
    assert body["check_out_date"] == str(DEPARTURE)
    [room] = body["rooms"]
    assert room["nights"] == 5
    assert [n["stay_date"] for n in room["nightly_rates"]] == [
        str(d) for d in days(CHECK_IN, DEPARTURE)
    ]
    assert stay_dates(engine, booking) == days(CHECK_IN, DEPARTURE)
    assert api.get(url(hotel, booking)).json()["check_out_date"] == str(DEPARTURE)


def test_the_money_that_leaves_is_the_rates_of_the_nights_removed(
    api: TestClient, hotel: str
) -> None:
    """Not the contracted total, not today's rate: the stored rates of the rows deleted."""
    booking = book(api, hotel, total="999.00")
    # The rate changes after the sale; the stored nights keep the 120.00 they were sold at.
    assert (
        api.patch(
            f"/api/v1/hotels/{hotel}/room-types/DLX", json={"base_price": "300.00"}
        ).status_code
        == 200
    )
    charge(api, hotel, booking, "1200.00")

    repricing = depart(api, hotel, booking).json()["repricing"]

    assert repricing == {
        "currency": "EUR",
        "previous_total": "1200.00",
        "new_total": "600.00",
        "difference": "-600.00",
        "additional_amount_due": "0.00",
        "refundable_amount": "600.00",
        "outstanding_after": "-600.00",
        "adjustment": "refundable",
    }


def test_nothing_is_refundable_when_nothing_was_paid(api: TestClient, hotel: str) -> None:
    booking = book(api, hotel)

    repricing = depart(api, hotel, booking).json()["repricing"]

    assert (repricing["difference"], repricing["refundable_amount"]) == ("-600.00", "0.00")
    assert repricing["adjustment"] == "none"
    assert repricing["outstanding_after"] == "600.00"


def test_a_complimentary_night_removed_takes_no_money_with_it(api: TestClient, hotel: str) -> None:
    booking = book(api, hotel, complimentary=frozenset({D(2026, 9, 22), D(2026, 9, 23)}))
    charge(api, hotel, booking, "960.00")

    repricing = depart(api, hotel, booking).json()["repricing"]

    assert (repricing["previous_total"], repricing["new_total"]) == ("960.00", "600.00")
    assert repricing["refundable_amount"] == "360.00"


def test_a_booking_paid_in_another_currency_is_not_departed(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    """The same refusal every repricing makes: no conversion is performed."""
    booking = book(api, hotel)
    charge(api, hotel, booking, "100.00", currency="USD")

    refused(depart(api, hotel, booking), "This booking cannot be repriced")
    refused(preview(api, hotel, booking, DEPARTURE), "This booking cannot be repriced")
    assert api.get(url(hotel, booking)).json()["status"] == "checked_in"
    assert stay_dates(engine, booking) == days(CHECK_IN, CHECK_OUT)


# --- what the figures say afterwards ----------------------------------------------------------


def test_occupancy_revenue_and_adr_count_only_the_nights_stayed(
    api: TestClient, hotel: str
) -> None:
    booking = book(api, hotel)
    assert overview(api, hotel)["occupancy"]["occupied_room_nights"] == 10

    depart(api, hotel, booking)

    body = overview(api, hotel)
    assert body["occupancy"]["occupied_room_nights"] == 5
    [revenue] = body["room_revenue"]
    assert Decimal(revenue["room_revenue"]) == 5 * RATE
    assert Decimal(revenue["adr"]) == RATE


def test_the_freed_nights_sold_again_are_counted_once(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    first = book(api, hotel)
    depart(api, hotel, first)

    # The same room, the nights the first guest did not stay.
    second = book(api, hotel, DEPARTURE, CHECK_OUT, status="confirmed")

    body = overview(api, hotel)
    assert body["occupancy"]["occupied_room_nights"] == 10
    assert Decimal(body["room_revenue"][0]["room_revenue"]) == 10 * RATE
    assert stay_dates(engine, second) == days(DEPARTURE, CHECK_OUT)
    with engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT count(*) FROM historical_room_overlaps")).scalar()
            == 0
        )


def test_demand_history_counts_only_the_nights_stayed(
    api: TestClient, hotel: str, session: Session
) -> None:
    booking = book(api, hotel)
    depart(api, hotel, booking)

    hotel_id = session.execute(
        sa.text("SELECT id FROM hotels WHERE public_id = :h"), {"h": hotel}
    ).scalar_one()
    demand = MlDemandRepository(session).demand_by_date(hotel_id, D(2026, 9, 1), D(2026, 9, 30))

    assert demand == dict.fromkeys(days(CHECK_IN, DEPARTURE), 1)


def test_reconciliation_owes_only_the_nights_stayed(api: TestClient, hotel: str) -> None:
    booking = book(api, hotel)
    charge(api, hotel, booking, "200.00")
    depart(api, hotel, booking)

    body = api.get(url(hotel, booking, "/reconciliation")).json()

    assert body["accommodation_total"] == "600.00"
    assert body["outstanding_amount"] == "400.00"


# --- the dates allowed ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check_in", "check_out", "departure", "allowed"),
    [
        # Later than check-in: the day after it is the earliest.
        (
            TODAY - dt.timedelta(days=3),
            TODAY + dt.timedelta(days=4),
            TODAY - dt.timedelta(days=2),
            True,
        ),
        (
            TODAY - dt.timedelta(days=3),
            TODAY + dt.timedelta(days=4),
            TODAY - dt.timedelta(days=3),
            False,
        ),
        # Earlier than the planned check-out: the day before it is the latest.
        (TODAY - dt.timedelta(days=5), TODAY, TODAY - dt.timedelta(days=1), True),
        (TODAY - dt.timedelta(days=5), TODAY, TODAY, False),
        # No later than the hotel's today.
        (TODAY - dt.timedelta(days=5), TODAY + dt.timedelta(days=3), TODAY, True),
        (
            TODAY - dt.timedelta(days=5),
            TODAY + dt.timedelta(days=3),
            TODAY + dt.timedelta(days=1),
            False,
        ),
        # No earlier than 28 days before it.
        (
            TODAY - dt.timedelta(days=30),
            TODAY + dt.timedelta(days=2),
            TODAY - dt.timedelta(days=28),
            True,
        ),
        (
            TODAY - dt.timedelta(days=30),
            TODAY + dt.timedelta(days=2),
            TODAY - dt.timedelta(days=29),
            False,
        ),
    ],
    ids=[
        "check_in_plus_1",
        "check_in",
        "planned_check_out_minus_1",
        "planned_check_out",
        "today",
        "today_plus_1",
        "today_minus_28",
        "today_minus_29",
    ],
)
def test_each_bound_of_the_departure_date(
    api: TestClient,
    hotel: str,
    engine: Engine,
    check_in: dt.date,
    check_out: dt.date,
    departure: dt.date,
    allowed: bool,
) -> None:
    booking = book(api, hotel, check_in, check_out)

    response = depart(api, hotel, booking, departure)

    if allowed:
        assert response.status_code == 200, response.text
        assert stay_dates(engine, booking) == days(check_in, departure)
    else:
        assert response.status_code == 409, response.text
        assert stay_dates(engine, booking) == days(check_in, check_out)
        assert api.get(url(hotel, booking)).json()["status"] == "checked_in"


def test_today_is_the_hotels_not_utcs(api: TestClient, hotel: str) -> None:
    """At 22:30 UTC on the 20th it is already the 21st in Athens: a departure on the 21st is
    today's, not tomorrow's, and the 28-day bound counts back from the 21st."""
    booking = book(api, hotel, D(2026, 8, 20), D(2026, 9, 24))

    assert preview(api, hotel, booking).json()["departure_date"] == str(TODAY)
    assert preview(api, hotel, booking, TODAY - dt.timedelta(days=28)).status_code == 200
    assert depart(api, hotel, booking, TODAY).status_code == 200


# --- the status policy ------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "confirmed", "checked_out", "cancelled", "no_show"])
def test_only_a_checked_in_stay_departs_early(
    api: TestClient, hotel: str, engine: Engine, status: str
) -> None:
    booking = book(api, hotel, status=status)

    refused(
        depart(api, hotel, booking),
        f"Only a checked-in stay can depart early; this booking is {status}.",
    )
    refused(preview(api, hotel, booking, DEPARTURE), "Only a checked-in stay can depart early")
    assert stay_dates(engine, booking) == days(CHECK_IN, CHECK_OUT)


def test_a_repeat_is_refused_and_changes_nothing(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    """Not idempotent, like the extension: the second attempt finds the booking checked out."""
    booking = book(api, hotel)
    assert depart(api, hotel, booking).status_code == 200

    refused(
        depart(api, hotel, booking),
        "Only a checked-in stay can depart early; this booking is checked_out.",
    )
    assert stay_dates(engine, booking) == days(CHECK_IN, DEPARTURE)
    assert [action for action, _ in audit_events(engine, booking)].count(
        AuditAction.BOOKING_STAY_MODIFIED.value
    ) == 1


def test_a_plain_check_out_before_the_planned_day_is_refused(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    booking = book(api, hotel, check_out=TODAY + dt.timedelta(days=1))

    response = api.patch(url(hotel, booking), json={"status": "checked_out"})

    refused(response, f"This stay is planned to check out on {TODAY + dt.timedelta(days=1)}")
    assert "early departure" in response.json()["error"]["message"]
    assert api.get(url(hotel, booking)).json()["status"] == "checked_in"
    assert stay_dates(engine, booking) == days(CHECK_IN, TODAY + dt.timedelta(days=1))


@pytest.mark.parametrize("check_out", [TODAY, TODAY - dt.timedelta(days=2)], ids=["today", "past"])
def test_a_plain_check_out_on_or_after_the_planned_day_is_unchanged(
    api: TestClient, hotel: str, engine: Engine, check_out: dt.date
) -> None:
    booking = book(api, hotel, check_out=check_out)

    response = api.patch(url(hotel, booking), json={"status": "checked_out"})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "checked_out"
    assert response.json()["check_out_date"] == str(check_out)
    assert stay_dates(engine, booking) == days(CHECK_IN, check_out)


# --- the preview ------------------------------------------------------------------------------


def test_the_preview_states_what_the_departure_then_does(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    booking = book(api, hotel)
    charge(api, hotel, booking, "1200.00")

    previewed = preview(api, hotel, booking, DEPARTURE)

    assert previewed.status_code == 200, previewed.text
    body = previewed.json()
    assert body["departure_date"] == str(DEPARTURE)
    assert body["earliest_departure_date"] == str(CHECK_IN + dt.timedelta(days=1))
    assert body["latest_departure_date"] == str(TODAY)
    assert body["planned_check_out_date"] == str(CHECK_OUT)
    assert body["nights_removed"] == 5
    # Nothing was written.
    assert stay_dates(engine, booking) == days(CHECK_IN, CHECK_OUT)
    assert audit_events(engine, booking)[-1][0] != AuditAction.BOOKING_STAY_MODIFIED.value
    assert depart(api, hotel, booking).json()["repricing"] == body["repricing"]


def test_the_preview_range_is_bounded_by_the_stay_and_by_the_lookback(
    api: TestClient, hotel: str
) -> None:
    booking = book(api, hotel, D(2026, 8, 1), D(2026, 9, 25))

    body = preview(api, hotel, booking).json()

    assert body["departure_date"] == str(TODAY)
    assert body["earliest_departure_date"] == str(TODAY - dt.timedelta(days=28))
    assert body["latest_departure_date"] == str(TODAY)


# --- the audit trail and the transaction ------------------------------------------------------


def test_both_events_are_recorded(api: TestClient, hotel: str, engine: Engine) -> None:
    booking = book(api, hotel)
    charge(api, hotel, booking, "1200.00")

    depart(api, hotel, booking)

    events = audit_events(engine, booking)
    stay = [d for a, d in events if a == AuditAction.BOOKING_STAY_MODIFIED.value]
    status = [d for a, d in events if a == AuditAction.BOOKING_STATUS_CHANGED.value]
    assert stay == [
        {
            "changed_fields": ["check_out_date"],
            "previous_check_out_date": str(CHECK_OUT),
            "check_out_date": str(DEPARTURE),
            "nights_removed": 5,
            "rooms": 1,
            "previous_amount": "1200.00",
            "new_amount": "600.00",
            "difference": "-600.00",
            "currency": "EUR",
        }
    ]
    assert status == [{"old_status": "checked_in", "new_status": "checked_out"}]


def test_a_failure_rolls_everything_back(
    api: TestClient, hotel: str, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second audit event fails: the nights, the dates, the status and the first event all
    go with it."""
    booking = book(api, hotel)
    before = audit_events(engine, booking)
    record = AuditTrail.record

    def failing(self: AuditTrail, action: AuditAction, *args: Any, **kwargs: Any) -> Any:
        if action is AuditAction.BOOKING_STATUS_CHANGED:
            raise IntegrityError("INSERT INTO audit_events", {}, Exception("injected"))
        return record(self, action, *args, **kwargs)

    monkeypatch.setattr(AuditTrail, "record", failing)

    response = depart(api, hotel, booking)

    assert response.status_code >= 400, response.text
    body = api.get(url(hotel, booking)).json()
    assert (body["status"], body["check_out_date"]) == ("checked_in", str(CHECK_OUT))
    assert stay_dates(engine, booking) == days(CHECK_IN, CHECK_OUT)
    assert audit_events(engine, booking) == before


def test_a_departure_waits_for_a_concurrent_change_and_judges_what_it_committed(
    api: TestClient, hotel: str, engine: Engine
) -> None:
    """Another transaction checks the guest out while the departure is in flight. The
    departure takes the booking's row lock before it reads the status, so it waits, then
    sees ``checked_out`` and refuses -- it does not remove nights from a stay it never saw."""
    booking = book(api, hotel)
    locked = threading.Event()

    def check_out_concurrently() -> None:
        with engine.connect() as connection:
            connection.execute(
                sa.text("UPDATE bookings SET status = 'checked_out' WHERE public_id = :b"),
                {"b": booking},
            )
            locked.set()
            threading.Event().wait(1.0)
            connection.commit()

    other = threading.Thread(target=check_out_concurrently)
    other.start()
    assert locked.wait(10)
    response = depart(api, hotel, booking)
    other.join(10)

    refused(response, "Only a checked-in stay can depart early; this booking is checked_out.")
    assert stay_dates(engine, booking) == days(CHECK_IN, CHECK_OUT)
