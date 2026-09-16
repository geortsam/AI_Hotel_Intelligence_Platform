"""Refund integrity against real PostgreSQL.

Stage 4.5.8. Three rules that no constraint can express, because each spans more than one row
while a CHECK sees exactly one: a refund never exceeds what is still refundable on its parent,
a refund cannot reverse another refund, and a refund settles in the parent's currency.

**Why this suite has to hit a real database.** The cap is only as good as the lock that holds
the parent while it is computed. A test against a mock would prove the arithmetic and miss the
thing that actually goes wrong in production -- two requests reading the same balance and both
being right about it. The concurrency section below opens two genuine connections and makes
them contend, with a barrier so the interleaving is forced rather than hoped for.

Money is compared as ``Decimal`` throughout. The column is ``Numeric(14, 2)``; comparing
through ``float`` would make a test that passes for the wrong reason.
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

from app.models import Payment
from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    requires_postgres,
)

SUITE_EMAIL = "refund-integrity@example.test"
OTHER_EMAIL = "refund-integrity-other@example.test"

pytestmark = requires_postgres

CHECK_IN = dt.date(2027, 4, 5)
CHECK_OUT = dt.date(2027, 4, 8)


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


def payments_url(hotel: str, booking: str) -> str:
    return f"/api/v1/hotels/{hotel}/bookings/{booking}/payments"


def charge_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"amount": "100.00", "currency": "EUR", "method": "card"}
    body.update(overrides)
    return body


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build_booking(api: TestClient, slug: str = "refund-hotel") -> tuple[str, str]:
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
                        {"stay_date": str(CHECK_IN + dt.timedelta(days=n))} for n in range(3)
                    ],
                }
            ],
        },
    ).json()["public_id"]
    return hotel, str(booking)


@pytest.fixture
def booking_ctx(api: TestClient) -> tuple[str, str]:
    return build_booking(api)


def charge(api: TestClient, ctx: tuple[str, str], **overrides: object) -> dict:
    response = api.post(payments_url(*ctx), json=charge_payload(**overrides))
    assert response.status_code == 201, response.text
    return dict(response.json())


def refund(api: TestClient, ctx: tuple[str, str], parent: str, **overrides: object) -> Response:
    return api.post(
        f"{payments_url(*ctx)}/refunds",
        json=charge_payload(refunds_public_id=parent, **overrides),
    )


def refunded_total(session: Session, parent_public_id: str) -> Decimal:
    """Summed from the database, not from any response body."""
    parent_id = session.scalar(
        sa.select(Payment.id).where(Payment.public_id == uuid.UUID(parent_public_id))
    )
    total = session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(Payment.amount), 0)).where(
            Payment.refunded_payment_id == parent_id,
            Payment.status.not_in(("failed", "cancelled")),
        )
    )
    return Decimal(total or 0)


# ======================================================================================
# The cap
# ======================================================================================


def test_a_refund_smaller_than_the_charge_succeeds(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    parent = charge(api, booking_ctx, amount="100.00")

    response = refund(api, booking_ctx, parent["public_id"], amount="30.00")

    assert response.status_code == 201
    assert Decimal(response.json()["amount"]) == Decimal("30.00")


def test_a_refund_exactly_equal_to_the_charge_succeeds(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """The boundary. ``>`` and ``>=`` differ by exactly this case."""
    parent = charge(api, booking_ctx, amount="100.00")

    response = refund(api, booking_ctx, parent["public_id"], amount="100.00")

    assert response.status_code == 201


def test_a_refund_one_cent_over_the_charge_is_refused(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """The other side of the same boundary, at the smallest step the column can hold."""
    parent = charge(api, booking_ctx, amount="100.00")

    response = refund(api, booking_ctx, parent["public_id"], amount="100.01")

    assert response.status_code == 409


def test_a_second_refund_within_the_remaining_balance_succeeds(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    parent = charge(api, booking_ctx, amount="100.00")
    assert refund(api, booking_ctx, parent["public_id"], amount="30.00").status_code == 201

    response = refund(api, booking_ctx, parent["public_id"], amount="70.00")

    assert response.status_code == 201


def test_a_second_refund_exceeding_the_remaining_balance_is_refused(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    parent = charge(api, booking_ctx, amount="100.00")
    assert refund(api, booking_ctx, parent["public_id"], amount="30.00").status_code == 201

    response = refund(api, booking_ctx, parent["public_id"], amount="70.01")

    assert response.status_code == 409


def test_the_worked_example_from_the_brief(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    """Charge 100; refund 30, 40, 30 -> all succeed; a further 1 -> refused."""
    parent = charge(api, booking_ctx, amount="100.00")
    public_id = parent["public_id"]

    assert refund(api, booking_ctx, public_id, amount="30.00").status_code == 201
    assert refund(api, booking_ctx, public_id, amount="40.00").status_code == 201
    assert refund(api, booking_ctx, public_id, amount="30.00").status_code == 201

    assert refund(api, booking_ctx, public_id, amount="1.00").status_code == 409
    assert refunded_total(session, public_id) == Decimal("100.00")


def test_any_refund_against_a_fully_refunded_charge_is_refused(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    parent = charge(api, booking_ctx, amount="100.00")
    assert refund(api, booking_ctx, parent["public_id"], amount="100.00").status_code == 201

    response = refund(api, booking_ctx, parent["public_id"], amount="0.01")

    assert response.status_code == 409


def test_the_balance_is_per_parent_not_per_booking(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """Two charges on one booking each carry their own refundable amount."""
    first = charge(api, booking_ctx, amount="100.00")
    second = charge(api, booking_ctx, amount="50.00")

    assert refund(api, booking_ctx, first["public_id"], amount="100.00").status_code == 201
    assert refund(api, booking_ctx, second["public_id"], amount="50.00").status_code == 201
    assert refund(api, booking_ctx, second["public_id"], amount="0.01").status_code == 409


def test_a_voided_refund_does_not_consume_the_balance(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """A refund that failed moved no money, so it must not reduce what can still be refunded.

    This is the case the brief warns about: counting failed operations as financially
    refunded would silently strand money that was never returned.
    """
    parent = charge(api, booking_ctx, amount="100.00")
    assert (
        refund(api, booking_ctx, parent["public_id"], amount="100.00", status="failed").status_code
        == 201
    )

    response = refund(api, booking_ctx, parent["public_id"], amount="100.00")

    assert response.status_code == 201


def test_a_pending_refund_does_consume_the_balance(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """The other half of the same decision, and the one that makes the cap enforceable.

    Payments are append-only and nothing in this application can change a status after the
    fact, so a pending refund stays pending. If pending did not count, posting the same
    refund repeatedly would return the charge many times over without ever tripping the cap.
    """
    parent = charge(api, booking_ctx, amount="100.00")
    assert refund(api, booking_ctx, parent["public_id"], amount="100.00").status_code == 201

    assert refund(api, booking_ctx, parent["public_id"], amount="100.00").status_code == 409


def test_nothing_can_be_refunded_against_a_failed_charge(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """A charge that never took money has nothing to give back."""
    parent = charge(api, booking_ctx, amount="100.00", status="failed")

    response = refund(api, booking_ctx, parent["public_id"], amount="1.00")

    assert response.status_code == 409


# ======================================================================================
# Refund of a refund
# ======================================================================================


def test_a_refund_against_the_original_charge_succeeds(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    parent = charge(api, booking_ctx, amount="100.00")

    assert refund(api, booking_ctx, parent["public_id"], amount="40.00").status_code == 201


def test_a_refund_against_a_refund_is_refused(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    parent = charge(api, booking_ctx, amount="100.00")
    first = refund(api, booking_ctx, parent["public_id"], amount="40.00").json()

    response = refund(api, booking_ctx, first["public_id"], amount="10.00")

    assert response.status_code == 409
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 2


def test_a_refund_chain_cannot_be_built_at_any_depth(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    """Every level is refused, so there is no depth at which a chain becomes possible."""
    parent = charge(api, booking_ctx, amount="100.00")
    level_one = refund(api, booking_ctx, parent["public_id"], amount="50.00").json()

    level_two = refund(api, booking_ctx, level_one["public_id"], amount="25.00")
    assert level_two.status_code == 409

    # There is no level-three parent to try, which is the point: the chain stops at one.
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 2


def test_a_refund_chain_cannot_launder_a_fresh_balance(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    """Why the rule exists, stated as a scenario rather than a principle.

    If a refund could parent another refund, the child would be capped against the refund's
    own amount rather than the charge's remaining balance -- so a fully refunded 100 charge
    would still support further refunds through its own reversals, indefinitely.
    """
    parent = charge(api, booking_ctx, amount="100.00")
    full = refund(api, booking_ctx, parent["public_id"], amount="100.00").json()

    assert refund(api, booking_ctx, full["public_id"], amount="100.00").status_code == 409
    assert refunded_total(session, parent["public_id"]) == Decimal("100.00")


# ======================================================================================
# Currency
# ======================================================================================


def test_a_refund_in_the_parents_currency_succeeds(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    parent = charge(api, booking_ctx, amount="100.00", currency="EUR")

    assert (
        refund(api, booking_ctx, parent["public_id"], amount="10.00", currency="EUR").status_code
        == 201
    )


def test_a_refund_in_a_different_currency_is_refused(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    """No conversion is attempted and none is introduced; the request is simply refused."""
    parent = charge(api, booking_ctx, amount="100.00", currency="EUR")

    response = refund(api, booking_ctx, parent["public_id"], amount="50.00", currency="USD")

    assert response.status_code == 409
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1


def test_the_currency_is_taken_from_the_parent_not_the_booking(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """The authoritative comparison is against the charge being reversed."""
    parent = charge(api, booking_ctx, amount="100.00", currency="USD")

    assert (
        refund(api, booking_ctx, parent["public_id"], amount="20.00", currency="USD").status_code
        == 201
    )
    assert (
        refund(api, booking_ctx, parent["public_id"], amount="20.00", currency="EUR").status_code
        == 409
    )


def test_partial_refunds_all_hold_the_parent_currency(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    parent = charge(api, booking_ctx, amount="100.00", currency="EUR")

    assert refund(api, booking_ctx, parent["public_id"], amount="30.00").status_code == 201
    assert (
        refund(api, booking_ctx, parent["public_id"], amount="30.00", currency="GBP").status_code
        == 409
    )
    assert refund(api, booking_ctx, parent["public_id"], amount="30.00").status_code == 201

    assert refunded_total(session, parent["public_id"]) == Decimal("60.00")


# ======================================================================================
# Tenant isolation and authorization
# ======================================================================================


def test_a_parent_payment_at_another_hotel_is_not_found(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    """404, and identical to a payment that does not exist: the refusal must not become an
    oracle for whether another property's payment is real."""
    other_ctx = build_booking(api, "refund-hotel-b")
    foreign = charge(api, other_ctx, amount="100.00")

    cross = refund(api, booking_ctx, foreign["public_id"], amount="10.00")
    invented = refund(api, booking_ctx, str(uuid.uuid4()), amount="10.00")

    assert cross.status_code == 404
    assert invented.status_code == 404
    assert cross.json() == invented.json()
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1


def test_the_cross_tenant_refusal_happens_before_any_business_check(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """A refund that is ALSO over-cap and in the wrong currency still answers 404.

    If a business check ran first, the different status code would tell an attacker that the
    foreign payment exists.
    """
    other_ctx = build_booking(api, "refund-hotel-c")
    foreign = charge(api, other_ctx, amount="10.00", currency="EUR")

    response = refund(api, booking_ctx, foreign["public_id"], amount="99999.00", currency="JPY")

    assert response.status_code == 404


def test_a_non_member_cannot_refund(
    api: TestClient, engine: Engine, booking_ctx: tuple[str, str], session: Session
) -> None:
    parent = charge(api, booking_ctx, amount="100.00")
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    response = stranger.post(
        f"{payments_url(*booking_ctx)}/refunds",
        json=charge_payload(amount="10.00", refunds_public_id=parent["public_id"]),
    )

    assert response.status_code == 404
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1


def test_a_viewer_cannot_refund(
    api: TestClient, engine: Engine, booking_ctx: tuple[str, str], session: Session
) -> None:
    """Refunds require STAFF. The integrity rules never become the thing deciding access."""
    parent = charge(api, booking_ctx, amount="100.00")
    viewer = authenticated_client(engine, email=OTHER_EMAIL)
    grant_membership(engine, OTHER_EMAIL, booking_ctx[0], "viewer")

    response = viewer.post(
        f"{payments_url(*booking_ctx)}/refunds",
        json=charge_payload(amount="10.00", refunds_public_id=parent["public_id"]),
    )

    assert response.status_code == 403
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1


def test_an_unauthenticated_caller_cannot_refund(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    parent = charge(api, booking_ctx, amount="100.00")
    anonymous = TestClient(api.app)

    response = anonymous.post(
        f"{payments_url(*booking_ctx)}/refunds",
        json=charge_payload(amount="10.00", refunds_public_id=parent["public_id"]),
    )

    assert response.status_code == 401
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 1


# ======================================================================================
# Error hygiene
# ======================================================================================


@pytest.mark.parametrize(
    ("scenario", "amount", "currency", "against_refund"),
    [
        ("over cap", "500.00", "EUR", False),
        ("wrong currency", "10.00", "USD", False),
        ("refund of refund", "10.00", "EUR", True),
    ],
)
def test_every_refusal_uses_the_shared_envelope_and_leaks_nothing(
    api: TestClient,
    booking_ctx: tuple[str, str],
    scenario: str,
    amount: str,
    currency: str,
    against_refund: bool,
) -> None:
    parent = charge(api, booking_ctx, amount="100.00", currency="EUR")
    target = parent["public_id"]
    if against_refund:
        target = refund(api, booking_ctx, target, amount="10.00").json()["public_id"]

    response = refund(api, booking_ctx, target, amount=amount, currency=currency)
    body = response.json()

    assert response.status_code == 409, scenario
    assert set(body) == {"error"}
    assert body["error"]["code"] == "CONFLICT"
    assert set(body["error"]) == {"code", "message", "details"}

    text = response.text
    for leak in [
        "SELECT",
        "INSERT",
        "FOR UPDATE",
        "payments",
        "refunded_payment_id",
        "ck_payments",
        "uq_payments",
        "23505",
        "23514",
        "sqlalchemy",
        "psycopg",
        "Traceback",
        "hotel_id",
        "booking_id",
    ]:
        assert leak not in text, f"{scenario} leaked {leak!r}"


def keys_anywhere(value: object) -> set[str]:
    """Every mapping key in a nested JSON structure, at any depth."""
    if isinstance(value, dict):
        found = set(value)
        for nested in value.values():
            found |= keys_anywhere(nested)
        return found
    if isinstance(value, list):
        return {key for item in value for key in keys_anywhere(item)}
    return set()


@pytest.mark.parametrize("amount", ["30.00", "500.00"])
def test_no_response_exposes_an_internal_identifier(
    api: TestClient, booking_ctx: tuple[str, str], amount: str
) -> None:
    """Structural, not textual: no key named ``id`` at any depth, in success or refusal.

    A substring search for the BIGINT would be worse than useless here -- the first payment's
    id is ``1``, which occurs inside ``100.00``. Asserting on the shape is the only version of
    this test that can actually fail for the right reason.
    """
    parent = charge(api, booking_ctx, amount="100.00")

    response = refund(api, booking_ctx, parent["public_id"], amount=amount)
    keys = keys_anywhere(response.json())

    assert "id" not in keys
    assert not {key for key in keys if key.endswith("_id") and not key.endswith("public_id")}


def test_the_over_cap_message_states_the_business_problem(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """Useful to a caller without describing the schema: the remaining amount is theirs."""
    parent = charge(api, booking_ctx, amount="100.00")
    refund(api, booking_ctx, parent["public_id"], amount="70.00")

    message = refund(api, booking_ctx, parent["public_id"], amount="50.00").json()["error"][
        "message"
    ]

    assert "refundable" in message
    assert "30.00" in message


# ======================================================================================
# Append-only, idempotency, and nothing left behind
# ======================================================================================


@pytest.mark.parametrize(
    ("amount", "currency", "against_refund"),
    [("500.00", "EUR", False), ("10.00", "USD", False), ("10.00", "EUR", True)],
)
def test_a_refused_refund_writes_no_row(
    api: TestClient,
    booking_ctx: tuple[str, str],
    session: Session,
    amount: str,
    currency: str,
    against_refund: bool,
) -> None:
    parent = charge(api, booking_ctx, amount="100.00", currency="EUR")
    target = parent["public_id"]
    expected = 1
    if against_refund:
        target = refund(api, booking_ctx, target, amount="10.00").json()["public_id"]
        expected = 2

    refund(api, booking_ctx, target, amount=amount, currency=currency)

    session.expire_all()
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == expected


def test_a_refused_refund_does_not_touch_the_parent(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    """Append-only: the original charge's amount and status are never adjusted, and a
    refusal certainly must not adjust them."""
    parent = charge(api, booking_ctx, amount="100.00")
    before = api.get(f"{payments_url(*booking_ctx)}/{parent['public_id']}").json()

    refund(api, booking_ctx, parent["public_id"], amount="500.00")

    assert api.get(f"{payments_url(*booking_ctx)}/{parent['public_id']}").json() == before


def test_a_successful_refund_does_not_touch_the_parent(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """The charge is not "marked refunded" by mutating it. The refund is its own row."""
    parent = charge(api, booking_ctx, amount="100.00")
    before = api.get(f"{payments_url(*booking_ctx)}/{parent['public_id']}").json()

    assert refund(api, booking_ctx, parent["public_id"], amount="100.00").status_code == 201

    assert api.get(f"{payments_url(*booking_ctx)}/{parent['public_id']}").json() == before


def test_duplicate_transaction_references_are_still_refused_on_refunds(
    api: TestClient, booking_ctx: tuple[str, str]
) -> None:
    """The partial unique index still governs idempotency; the cap runs before it and must
    not have replaced it."""
    parent = charge(api, booking_ctx, amount="100.00")
    reference = {"provider": "stripe", "transaction_reference": "re_dup_4558"}

    first = refund(api, booking_ctx, parent["public_id"], amount="10.00", **reference)
    second = refund(api, booking_ctx, parent["public_id"], amount="10.00", **reference)

    assert first.status_code == 201
    assert second.status_code == 409


def test_idempotency_is_not_bypassed_by_a_refund_that_would_also_breach_the_cap(
    api: TestClient, booking_ctx: tuple[str, str], session: Session
) -> None:
    """Whichever check answers first, no second row appears."""
    parent = charge(api, booking_ctx, amount="100.00")
    reference = {"provider": "stripe", "transaction_reference": "re_dup_4558b"}
    assert (
        refund(api, booking_ctx, parent["public_id"], amount="100.00", **reference).status_code
        == 201
    )

    duplicate = refund(api, booking_ctx, parent["public_id"], amount="100.00", **reference)

    assert duplicate.status_code == 409
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 2


# ======================================================================================
# Concurrency -- two real connections contending on the parent row
# ======================================================================================


def concurrent_refunds(
    engine: Engine, ctx: tuple[str, str], parent_public_id: str, amounts: tuple[str, str]
) -> list[int]:
    """Fire two refunds from separate clients, released together by a barrier.

    Each client is its own application instance with its own session, so the two requests
    reach PostgreSQL on different connections -- which is the only way the row lock is
    actually exercised. The barrier removes the timing luck: both threads are inside the
    handler before either is allowed to proceed.
    """
    both_ready = threading.Barrier(2, timeout=30)
    codes: dict[int, int] = {}

    def attempt(index: int, amount: str) -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        both_ready.wait()
        codes[index] = client.post(
            f"{payments_url(*ctx)}/refunds",
            json=charge_payload(amount=amount, refunds_public_id=parent_public_id),
        ).status_code

    threads = [
        threading.Thread(target=attempt, args=(0, amounts[0])),
        threading.Thread(target=attempt, args=(1, amounts[1])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    return [codes[0], codes[1]]


def assert_exactly_one_refund_won(codes: list[int]) -> None:
    """Exactly one refund is accepted and the other is refused.

    The row lock guarantees that much. The loser's status does not follow from it: normally
    it blocks, re-reads the committed balance and is refused with 409, but PostgreSQL may
    instead detect a deadlock and abort it, which the application maps to 503 (observed in
    CI run 35097766182). Pinning one of the two asserts a scheduling outcome rather than the
    safety property, so both are accepted -- and nothing else is: two successes, two
    rejections, or any other pair still fails.
    """
    assert codes.count(201) == 1, codes
    assert sum(code in {409, 503} for code in codes) == 1, codes


def test_two_concurrent_full_refunds_cannot_both_win(
    api: TestClient, engine: Engine, booking_ctx: tuple[str, str], session: Session
) -> None:
    """The race this stage exists to close.

    Both requests observe a 100 charge with nothing refunded, and both are individually
    correct to conclude that 100 is refundable. Without the row lock both commit and 200 is
    returned against a 100 charge. With it, the second blocks, re-reads the balance the first
    committed, and is refused.
    """
    parent = charge(api, booking_ctx, amount="100.00")

    codes = concurrent_refunds(engine, booking_ctx, parent["public_id"], ("100.00", "100.00"))

    assert_exactly_one_refund_won(codes)
    session.expire_all()
    assert refunded_total(session, parent["public_id"]) == Decimal("100.00")


def test_two_concurrent_partial_refunds_cannot_exceed_the_remainder(
    api: TestClient, engine: Engine, booking_ctx: tuple[str, str], session: Session
) -> None:
    """100 charged, 60 already refunded, two concurrent requests for 40. Only one fits."""
    parent = charge(api, booking_ctx, amount="100.00")
    assert refund(api, booking_ctx, parent["public_id"], amount="60.00").status_code == 201

    codes = concurrent_refunds(engine, booking_ctx, parent["public_id"], ("40.00", "40.00"))

    assert_exactly_one_refund_won(codes)
    session.expire_all()
    assert refunded_total(session, parent["public_id"]) == Decimal("100.00")


def test_two_concurrent_refunds_that_both_fit_both_succeed(
    api: TestClient, engine: Engine, booking_ctx: tuple[str, str], session: Session
) -> None:
    """The lock serialises; it does not refuse work that the balance can actually support."""
    parent = charge(api, booking_ctx, amount="100.00")

    codes = concurrent_refunds(engine, booking_ctx, parent["public_id"], ("50.00", "50.00"))

    assert codes == [201, 201], codes
    session.expire_all()
    assert refunded_total(session, parent["public_id"]) == Decimal("100.00")


def test_the_losing_transaction_leaves_no_row_behind(
    api: TestClient, engine: Engine, booking_ctx: tuple[str, str], session: Session
) -> None:
    parent = charge(api, booking_ctx, amount="100.00")

    concurrent_refunds(engine, booking_ctx, parent["public_id"], ("100.00", "100.00"))

    session.expire_all()
    # The charge and exactly one refund.
    assert session.scalar(sa.select(sa.func.count()).select_from(Payment)) == 2
