"""Financial repricing against real PostgreSQL.

Stage 4.5.24. The arithmetic is checked without a database in
``tests/backend/test_repricing_policy.py``. What can only be checked here is everything that
makes the arithmetic *safe*:

* that the ledger is genuinely untouched -- no payment written, none altered, none removed;
* that a refused repricing leaves the booking, its nights and its audit history exactly as
  they were, which is a claim about what a ROLLBACK did;
* that the tenant wall answers before any money is calculated;
* that repeating the request produces no second financial effect;
* that the row lock serialises two modifications rather than letting both measure from the
  same starting point.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import authenticated_client, requires_postgres

SUITE_EMAIL = "repricing@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 9, 10)
CHECK_OUT = dt.date(2026, 9, 14)  # four nights
NIGHTS = (CHECK_OUT - CHECK_IN).days
BASE = "100.00"


def hotel_payload(slug: str, currency: str = "EUR") -> dict[str, object]:
    return {
        "slug": slug,
        "name": f"Hotel {slug}",
        "address_line1": "1 Dionysiou Areopagitou",
        "city": "Athens",
        "country_code": "GR",
        "timezone": "Europe/Athens",
        "currency": currency,
    }


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build_hotel(
    api: TestClient,
    slug: str,
    *,
    base_price: str = BASE,
    currency: str = "EUR",
    rooms: tuple[str, ...] = ("101", "102"),
) -> str:
    hotel = str(api.post("/api/v1/hotels", json=hotel_payload(slug, currency)).json()["public_id"])
    api.post(
        f"/api/v1/hotels/{hotel}/room-types",
        json={
            "code": "DLX",
            "name": "Deluxe",
            "max_occupancy": 4,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": base_price,
            "currency": currency,
        },
    )
    for number in rooms:
        api.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": number})
    return hotel


def set_base_price(api: TestClient, hotel: str, price: str) -> None:
    response = api.patch(f"/api/v1/hotels/{hotel}/room-types/DLX", json={"base_price": price})
    assert response.status_code == 200, response.text


def nights_for(check_in: dt.date, check_out: dt.date) -> list[dict]:
    return [
        {"stay_date": str(check_in + dt.timedelta(days=n))}
        for n in range((check_out - check_in).days)
    ]


def make_booking(
    api: TestClient,
    hotel: str,
    *,
    rooms: tuple[str, ...] = ("101",),
    check_in: dt.date = CHECK_IN,
    check_out: dt.date = CHECK_OUT,
    status: str = "confirmed",
    currency: str = "EUR",
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
            "total_amount": "400.00",
            "currency": currency,
            "rooms": [
                {"room_number": number, "nights": nights_for(check_in, check_out)}
                for number in rooms
            ],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def stay_url(hotel: str, booking: str) -> str:
    return f"/api/v1/hotels/{hotel}/bookings/{booking}/stay"


def modify(
    api: TestClient,
    hotel: str,
    booking: str,
    check_in: dt.date = CHECK_IN,
    check_out: dt.date = CHECK_OUT,
    *,
    rooms: tuple[str, ...] = ("101",),
) -> Response:
    return api.patch(
        stay_url(hotel, booking),
        json={
            "check_in_date": str(check_in),
            "check_out_date": str(check_out),
            "rooms": [
                {"room_number": number, "nights": nights_for(check_in, check_out)}
                for number in rooms
            ],
        },
    )


def charge(api: TestClient, hotel: str, booking: str, amount: str, currency: str = "EUR") -> str:
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={
            "amount": amount,
            "currency": currency,
            "method": "card",
            "status": "captured",
            "paid_at": dt.datetime.now(dt.UTC).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"])


def repricing_of(response: Response) -> dict[str, object]:
    assert response.status_code == 200, response.text
    return dict(response.json()["repricing"])


def ledger_rows(session: Session, slug: str) -> list[tuple]:
    """Every payment posted at one hotel, read straight from the table."""
    session.rollback()
    rows = session.execute(
        sa.text(
            "SELECT p.public_id, p.kind, p.amount, p.status, p.refunded_payment_id"
            " FROM payments p JOIN hotels h ON h.id = p.hotel_id"
            " WHERE h.slug = :slug ORDER BY p.id"
        ),
        {"slug": slug},
    ).all()
    return [tuple(row) for row in rows]


def night_rates(session: Session, slug: str) -> list[Decimal]:
    session.rollback()
    return list(
        session.execute(
            sa.text(
                "SELECT n.rate FROM booking_room_nights n JOIN hotels h ON h.id = n.hotel_id"
                " WHERE h.slug = :slug ORDER BY n.stay_date"
            ),
            {"slug": slug},
        ).scalars()
    )


# ======================================================================================
# A. No price change
# ======================================================================================


def test_a_modification_at_the_same_rate_adjusts_nothing(api: TestClient) -> None:
    hotel = build_hotel(api, "rp-none")
    booking = make_booking(api, hotel)

    result = repricing_of(modify(api, hotel, booking))

    assert result["difference"] == "0.00"
    assert result["adjustment"] == "none"
    assert result["previous_total"] == result["new_total"] == "400.00"


# ======================================================================================
# B. Price increase
# ======================================================================================


def test_a_dearer_stay_reports_the_amount_due(api: TestClient) -> None:
    hotel = build_hotel(api, "rp-up")
    booking = make_booking(api, hotel)
    set_base_price(api, hotel, "150.00")

    result = repricing_of(modify(api, hotel, booking))

    assert result["previous_total"] == "400.00"
    assert result["new_total"] == "600.00"
    assert result["difference"] == "200.00"
    assert result["additional_amount_due"] == "200.00"
    assert result["refundable_amount"] == "0.00"
    assert result["adjustment"] == "amount_due"


def test_an_increase_charges_nothing(api: TestClient, session: Session) -> None:
    """The ledger is untouched. The platform owns no payment processor and this stage does
    not pretend otherwise -- an amount due is a statement, not a charge."""
    hotel = build_hotel(api, "rp-up-nocharge")
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "400.00")
    before = ledger_rows(session, "rp-up-nocharge")
    set_base_price(api, hotel, "150.00")

    modify(api, hotel, booking)

    assert ledger_rows(session, "rp-up-nocharge") == before


# ======================================================================================
# C. Price decrease
# ======================================================================================


def test_a_cheaper_stay_that_was_paid_for_reports_a_refundable_amount(
    api: TestClient,
) -> None:
    hotel = build_hotel(api, "rp-down")
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "400.00")
    set_base_price(api, hotel, "50.00")

    result = repricing_of(modify(api, hotel, booking))

    assert result["difference"] == "-200.00"
    assert result["refundable_amount"] == "200.00"
    assert result["additional_amount_due"] == "0.00"
    assert result["adjustment"] == "refundable"


def test_a_decrease_refunds_nothing(api: TestClient, session: Session) -> None:
    hotel = build_hotel(api, "rp-down-norefund")
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "400.00")
    before = ledger_rows(session, "rp-down-norefund")
    set_base_price(api, hotel, "50.00")

    modify(api, hotel, booking)

    assert ledger_rows(session, "rp-down-norefund") == before
    assert not [row for row in ledger_rows(session, "rp-down-norefund") if row[1] == "refund"]


def test_a_cheaper_unpaid_stay_refunds_nothing_and_says_so(api: TestClient) -> None:
    """The cap, end to end: nothing was collected, so nothing became refundable -- what
    changed is the balance owed."""
    hotel = build_hotel(api, "rp-down-unpaid")
    booking = make_booking(api, hotel)
    set_base_price(api, hotel, "50.00")

    result = repricing_of(modify(api, hotel, booking))

    assert result["difference"] == "-200.00"
    assert result["refundable_amount"] == "0.00"
    assert result["adjustment"] == "none"
    assert result["outstanding_after"] == "200.00"


# ======================================================================================
# D & E. Existing payments and refund integrity
# ======================================================================================


@pytest.mark.parametrize(
    ("paid", "expected_refundable"),
    [("400.00", "200.00"), ("120.00", "120.00"), ("600.00", "200.00")],
    ids=["paid-in-full", "partially-paid", "overpaid"],
)
def test_the_refundable_amount_never_exceeds_what_was_collected(
    api: TestClient, paid: str, expected_refundable: str
) -> None:
    hotel = build_hotel(api, f"rp-cap-{paid.replace('.', '')}")
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, paid)
    set_base_price(api, hotel, "50.00")

    result = repricing_of(modify(api, hotel, booking))

    assert result["refundable_amount"] == expected_refundable


def test_a_previous_refund_lowers_what_is_refundable_again(api: TestClient) -> None:
    """``net_paid`` is charged minus refunded, so money already returned cannot be offered a
    second time. Over-refunding is what this prevents."""
    hotel = build_hotel(api, "rp-prior-refund")
    booking = make_booking(api, hotel)
    reference = charge(api, hotel, booking, "400.00")
    refunded = api.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments/refunds",
        json={
            "refunds_public_id": reference,
            "amount": "350.00",
            "currency": "EUR",
            "method": "card",
        },
    )
    assert refunded.status_code == 201, refunded.text
    set_base_price(api, hotel, "50.00")

    result = repricing_of(modify(api, hotel, booking))

    assert result["difference"] == "-200.00"
    assert result["refundable_amount"] == "50.00"


def test_historical_payments_are_never_altered(api: TestClient, session: Session) -> None:
    hotel = build_hotel(api, "rp-history")
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "400.00")
    before = ledger_rows(session, "rp-history")
    set_base_price(api, hotel, "250.00")

    modify(api, hotel, booking)
    modify(api, hotel, booking, CHECK_IN, CHECK_OUT + dt.timedelta(days=1))

    assert ledger_rows(session, "rp-history") == before


# ======================================================================================
# F. Currency
# ======================================================================================


def test_a_booking_whose_ledger_is_in_another_currency_cannot_be_repriced(
    api: TestClient, session: Session
) -> None:
    """A difference measured against a ledger that adds EUR to USD is a number that means
    nothing. Refused before anything moves, in the same words reconciliation uses."""
    hotel = build_hotel(api, "rp-currency")
    booking = make_booking(api, hotel)
    session.rollback()
    session.execute(
        sa.text(
            "INSERT INTO payments (booking_id, hotel_id, kind, amount, currency, method,"
            " status, paid_at)"
            " SELECT b.id, b.hotel_id, 'charge', 10, 'USD', 'card', 'captured', now()"
            " FROM bookings b JOIN hotels h ON h.id = b.hotel_id WHERE h.slug = :slug"
        ),
        {"slug": "rp-currency"},
    )
    session.commit()
    before = night_rates(session, "rp-currency")

    response = modify(api, hotel, booking)

    assert response.status_code == 409, response.text
    assert set(response.json()) == {"error"}
    assert night_rates(session, "rp-currency") == before


def test_the_currency_refusal_names_no_internals(api: TestClient, session: Session) -> None:
    hotel = build_hotel(api, "rp-currency-leak")
    booking = make_booking(api, hotel)
    session.rollback()
    session.execute(
        sa.text(
            "INSERT INTO payments (booking_id, hotel_id, kind, amount, currency, method,"
            " status, paid_at)"
            " SELECT b.id, b.hotel_id, 'charge', 10, 'USD', 'card', 'captured', now()"
            " FROM bookings b JOIN hotels h ON h.id = b.hotel_id WHERE h.slug = :slug"
        ),
        {"slug": "rp-currency-leak"},
    )
    session.commit()

    rendered = str(modify(api, hotel, booking).json())

    # "payments" is deliberately absent from this list: the refusal says the booking "has
    # payments in USD", which is the English plural and not the table it happens to match.
    for forbidden in ["booking_id", "hotel_id", "SELECT", "sqlalchemy", "psycopg", "public_id"]:
        assert forbidden not in rendered, f"the refusal leaked {forbidden!r}"


# ======================================================================================
# G. Status policy
# ======================================================================================


@pytest.mark.parametrize("status", ["pending", "confirmed"])
def test_a_modifiable_booking_may_be_repriced(api: TestClient, status: str) -> None:
    hotel = build_hotel(api, f"rp-status-{status}")
    booking = make_booking(api, hotel, status=status)

    assert modify(api, hotel, booking).status_code == 200


@pytest.mark.parametrize("status", ["checked_in", "checked_out", "cancelled", "no_show"])
def test_a_booking_past_arrival_cannot_be_repriced(api: TestClient, status: str) -> None:
    """The status policy is the one modify-stay already had: repricing rides on the
    modification and inherits its gate rather than inventing a second one."""
    hotel = build_hotel(api, f"rp-status-{status.replace('_', '')}")
    booking = make_booking(api, hotel)
    moved = api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_in"})
    assert moved.status_code == 200, moved.text
    if status != "checked_in":
        api.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": status})

    response = modify(api, hotel, booking)

    assert response.status_code == 409, response.text


# ======================================================================================
# H. Concurrency
# ======================================================================================


def test_two_simultaneous_repricings_serialise_on_the_booking_lock(
    api: TestClient, engine: Engine, session: Session
) -> None:
    """Both measure the value, both apply -- but one after the other, because the lock is
    taken before the old value is read. Without it both would measure the same starting
    point and the second would report a difference that never happened."""
    hotel = build_hotel(api, "rp-concurrent")
    booking = make_booking(api, hotel)
    set_base_price(api, hotel, "150.00")
    both_ready = threading.Barrier(2, timeout=30)
    results: dict[int, tuple[int, str]] = {}

    def attempt(index: int) -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        both_ready.wait()
        response = client.patch(
            stay_url(hotel, booking),
            json={
                "check_in_date": str(CHECK_IN),
                "check_out_date": str(CHECK_OUT),
                "rooms": [{"room_number": "101", "nights": nights_for(CHECK_IN, CHECK_OUT)}],
            },
        )
        body = response.json()
        results[index] = (
            response.status_code,
            body["repricing"]["difference"] if response.status_code == 200 else "",
        )

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    assert sorted(code for code, _ in results.values()) == [200, 200]
    # Serialised, so exactly one saw the 400 -> 600 move and the other saw 600 -> 600.
    assert sorted(difference for _, difference in results.values()) == ["0.00", "200.00"]
    assert night_rates(session, "rp-concurrent") == [Decimal("150.00")] * NIGHTS


# ======================================================================================
# I & J. Tenant isolation and authorization
# ======================================================================================


def test_a_booking_at_another_hotel_cannot_be_repriced(api: TestClient) -> None:
    first = build_hotel(api, "rp-tenant-a")
    second = build_hotel(api, "rp-tenant-b", base_price="300.00")
    booking = make_booking(api, second)

    response = modify(api, first, booking)

    assert response.status_code == 404, response.text
    assert set(response.json()) == {"error"}


def test_another_hotels_booking_is_not_repriced_by_this_hotels_rate_card(
    api: TestClient, session: Session
) -> None:
    """The non-vacuity fixture: two properties, two rate cards, one booking. The refusal must
    leave the other property's nights exactly as they were."""
    first = build_hotel(api, "rp-cross-a")
    second = build_hotel(api, "rp-cross-b", base_price="300.00")
    booking = make_booking(api, second)
    before = night_rates(session, "rp-cross-b")

    assert modify(api, first, booking).status_code == 404
    assert night_rates(session, "rp-cross-b") == before
    assert before == [Decimal("300.00")] * NIGHTS


def test_a_non_member_meets_the_same_wall(api: TestClient, engine: Engine) -> None:
    hotel = build_hotel(api, "rp-stranger")
    booking = make_booking(api, hotel)
    stranger = authenticated_client(engine, email="rp-stranger@example.test")

    refused = stranger.patch(
        stay_url(hotel, booking),
        json={
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "rooms": [{"room_number": "101", "nights": nights_for(CHECK_IN, CHECK_OUT)}],
        },
    )
    invented = stranger.patch(
        stay_url(str(uuid.uuid4()), booking),
        json={
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "rooms": [{"room_number": "101", "nights": nights_for(CHECK_IN, CHECK_OUT)}],
        },
    )

    assert refused.status_code == 404
    assert refused.json() == invented.json()


def test_an_unauthenticated_caller_cannot_reprice(api: TestClient, engine: Engine) -> None:
    hotel = build_hotel(api, "rp-anon")
    booking = make_booking(api, hotel)
    from tests.integration.conftest import create_test_app

    anonymous = TestClient(create_test_app(engine))

    response = anonymous.patch(
        stay_url(hotel, booking),
        json={
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "rooms": [{"room_number": "101", "nights": nights_for(CHECK_IN, CHECK_OUT)}],
        },
    )

    assert response.status_code == 401


# ======================================================================================
# K. Rollback
# ======================================================================================


def test_a_refused_modification_leaves_the_money_and_the_history_alone(
    api: TestClient, session: Session
) -> None:
    """Failure injected at the exclusion constraint, AFTER the price was calculated.

    The second booking already holds room 102 for the window, so moving the first onto it is
    refused at INSERT -- past the point where the old value was read and the new one priced.
    Nothing may survive that: not the dates, not the nights, not the audit row.
    """
    hotel = build_hotel(api, "rp-rollback")
    first = make_booking(api, hotel, rooms=("101",))
    make_booking(api, hotel, rooms=("102",))
    charge(api, hotel, first, "400.00")
    set_base_price(api, hotel, "999.00")
    before_nights = night_rates(session, "rp-rollback")
    before_ledger = ledger_rows(session, "rp-rollback")
    session.rollback()
    before_events = session.execute(
        sa.text("SELECT count(*) FROM audit_events WHERE action = 'booking.stay_modified'")
    ).scalar()

    response = modify(api, hotel, first, rooms=("102",))

    assert response.status_code == 409, response.text
    assert night_rates(session, "rp-rollback") == before_nights
    assert ledger_rows(session, "rp-rollback") == before_ledger
    session.rollback()
    assert (
        session.execute(
            sa.text("SELECT count(*) FROM audit_events WHERE action = 'booking.stay_modified'")
        ).scalar()
        == before_events
    )


# ======================================================================================
# L & M. History and idempotency
# ======================================================================================


def test_repeating_the_same_modification_produces_no_second_adjustment(
    api: TestClient, session: Session
) -> None:
    """The second request is measured against what the first committed, so it reports no
    change -- and creates no financial effect, because there is none to create."""
    hotel = build_hotel(api, "rp-idempotent")
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "400.00")
    set_base_price(api, hotel, "150.00")

    first = repricing_of(modify(api, hotel, booking))
    second = repricing_of(modify(api, hotel, booking))

    assert first["difference"] == "200.00"
    assert second["difference"] == "0.00"
    assert second["adjustment"] == "none"
    assert night_rates(session, "rp-idempotent") == [Decimal("150.00")] * NIGHTS
    assert len(ledger_rows(session, "rp-idempotent")) == 1


def test_the_declared_total_is_never_rewritten(api: TestClient) -> None:
    """Approved decision 10: ``total_amount`` is the CONTRACTED figure and may legitimately
    differ from the nightly sum. Repricing does not recompute it, because the schema records
    nothing of the discounts and packages that difference may represent."""
    hotel = build_hotel(api, "rp-declared")
    booking = make_booking(api, hotel)
    set_base_price(api, hotel, "150.00")

    body = modify(api, hotel, booking).json()

    assert body["booking"]["total_amount"] == "400.00"
    assert body["repricing"]["new_total"] == "600.00"


def test_reconciliation_reports_the_repriced_stay(api: TestClient) -> None:
    """The authority is recomputed from the rows this stage rewrote -- no stale total."""
    hotel = build_hotel(api, "rp-recon")
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "400.00")
    set_base_price(api, hotel, "50.00")

    modify(api, hotel, booking)
    body = api.get(f"/api/v1/hotels/{hotel}/bookings/{booking}/reconciliation").json()

    assert Decimal(body["accommodation_total"]) == Decimal("200.00")
    assert Decimal(body["net_paid"]) == Decimal("400.00")
    assert body["payment_state"] == "overpaid"


# ======================================================================================
# N. Audit and leakage
# ======================================================================================


def test_the_modification_is_audited_with_both_amounts(api: TestClient, session: Session) -> None:
    hotel = build_hotel(api, "rp-audit")
    booking = make_booking(api, hotel)
    set_base_price(api, hotel, "150.00")

    modify(api, hotel, booking)

    session.rollback()
    details = session.execute(
        sa.text(
            "SELECT details FROM audit_events WHERE action = 'booking.stay_modified'"
            " ORDER BY id DESC LIMIT 1"
        )
    ).scalar_one()
    assert details["previous_amount"] == "400.00"
    assert details["new_amount"] == "600.00"
    assert details["difference"] == "200.00"
    assert details["currency"] == "EUR"


def test_the_audit_row_carries_no_payment_detail(api: TestClient, session: Session) -> None:
    hotel = build_hotel(api, "rp-audit-clean")
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "400.00")
    set_base_price(api, hotel, "150.00")

    modify(api, hotel, booking)

    session.rollback()
    details = session.execute(
        sa.text(
            "SELECT details FROM audit_events WHERE action = 'booking.stay_modified'"
            " ORDER BY id DESC LIMIT 1"
        )
    ).scalar_one()
    assert not {"card_last_four", "provider", "transaction_reference", "method"} & set(details)


def test_the_response_exposes_no_internal_identifier(api: TestClient) -> None:
    hotel = build_hotel(api, "rp-no-ids")
    booking = make_booking(api, hotel)

    body = modify(api, hotel, booking).json()

    assert not {"id", "hotel_id", "booking_id", "guest_id"} & set(body["booking"])
    assert not {"id", "booking_id", "payment_id"} & set(body["repricing"])


def test_the_response_never_claims_money_moved(api: TestClient) -> None:
    """The field names say what they are: an amount DUE and a REFUNDABLE amount, not a charge
    and not a refund. Nothing in the payload asserts a transaction happened."""
    hotel = build_hotel(api, "rp-wording")
    booking = make_booking(api, hotel)
    charge(api, hotel, booking, "400.00")
    set_base_price(api, hotel, "50.00")

    repricing = repricing_of(modify(api, hotel, booking))

    assert set(repricing) == {
        "currency",
        "previous_total",
        "new_total",
        "difference",
        "additional_amount_due",
        "refundable_amount",
        "outstanding_after",
        "adjustment",
    }
    assert not {"charged", "refunded", "paid", "captured"} & set(repricing)
