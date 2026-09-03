"""Payment domain against real PostgreSQL.

What only the database can prove here: that the partial unique index gives webhook
idempotency (and stays inert when there is no reference), that the biconditional CHECK
constraints hold, and that migration 0002's ``public_id`` is unique and server-assigned.

Several tests below assert behaviour the schema *permits* but a finance team might not want --
over-refunding, refunding a refund. They are written as findings, not as endorsements: the
brief forbade inventing financial rules, so the tests record what the frozen schema actually
does. Each is called out in the completion report.

SQLite is not substituted.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Payment
from tests.integration.conftest import authenticated_client, requires_postgres

#: One account per suite, so a failure in one cannot be caused by another's state.
SUITE_EMAIL = "payments@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2026, 9, 1)
CHECK_OUT = dt.date(2026, 9, 4)
CARD_LAST_FOUR = "4242"
TXN_REFERENCE = "pi_3ABCdefGHIjklMNO"


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


def charge_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"amount": "100.00", "currency": "EUR", "method": "card"}
    body.update(overrides)
    return body


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    # Stage 4.2: every hotel-scoped endpoint requires an authenticated MEMBER, so
    # the suite's client carries a token. `POST /hotels` grants its creator `owner`,
    # which is why suites that build their own hotels need nothing further.
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build_booking(api: TestClient, slug: str = "hotel-a") -> tuple[str, str]:
    """A hotel with a room, a guest and one confirmed booking. Returns (hotel, booking)."""
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
    api.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": "101"})
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    booking = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": "confirmed",
            "total_amount": "360.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [
                        {"stay_date": str(CHECK_IN + dt.timedelta(days=n)), "rate": "120.00"}
                        for n in range(3)
                    ],
                }
            ],
        },
    ).json()["public_id"]
    return hotel, str(booking)


def payments_url(hotel: str, booking: str) -> str:
    return f"/api/v1/hotels/{hotel}/bookings/{booking}/payments"


@pytest.fixture
def booking_ctx(api: TestClient) -> tuple[str, str]:
    return build_booking(api)


# --- charge creation --------------------------------------------------------------------------


def test_create_charge_returns_201(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx

    response = api.post(payments_url(hotel, booking), json=charge_payload())
    body = response.json()

    assert response.status_code == 201
    assert body["kind"] == "charge"
    assert body["status"] == "pending"
    assert Decimal(body["amount"]) == Decimal("100.00")
    assert body["refunds_public_id"] is None
    assert uuid.UUID(body["public_id"])  # server-assigned by migration 0002


def test_the_charge_is_persisted(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    hotel, booking = booking_ctx
    api.post(payments_url(hotel, booking), json=charge_payload())

    stored = session.scalars(sa.select(Payment)).one()
    assert stored.kind == "charge"
    assert stored.refunded_payment_id is None
    assert stored.public_id is not None


def test_a_captured_charge_requires_paid_at(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx

    rejected = api.post(payments_url(hotel, booking), json=charge_payload(status="captured"))
    assert rejected.status_code == 422

    accepted = api.post(
        payments_url(hotel, booking),
        json=charge_payload(status="captured", paid_at="2026-09-01T12:00:00Z"),
    )
    assert accepted.status_code == 201


def test_every_schema_method_is_accepted(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx

    for method in ["card", "cash", "bank_transfer", "online_gateway", "ota_collect", "voucher"]:
        response = api.post(payments_url(hotel, booking), json=charge_payload(method=method))
        assert response.status_code == 201, method


def test_card_last_four_is_stored_and_returned(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    hotel, booking = booking_ctx

    body = api.post(
        payments_url(hotel, booking), json=charge_payload(card_last_four=CARD_LAST_FOUR)
    ).json()

    assert body["card_last_four"] == CARD_LAST_FOUR


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", "0"),
        ("amount", "-1.00"),
        ("currency", "EU"),
        ("method", "paypal"),
        ("status", "settled"),
        ("card_last_four", "12345"),
    ],
)
def test_invalid_charge_payloads_return_422(
    api: TestClient, booking_ctx: tuple[str, str], field: str, value: object
) -> None:
    hotel, booking = booking_ctx

    response = api.post(payments_url(hotel, booking), json=charge_payload(**{field: value}))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- idempotency: the partial unique index --------------------------------------------------------


def test_a_duplicate_provider_reference_returns_409(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """Payment providers deliver webhooks at least once; a repeat must not create a second
    record."""
    hotel, booking = booking_ctx
    payload = charge_payload(provider="stripe", transaction_reference=TXN_REFERENCE)
    api.post(payments_url(hotel, booking), json=payload)

    response = api.post(payments_url(hotel, booking), json=payload)
    body = response.json()

    assert response.status_code == 409
    assert body["error"]["code"] == "CONFLICT"
    assert "already been recorded" in body["error"]["message"]


def test_the_duplicate_is_not_written(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    hotel, booking = booking_ctx
    payload = charge_payload(provider="stripe", transaction_reference=TXN_REFERENCE)
    api.post(payments_url(hotel, booking), json=payload)
    api.post(payments_url(hotel, booking), json=payload)

    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1


def test_the_index_is_partial_so_many_payments_may_carry_no_reference(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """WHERE transaction_reference IS NOT NULL -- cash payments are unconstrained."""
    hotel, booking = booking_ctx

    for _ in range(3):
        assert (
            api.post(payments_url(hotel, booking), json=charge_payload(method="cash")).status_code
            == 201
        )

    assert api.get(payments_url(hotel, booking)).json()["total"] == 3


def test_the_same_reference_from_a_different_provider_is_allowed(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """The index spans BOTH columns, so the pair is what must be unique."""
    hotel, booking = booking_ctx
    api.post(
        payments_url(hotel, booking),
        json=charge_payload(provider="stripe", transaction_reference=TXN_REFERENCE),
    )

    response = api.post(
        payments_url(hotel, booking),
        json=charge_payload(provider="adyen", transaction_reference=TXN_REFERENCE),
    )

    assert response.status_code == 201


def test_the_idempotency_conflict_leaks_no_sql_or_reference(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    hotel, booking = booking_ctx
    payload = charge_payload(provider="stripe", transaction_reference=TXN_REFERENCE)
    api.post(payments_url(hotel, booking), json=payload)

    text = api.post(payments_url(hotel, booking), json=payload).text

    assert TXN_REFERENCE not in text
    for leak in [
        "uq_payments_provider_transaction_reference",
        "insert",
        "psycopg",
        "sqlalchemy",
        "23505",
        "DETAIL:",
    ]:
        assert leak.lower() not in text.lower(), f"leaked {leak!r}"


# --- retrieval and listing ------------------------------------------------------------------------


def test_get_payment_by_public_id(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx
    created = api.post(payments_url(hotel, booking), json=charge_payload()).json()

    response = api.get(f"{payments_url(hotel, booking)}/{created['public_id']}")

    assert response.status_code == 200
    assert response.json()["public_id"] == created["public_id"]


def test_url_rebuilt_from_the_response_resolves(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    hotel, booking = booking_ctx
    body = api.post(payments_url(hotel, booking), json=charge_payload()).json()

    rebuilt = (
        f"/api/v1/hotels/{body['hotel_public_id']}"
        f"/bookings/{body['booking_public_id']}/payments/{body['public_id']}"
    )
    assert api.get(rebuilt).status_code == 200


def test_list_uses_the_shared_envelope(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx
    api.post(payments_url(hotel, booking), json=charge_payload())

    body = api.get(payments_url(hotel, booking)).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] == 1


def test_list_is_ordered_oldest_first(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx
    amounts = ["10.00", "20.00", "30.00"]
    for amount in amounts:
        api.post(payments_url(hotel, booking), json=charge_payload(amount=amount))

    listed = [i["amount"] for i in api.get(payments_url(hotel, booking)).json()["items"]]

    assert listed == amounts
    assert listed == [i["amount"] for i in api.get(payments_url(hotel, booking)).json()["items"]]


def test_pagination_splits_results(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx
    for index in range(5):
        api.post(payments_url(hotel, booking), json=charge_payload(amount=f"{index + 1}.00"))

    first = api.get(payments_url(hotel, booking), params={"page": 1, "page_size": 2}).json()
    third = api.get(payments_url(hotel, booking), params={"page": 3, "page_size": 2}).json()

    assert [first["total"], first["pages"]] == [5, 3]
    assert len(third["items"]) == 1


def test_missing_payment_returns_404(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx

    response = api.get(f"{payments_url(hotel, booking)}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Payment not found for this booking."


def test_missing_booking_returns_404(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, _ = booking_ctx

    response = api.get(payments_url(hotel, str(uuid.uuid4())))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Booking not found for this hotel."


def test_missing_hotel_returns_404(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    _, booking = booking_ctx

    response = api.get(payments_url(str(uuid.uuid4()), booking))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Hotel not found."


# --- refunds --------------------------------------------------------------------------------------


def test_create_refund_returns_201_and_names_its_parent(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    hotel, booking = booking_ctx
    charge = api.post(payments_url(hotel, booking), json=charge_payload()).json()

    response = api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(amount="40.00", refunds_public_id=charge["public_id"]),
    )
    body = response.json()

    assert response.status_code == 201
    assert body["kind"] == "refund"
    assert body["refunds_public_id"] == charge["public_id"]
    assert Decimal(body["amount"]) == Decimal("40.00")


def test_the_refund_is_linked_in_the_database(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    hotel, booking = booking_ctx
    charge = api.post(payments_url(hotel, booking), json=charge_payload()).json()
    api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(amount="40.00", refunds_public_id=charge["public_id"]),
    )

    rows = session.scalars(sa.select(Payment).order_by(Payment.id)).all()
    assert len(rows) == 2
    assert rows[1].kind == "refund"
    assert rows[1].refunded_payment_id == rows[0].id


def test_a_refund_amount_is_positive_not_negative(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """ck_payments_amount_positive applies to refunds too; direction lives in `kind`."""
    hotel, booking = booking_ctx
    charge = api.post(payments_url(hotel, booking), json=charge_payload()).json()

    response = api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(amount="-40.00", refunds_public_id=charge["public_id"]),
    )

    assert response.status_code == 422


def test_refunding_an_unknown_payment_returns_404(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    hotel, booking = booking_ctx

    response = api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(refunds_public_id=str(uuid.uuid4())),
    )

    assert response.status_code == 404
    assert "was not found on this booking" in response.json()["error"]["message"]


def test_a_refund_cannot_reference_a_payment_on_another_booking(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    """refunded_payment_id is a SINGLE-column FK with no booking or hotel in it, so the
    database alone would allow this. The scoped lookup is what refuses it."""
    hotel_a, booking_a = booking_ctx
    hotel_b, booking_b = build_booking(api, "hotel-b")
    foreign_charge = api.post(payments_url(hotel_b, booking_b), json=charge_payload()).json()

    response = api.post(
        f"{payments_url(hotel_a, booking_a)}/refunds",
        json=charge_payload(refunds_public_id=foreign_charge["public_id"]),
    )

    assert response.status_code == 404
    # Only the original foreign charge exists; no refund was written.
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1


def test_a_rejected_refund_leaves_nothing_behind(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    hotel, booking = booking_ctx
    api.post(payments_url(hotel, booking), json=charge_payload())

    api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(refunds_public_id=str(uuid.uuid4())),
    )

    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1


# --- behaviour the schema PERMITS: recorded as findings, not endorsed -----------------------------


def test_the_schema_permits_refunding_more_than_was_charged(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """FINDING. The only amount rule is ``amount > 0``; nothing caps a refund at the charge.

    Asserted so the gap is visible and regression-tracked. Adding the cap would be inventing
    a financial rule the frozen schema does not express.
    """
    hotel, booking = booking_ctx
    charge = api.post(payments_url(hotel, booking), json=charge_payload(amount="50.00")).json()

    response = api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(amount="500.00", refunds_public_id=charge["public_id"]),
    )

    assert response.status_code == 201


def test_the_schema_permits_refunding_a_refund(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """FINDING. ``refunded_payment_id`` references payments(id) -- any row, including a
    refund. The constraint is named ..._references_charge but only checks non-null."""
    hotel, booking = booking_ctx
    charge = api.post(payments_url(hotel, booking), json=charge_payload()).json()
    refund = api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(amount="40.00", refunds_public_id=charge["public_id"]),
    ).json()

    response = api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(amount="10.00", refunds_public_id=refund["public_id"]),
    )

    assert response.status_code == 201


# --- append-only ----------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["patch", "put", "delete"])
def test_payments_cannot_be_modified_or_removed(
    api: TestClient, booking_ctx: tuple[str, str], method: str
) -> None:
    """A financial record is corrected by a reversal, never by an edit."""
    hotel, booking = booking_ctx
    created = api.post(payments_url(hotel, booking), json=charge_payload()).json()
    url = f"{payments_url(hotel, booking)}/{created['public_id']}"

    call = getattr(api, method)
    response = call(url, json={"amount": "1.00"}) if method != "delete" else call(url)

    assert response.status_code == 405


def test_a_charge_with_a_refund_cannot_be_removed_by_the_database_either(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    """fk_payments_refunded_payment_id_payments is ON DELETE RESTRICT."""
    from sqlalchemy.exc import IntegrityError

    hotel, booking = booking_ctx
    charge = api.post(payments_url(hotel, booking), json=charge_payload()).json()
    api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(amount="40.00", refunds_public_id=charge["public_id"]),
    )

    charge_row = session.scalars(sa.select(Payment).where(Payment.kind == "charge")).one()
    with pytest.raises(IntegrityError):
        session.execute(sa.delete(Payment).where(Payment.id == charge_row.id))
        session.flush()
    session.rollback()


def test_deleting_a_booking_with_payments_is_refused(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """The Booking-domain RESTRICT from Stage 3B.6 still holds."""
    hotel, booking = booking_ctx
    api.post(payments_url(hotel, booking), json=charge_payload())

    assert api.delete(f"/api/v1/hotels/{hotel}/bookings/{booking}").status_code == 409


# --- hotel isolation ------------------------------------------------------------------------------


def test_a_payment_is_not_reachable_through_another_hotel(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    hotel_a, booking_a = booking_ctx
    hotel_b, booking_b = build_booking(api, "hotel-b")
    created = api.post(payments_url(hotel_a, booking_a), json=charge_payload()).json()
    pid = created["public_id"]

    assert api.get(f"{payments_url(hotel_a, booking_a)}/{pid}").status_code == 200
    assert api.get(f"{payments_url(hotel_b, booking_b)}/{pid}").status_code == 404
    # And the booking of hotel B is not reachable through hotel A either.
    assert api.get(payments_url(hotel_a, booking_b)).status_code == 404


def test_listing_never_contains_another_bookings_payments(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    hotel_a, booking_a = booking_ctx
    hotel_b, booking_b = build_booking(api, "hotel-b")
    api.post(payments_url(hotel_a, booking_a), json=charge_payload(amount="11.00"))
    api.post(payments_url(hotel_b, booking_b), json=charge_payload(amount="22.00"))

    assert [i["amount"] for i in api.get(payments_url(hotel_a, booking_a)).json()["items"]] == [
        "11.00"
    ]
    assert [i["amount"] for i in api.get(payments_url(hotel_b, booking_b)).json()["items"]] == [
        "22.00"
    ]


# --- identifiers and error hygiene ----------------------------------------------------------------


def test_response_exposes_no_internal_ids(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx
    charge = api.post(payments_url(hotel, booking), json=charge_payload()).json()
    refund = api.post(
        f"{payments_url(hotel, booking)}/refunds",
        json=charge_payload(amount="10.00", refunds_public_id=charge["public_id"]),
    ).json()

    for body in (charge, refund):
        for forbidden in ["id", "booking_id", "hotel_id", "refunded_payment_id"]:
            assert forbidden not in body


def test_response_schema_is_exactly_the_declared_contract(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    hotel, booking = booking_ctx
    body = api.post(payments_url(hotel, booking), json=charge_payload()).json()

    assert set(body) == {
        "hotel_public_id",
        "booking_public_id",
        "public_id",
        "kind",
        "amount",
        "currency",
        "method",
        "status",
        "paid_at",
        "provider",
        "transaction_reference",
        "refunds_public_id",
        "failure_reason",
        "card_last_four",
        "created_at",
        "updated_at",
    }


def test_public_ids_are_distinct_per_payment(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx
    ids = {
        api.post(payments_url(hotel, booking), json=charge_payload()).json()["public_id"]
        for _ in range(5)
    }

    assert len(ids) == 5


def test_money_survives_the_round_trip_as_exact_decimal(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    hotel, booking = booking_ctx

    body = api.post(payments_url(hotel, booking), json=charge_payload(amount="100.05")).json()

    assert Decimal(body["amount"]) == Decimal("100.05")


def test_logs_contain_no_card_fragment_or_transaction_reference(
    api: TestClient, booking_ctx: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """A payment's driver message carries the amount, the reference and the card fragment."""
    hotel, booking = booking_ctx
    payload = charge_payload(
        provider="stripe", transaction_reference=TXN_REFERENCE, card_last_four=CARD_LAST_FOUR
    )
    api.post(payments_url(hotel, booking), json=payload)

    with caplog.at_level(logging.DEBUG):
        api.post(payments_url(hotel, booking), json=payload)  # triggers the 409

    captured = "\n".join(
        record.getMessage() + str(record.exc_info or "") for record in caplog.records
    )
    for secret in [TXN_REFERENCE, CARD_LAST_FOUR]:
        assert secret not in captured, f"leaked {secret!r} into logs"


def test_errors_use_the_shared_envelope(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx

    body = api.get(f"{payments_url(hotel, booking)}/{uuid.uuid4()}").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_malformed_identifier_returns_422(api: TestClient, booking_ctx: tuple[str, str]) -> None:
    hotel, booking = booking_ctx

    assert api.get(f"{payments_url(hotel, booking)}/not-a-uuid").status_code == 422
