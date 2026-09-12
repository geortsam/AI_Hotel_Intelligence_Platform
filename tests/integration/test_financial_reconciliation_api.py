"""Booking financial reconciliation against real PostgreSQL.

Stage 4.5.9. The question this endpoint answers is what a booking is worth, what has been paid
against it, and what is left -- and the whole point is that the first of those comes from the
server rather than from whatever total the client declared.

**Why the authoritative figure is the sum of the nightly rates.** ``bookings.total_amount``
arrives in the create payload; nothing that makes a financial decision reads it. Its schema
comment (approved decision 10) says outright that it is the *contracted* figure and is NOT
defined as the sum of the nightly rates. ``booking_room_nights`` is what the model calls the
atomic financial unit, its rows are complete by constraint trigger, and its rates are
non-negative by CHECK. So the sum is derivable, deterministic and unforgeable, and the
declared total is reported beside it precisely so a divergence is visible rather than
resolved by fiat.

**Money is Decimal everywhere below.** The columns are ``Numeric(14, 2)``. Comparing through
``float`` would make a test that passes for the wrong reason, and 0.1 + 0.2 is the reason.

The whole chain -- booking, rooms, nights, charges, refunds -- is built through the real API
against the real database. Nothing here is mocked.
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
from sqlalchemy.orm import sessionmaker

from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    requires_postgres,
)

SUITE_EMAIL = "reconciliation@example.test"
OTHER_EMAIL = "reconciliation-other@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2027, 7, 1)
CHECK_OUT = dt.date(2027, 7, 4)  # three nights
NIGHTS = 3


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


def bookings_url(hotel: str) -> str:
    return f"/api/v1/hotels/{hotel}/bookings"


def reconciliation_url(hotel: str, booking: str) -> str:
    return f"{bookings_url(hotel)}/{booking}/reconciliation"


def payments_url(hotel: str, booking: str) -> str:
    return f"{bookings_url(hotel)}/{booking}/payments"


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def type_code_for(room_number: str) -> str:
    """Each room gets its own room type.

    Since Stage 4.5.23 a night is priced from its room type, so two rooms can only carry
    different rates if they are of different types -- which is how a hotel actually works.
    Giving every room its own type lets these tests keep saying 'room 101 at 120, room 102
    at 150' without inventing a second pricing path.
    """
    return f"T{room_number}"


def build_hotel(api: TestClient, slug: str, rooms: tuple[str, ...] = ("101", "102")) -> str:
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    for number in rooms:
        code = type_code_for(number)
        api.post(
            f"/api/v1/hotels/{hotel}/room-types",
            json={
                "code": code,
                "name": f"Type {code}",
                "max_occupancy": 4,
                "standard_occupancy": 2,
                "bed_count": 1,
                "base_price": "120.00",
                "currency": "EUR",
            },
        )
        api.post(f"/api/v1/hotels/{hotel}/room-types/{code}/rooms", json={"room_number": number})
    return hotel


def make_booking(
    api: TestClient,
    hotel: str,
    *,
    rooms: dict[str, str] | None = None,
    declared_total: str = "360.00",
    currency: str = "EUR",
) -> str:
    """A booking whose rooms are ``{room_number: nightly_rate}``, over three nights."""
    rooms = rooms or {"101": "120.00"}
    for number, rate in rooms.items():
        configured = api.patch(
            f"/api/v1/hotels/{hotel}/room-types/{type_code_for(number)}",
            json={"base_price": rate, "currency": currency},
        )
        assert configured.status_code == 200, configured.text
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    response = api.post(
        bookings_url(hotel),
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": "confirmed",
            "total_amount": declared_total,
            "currency": currency,
            "rooms": [
                {
                    "room_number": number,
                    "nights": [
                        {"stay_date": str(CHECK_IN + dt.timedelta(days=n))} for n in range(NIGHTS)
                    ],
                }
                for number, rate in rooms.items()
            ],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def charge(api: TestClient, hotel: str, booking: str, amount: str, **extra: object) -> dict:
    body: dict[str, object] = {"amount": amount, "currency": "EUR", "method": "card"}
    body.update(extra)
    response = api.post(payments_url(hotel, booking), json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


def refund(api: TestClient, hotel: str, booking: str, parent: str, amount: str) -> dict:
    response = api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json={
            "amount": amount,
            "currency": "EUR",
            "method": "card",
            "refunds_public_id": parent,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def summary(api: TestClient, hotel: str, booking: str) -> dict:
    response = api.get(reconciliation_url(hotel, booking))
    assert response.status_code == 200, response.text
    return dict(response.json())


@pytest.fixture
def hotel(api: TestClient) -> str:
    return build_hotel(api, "recon-hotel")


# ======================================================================================
# The authoritative total
# ======================================================================================


def test_one_room_one_night(api: TestClient) -> None:
    hotel = build_hotel(api, "recon-one-night")
    # The night is priced from the room type, so 99.00 is configured rather than sent.
    api.patch(
        f"/api/v1/hotels/{hotel}/room-types/{type_code_for('101')}",
        json={"base_price": "99.00"},
    )
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "L"}
    ).json()["public_id"]
    booking = api.post(
        bookings_url(hotel),
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_IN + dt.timedelta(days=1)),
            "status": "confirmed",
            "total_amount": "99.00",
            "currency": "EUR",
            "rooms": [{"room_number": "101", "nights": [{"stay_date": str(CHECK_IN)}]}],
        },
    ).json()["public_id"]

    assert Decimal(summary(api, hotel, booking)["accommodation_total"]) == Decimal("99.00")


def test_one_room_multiple_nights(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel, rooms={"101": "120.00"})

    assert Decimal(summary(api, hotel, booking)["accommodation_total"]) == Decimal("360.00")


def test_multiple_rooms_at_different_rates(api: TestClient, hotel: str) -> None:
    """The realistic case: two rooms, three nights each, different nightly rates."""
    booking = make_booking(api, hotel, rooms={"101": "120.00", "102": "150.00"})

    body = summary(api, hotel, booking)

    assert Decimal(body["accommodation_total"]) == Decimal("810.00")
    assert body["currency"] == "EUR"


def test_the_total_is_summed_at_two_decimal_places_without_drift(
    api: TestClient, hotel: str
) -> None:
    """A rate that would round badly through binary floating point.

    0.1 + 0.2 != 0.3 in float. Three nights of 33.33 must be exactly 99.99.
    """
    booking = make_booking(api, hotel, rooms={"101": "33.33"})

    total = summary(api, hotel, booking)["accommodation_total"]

    assert Decimal(total) == Decimal("99.99")
    assert total == "99.99"


def test_a_zero_rate_night_is_counted_as_zero_not_missing(api: TestClient, hotel: str) -> None:
    """A complimentary night has a real rate of zero; the sum must not skip the row."""
    booking = make_booking(api, hotel, rooms={"101": "0.00"})

    body = summary(api, hotel, booking)

    assert Decimal(body["accommodation_total"]) == Decimal("0.00")
    assert body["payment_state"] == "paid"  # nothing owed, nothing paid


# ======================================================================================
# The declared total has no authority
# ======================================================================================


def test_the_declared_total_does_not_change_the_authoritative_one(
    api: TestClient, hotel: str
) -> None:
    """The heart of the stage. A client claiming 10.00 for a 360.00 stay changes nothing."""
    booking = make_booking(api, hotel, rooms={"101": "120.00"}, declared_total="10.00")

    body = summary(api, hotel, booking)

    assert Decimal(body["accommodation_total"]) == Decimal("360.00")
    assert Decimal(body["declared_total"]) == Decimal("10.00")
    assert body["totals_agree"] is False


def test_an_inflated_declared_total_is_equally_powerless(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel, rooms={"101": "120.00"}, declared_total="99999.00")

    body = summary(api, hotel, booking)

    assert Decimal(body["accommodation_total"]) == Decimal("360.00")
    assert body["totals_agree"] is False


def test_agreeing_totals_are_reported_as_agreeing(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel, rooms={"101": "120.00"}, declared_total="360.00")

    assert summary(api, hotel, booking)["totals_agree"] is True


def test_outstanding_is_measured_against_the_server_figure(api: TestClient, hotel: str) -> None:
    """Paying the declared total in full leaves the real balance outstanding."""
    booking = make_booking(api, hotel, rooms={"101": "120.00"}, declared_total="10.00")
    charge(api, hotel, booking, "10.00")

    body = summary(api, hotel, booking)

    assert Decimal(body["outstanding_amount"]) == Decimal("350.00")
    assert body["payment_state"] == "partially_paid"


# ======================================================================================
# Charges, refunds and net paid
# ======================================================================================


def test_a_booking_with_no_payments(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    body = summary(api, hotel, booking)

    assert Decimal(body["charged_total"]) == Decimal("0.00")
    assert Decimal(body["refunded_total"]) == Decimal("0.00")
    assert Decimal(body["net_paid"]) == Decimal("0.00")
    assert Decimal(body["outstanding_amount"]) == Decimal("360.00")
    assert body["payment_state"] == "unpaid"


def test_multiple_charges_accumulate(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "100.00")
    charge(api, hotel, booking, "60.00")

    body = summary(api, hotel, booking)

    assert Decimal(body["charged_total"]) == Decimal("160.00")
    assert Decimal(body["net_paid"]) == Decimal("160.00")


@pytest.mark.parametrize("voided", ["failed", "cancelled"])
def test_a_voided_charge_is_not_money(api: TestClient, hotel: str, voided: str) -> None:
    """The same status vocabulary the refund cap uses, and for the same reason."""
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "100.00", status=voided)

    body = summary(api, hotel, booking)

    assert Decimal(body["charged_total"]) == Decimal("0.00")
    assert body["payment_state"] == "unpaid"


def test_a_captured_charge_counts(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "360.00", status="captured", paid_at="2027-07-01T10:00:00Z")

    body = summary(api, hotel, booking)

    assert Decimal(body["charged_total"]) == Decimal("360.00")
    assert body["payment_state"] == "paid"


def test_a_refund_reduces_net_paid(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    parent = charge(api, hotel, booking, "360.00")
    refund(api, hotel, booking, parent["public_id"], "60.00")

    body = summary(api, hotel, booking)

    assert Decimal(body["charged_total"]) == Decimal("360.00")
    assert Decimal(body["refunded_total"]) == Decimal("60.00")
    assert Decimal(body["net_paid"]) == Decimal("300.00")
    assert Decimal(body["outstanding_amount"]) == Decimal("60.00")
    assert body["payment_state"] == "partially_paid"


def test_multiple_partial_refunds_accumulate(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    parent = charge(api, hotel, booking, "360.00")
    refund(api, hotel, booking, parent["public_id"], "100.00")
    refund(api, hotel, booking, parent["public_id"], "50.00")

    body = summary(api, hotel, booking)

    assert Decimal(body["refunded_total"]) == Decimal("150.00")
    assert Decimal(body["net_paid"]) == Decimal("210.00")


def test_a_fully_refunded_booking_returns_to_unpaid(api: TestClient, hotel: str) -> None:
    """Net paid is zero again, and the state says so rather than clinging to 'paid'."""
    booking = make_booking(api, hotel)
    parent = charge(api, hotel, booking, "360.00")
    refund(api, hotel, booking, parent["public_id"], "360.00")

    body = summary(api, hotel, booking)

    assert Decimal(body["net_paid"]) == Decimal("0.00")
    assert Decimal(body["outstanding_amount"]) == Decimal("360.00")
    assert body["payment_state"] == "unpaid"


def test_a_refund_never_increases_net_paid(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    parent = charge(api, hotel, booking, "200.00")
    before = Decimal(summary(api, hotel, booking)["net_paid"])

    refund(api, hotel, booking, parent["public_id"], "50.00")

    assert Decimal(summary(api, hotel, booking)["net_paid"]) < before


def test_reconciliation_agrees_with_the_refund_cap(api: TestClient, hotel: str) -> None:
    """Both read the same definition of money, so they cannot drift apart.

    The cap refuses anything past 360; reconciliation then reports exactly 360 refunded.
    """
    booking = make_booking(api, hotel)
    parent = charge(api, hotel, booking, "360.00")
    refund(api, hotel, booking, parent["public_id"], "360.00")

    over = api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json={
            "amount": "0.01",
            "currency": "EUR",
            "method": "card",
            "refunds_public_id": parent["public_id"],
        },
    )

    assert over.status_code == 409
    assert Decimal(summary(api, hotel, booking)["refunded_total"]) == Decimal("360.00")


# ======================================================================================
# Payment state
# ======================================================================================


@pytest.mark.parametrize(
    ("paid", "expected", "outstanding"),
    [
        ("0.00", "unpaid", "360.00"),
        ("40.00", "partially_paid", "320.00"),
        ("359.99", "partially_paid", "0.01"),
        ("360.00", "paid", "0.00"),
        ("360.01", "overpaid", "-0.01"),
        ("500.00", "overpaid", "-140.00"),
    ],
)
def test_the_payment_state_matrix(
    api: TestClient, hotel: str, paid: str, expected: str, outstanding: str
) -> None:
    """Including both sides of each boundary, at one cent."""
    booking = make_booking(api, hotel)
    if Decimal(paid) > 0:
        charge(api, hotel, booking, paid)

    body = summary(api, hotel, booking)

    assert body["payment_state"] == expected
    assert Decimal(body["outstanding_amount"]) == Decimal(outstanding)


def test_overpayment_is_detected_and_reported_not_rejected(api: TestClient, hotel: str) -> None:
    """A charge larger than the stay is accepted -- the ledger is append-only and the API
    does not police provider settlement -- but the inconsistency is surfaced, which is the
    invariant this stage owes."""
    booking = make_booking(api, hotel)

    accepted = api.post(
        payments_url(hotel, booking),
        json={"amount": "1000.00", "currency": "EUR", "method": "card"},
    )
    body = summary(api, hotel, booking)

    assert accepted.status_code == 201
    assert body["payment_state"] == "overpaid"
    assert Decimal(body["outstanding_amount"]) == Decimal("-640.00")


# ======================================================================================
# Currency
# ======================================================================================


def test_a_foreign_currency_charge_makes_reconciliation_refuse_rather_than_add(
    api: TestClient, hotel: str
) -> None:
    """No FX is introduced. Adding EUR to USD would produce a number meaning nothing."""
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "100.00")
    api.post(
        payments_url(hotel, booking),
        json={"amount": "50.00", "currency": "USD", "method": "card"},
    )

    response = api.get(reconciliation_url(hotel, booking))

    assert response.status_code == 409
    assert "USD" in response.json()["error"]["message"]
    assert "No conversion" in response.json()["error"]["message"]


def test_a_single_currency_booking_reconciles_normally(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "100.00")

    assert api.get(reconciliation_url(hotel, booking)).status_code == 200


# ======================================================================================
# Tenant isolation and authorization
# ======================================================================================


def test_a_booking_at_another_hotel_is_not_found(api: TestClient, hotel: str) -> None:
    """404, byte-identical to a booking that does not exist: no financial oracle."""
    booking = make_booking(api, hotel)
    other = build_hotel(api, "recon-hotel-b")

    cross = api.get(reconciliation_url(other, booking))
    invented = api.get(reconciliation_url(other, str(uuid.uuid4())))

    assert cross.status_code == 404
    assert invented.status_code == 404
    assert cross.json() == invented.json()


def test_a_non_member_sees_no_financial_information(
    api: TestClient, engine: Engine, hotel: str
) -> None:
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "100.00")
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    response = stranger.get(reconciliation_url(hotel, booking))

    assert response.status_code == 404
    assert "100.00" not in response.text
    assert "360.00" not in response.text


def test_an_unauthenticated_caller_sees_no_financial_information(
    api: TestClient, hotel: str
) -> None:
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "100.00")
    anonymous = TestClient(api.app)

    response = anonymous.get(reconciliation_url(hotel, booking))

    assert response.status_code == 401
    assert "100.00" not in response.text


def test_a_viewer_may_read_the_summary(api: TestClient, engine: Engine, hotel: str) -> None:
    """It is a read, and reading payments already requires membership without a role.
    Inventing a stricter rule here would be a new permission model."""
    booking = make_booking(api, hotel)
    viewer = authenticated_client(engine, email=OTHER_EMAIL)
    grant_membership(engine, OTHER_EMAIL, hotel, "viewer")

    response = viewer.get(reconciliation_url(hotel, booking))

    assert response.status_code == 200
    assert Decimal(response.json()["accommodation_total"]) == Decimal("360.00")


def test_another_hotels_payments_are_not_counted(api: TestClient, hotel: str) -> None:
    """Two bookings at two hotels; each reconciles only its own ledger."""
    mine = make_booking(api, hotel)
    other_hotel = build_hotel(api, "recon-hotel-c")
    theirs = make_booking(api, other_hotel)
    charge(api, other_hotel, theirs, "999.00")

    body = summary(api, hotel, mine)

    assert Decimal(body["charged_total"]) == Decimal("0.00")


def test_two_bookings_at_one_hotel_do_not_share_a_ledger(api: TestClient, hotel: str) -> None:
    first = make_booking(api, hotel, rooms={"101": "120.00"})
    second = make_booking(api, hotel, rooms={"102": "150.00"})
    charge(api, hotel, first, "100.00")

    assert Decimal(summary(api, hotel, first)["charged_total"]) == Decimal("100.00")
    assert Decimal(summary(api, hotel, second)["charged_total"]) == Decimal("0.00")
    assert Decimal(summary(api, hotel, second)["accommodation_total"]) == Decimal("450.00")


# ======================================================================================
# Response shape and error hygiene
# ======================================================================================


def test_the_response_exposes_only_public_fields(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)

    body = summary(api, hotel, booking)

    assert set(body) == {
        "booking_public_id",
        "currency",
        "accommodation_total",
        "declared_total",
        "totals_agree",
        "charged_total",
        "refunded_total",
        "net_paid",
        "outstanding_amount",
        "payment_state",
    }
    assert body["booking_public_id"] == booking


def test_no_response_carries_an_internal_identifier(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "100.00")

    keys = set(summary(api, hotel, booking))

    assert "id" not in keys
    assert not {key for key in keys if key.endswith("_id") and key != "booking_public_id"}


def test_the_currency_conflict_leaks_no_internals(api: TestClient, hotel: str) -> None:
    booking = make_booking(api, hotel)
    api.post(
        payments_url(hotel, booking),
        json={"amount": "50.00", "currency": "USD", "method": "card"},
    )

    response = api.get(reconciliation_url(hotel, booking))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    # "payments" is deliberately NOT in this list. It is the public route segment the caller
    # just posted to, so its presence in a sentence about payments discloses nothing; an
    # assertion against it would fail on plain domain language rather than on a leak.
    for leak in [
        "SELECT",
        "booking_room_nights",
        "booking_rooms",
        "hotel_id",
        "booking_id",
        "refunded_payment_id",
        "sqlalchemy",
        "psycopg",
        "Traceback",
        "23505",
        "coalesce",
    ]:
        assert leak not in response.text, f"leaked {leak!r}"


def test_a_missing_booking_leaks_nothing(api: TestClient, hotel: str) -> None:
    response = api.get(reconciliation_url(hotel, str(uuid.uuid4())))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    for leak in ["SELECT", "booking_room_nights", "sqlalchemy", "Traceback"]:
        assert leak not in response.text


# ======================================================================================
# Query behaviour
# ======================================================================================


def test_the_summary_does_not_scale_its_queries_with_the_data(
    api: TestClient, engine: Engine, hotel: str
) -> None:
    """A fixed number of statements regardless of rooms, nights and payments.

    Counted by listening to the engine, so an N+1 introduced later fails here rather than
    quietly costing a query per payment.
    """
    booking = make_booking(api, hotel, rooms={"101": "120.00", "102": "150.00"})
    parent = charge(api, hotel, booking, "300.00")
    for _ in range(5):
        refund(api, hotel, booking, parent["public_id"], "10.00")
        charge(api, hotel, booking, "10.00")

    statements: list[str] = []

    def record(  # type: ignore[no-untyped-def]
        conn, cursor, statement, parameters, context, executemany
    ):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        assert api.get(reconciliation_url(hotel, booking)).status_code == 200
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # hotel, membership, booking (+ its two eager loads), nightly sum, ledger totals,
    # currencies. Bounded and small; the point is that it does not grow with 11 payments.
    assert len(selects) <= 10, f"{len(selects)} SELECTs: {selects}"
