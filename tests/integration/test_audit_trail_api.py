"""The audit trail against real PostgreSQL.

Stage 4.5.12. Two claims are made about this table and neither can be tested anywhere but
against a live database.

**It is append-only, and the database says so.** Migration 0007 installs a trigger that raises
on UPDATE and DELETE. A mock cannot refuse a write; only PostgreSQL can, and the section below
makes it.

**An audit event and the mutation it describes commit or roll back together.** The proof that
matters is the negative one: a booking rejected by the DEFERRED night-completeness trigger
fails at COMMIT, *after* the audit row has been staged and flushed successfully -- so the row
demonstrably existed inside the transaction and is demonstrably gone once it rolls back. No
in-memory database has a deferred constraint trigger, so that scenario cannot be simulated.

Money is compared as ``Decimal``; audit details hold it as a decimal STRING, and both are
checked, because a figure that survived a round trip through ``float`` is a figure that might
not be the one that was posted.
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
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import (
    TEST_PASSWORD,
    authenticated_client,
    create_test_app,
    grant_membership,
    grant_platform_admin,
    register_and_login,
    requires_postgres,
)

pytestmark = requires_postgres

SUITE_EMAIL = "audit-trail@example.test"
OTHER_EMAIL = "audit-trail-other@example.test"

#: The replacement credential this suite sets. Named once so the privacy sweep can assert
#: that this exact value never appears in an audit row.
NEW_PASSWORD = "audit-suite-replacement-password"

CHECK_IN = dt.date(2027, 6, 7)
CHECK_OUT = dt.date(2027, 6, 10)


# ======================================================================================
# Scaffolding
# ======================================================================================


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
    client = authenticated_client(engine, email=SUITE_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        # audit_events first: both its foreign keys are ON DELETE RESTRICT, deliberately, so
        # a property with history and an account that has acted are protected from a plain
        # DELETE. TRUNCATE ... CASCADE is not a delete and the append-only trigger does not
        # fire for it -- which is exactly the distinction migration 0007 relies on.
        cleanup.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build_hotel(api: TestClient, slug: str = "audit-hotel") -> str:
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
    for number in ("101", "102"):
        api.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": number})
    return hotel


def booking_body(
    guest: str,
    *,
    room: str = "101",
    check_in: dt.date = CHECK_IN,
    check_out: dt.date = CHECK_OUT,
    status: str = "confirmed",
    nights: int | None = None,
) -> dict[str, Any]:
    span = (check_out - check_in).days if nights is None else nights
    return {
        "guest_public_id": guest,
        "reference": f"BK-{uuid.uuid4().hex[:8].upper()}",
        "check_in_date": str(check_in),
        "check_out_date": str(check_out),
        "status": status,
        "total_amount": "360.00",
        "currency": "EUR",
        "rooms": [
            {
                "room_number": room,
                "nights": [
                    {"stay_date": str(check_in + dt.timedelta(days=n))} for n in range(span)
                ],
            }
        ],
    }


def make_guest(api: TestClient, hotel: str) -> str:
    return str(
        api.post(
            f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
        ).json()["public_id"]
    )


@pytest.fixture
def world(api: TestClient) -> dict[str, str]:
    """A hotel with two rooms, a guest and one confirmed booking."""
    hotel = build_hotel(api)
    guest = make_guest(api, hotel)
    booking = api.post(f"/api/v1/hotels/{hotel}/bookings", json=booking_body(guest))
    assert booking.status_code == 201, booking.text
    return {"hotel": hotel, "guest": guest, "booking": str(booking.json()["public_id"])}


def audit_url(hotel: str) -> str:
    return f"/api/v1/hotels/{hotel}/audit-events"


def events(api: TestClient, hotel: str, **params: object) -> list[dict[str, Any]]:
    response = api.get(audit_url(hotel), params=params)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def rows(session: Session, **where: object) -> list[dict[str, Any]]:
    """Audit rows read straight from the table, bypassing the API entirely."""
    clause = " AND ".join(f"{column} = :{column}" for column in where) or "TRUE"
    result = session.execute(
        sa.text(f"SELECT * FROM audit_events WHERE {clause} ORDER BY id"), where
    )
    return [dict(row) for row in result.mappings()]


def manager(engine: Engine, hotel: str, email: str, role: str = "manager") -> TestClient:
    """A second client at *role*, so the read policy can be exercised for each level."""
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, hotel, role)
    return client


# ======================================================================================
# The table itself
# ======================================================================================


def test_the_table_exists_with_the_expected_columns(session: Session) -> None:
    columns = {
        row[0]
        for row in session.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'audit_events'"
            )
        )
    }

    assert columns == {
        "id",
        "public_id",
        "hotel_id",
        "actor_user_id",
        "action",
        "resource_type",
        "resource_reference",
        "request_id",
        "details",
        "occurred_at",
    }


def test_the_table_has_no_updated_at_column(session: Session) -> None:
    """Every other table in this schema has one. A row that can never be updated has no
    moment of last update, and a trigger maintaining one would contradict the append-only
    trigger beside it."""
    columns = {
        row[0]
        for row in session.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'audit_events'"
            )
        )
    }

    assert "updated_at" not in columns


def test_the_four_indexes_exist(session: Session) -> None:
    names = {
        row[0]
        for row in session.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'audit_events'")
        )
    }

    assert {
        "ix_audit_events_hotel_id_occurred_at",
        "ix_audit_events_actor_user_id_occurred_at",
        "ix_audit_events_resource_type_resource_reference",
        "ix_audit_events_request_id",
    } <= names


def test_the_listing_index_orders_newest_first(session: Session) -> None:
    """``occurred_at DESC, id DESC`` term for term. An index that did not match the ORDER BY
    would make the listing a sort of the whole table."""
    definition = session.scalar(
        sa.text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_audit_events_hotel_id_occurred_at'"
        )
    )

    assert "hotel_id" in str(definition)
    assert "occurred_at DESC" in str(definition)
    assert "id DESC" in str(definition)


def test_details_is_jsonb(session: Session) -> None:
    kind = session.scalar(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'audit_events' AND column_name = 'details'"
        )
    )

    assert kind == "jsonb"


def test_both_foreign_keys_restrict(session: Session) -> None:
    """CASCADE would delete rows and SET NULL would update them, and the append-only trigger
    refuses both -- so RESTRICT is not a preference here, it is the only policy consistent
    with immutability."""
    policies = {
        row[0]: row[1]
        for row in session.execute(
            sa.text(
                "SELECT c.conname, c.confdeltype FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'audit_events' AND c.contype = 'f'"
            )
        )
    }

    assert policies == {
        "fk_audit_events_hotel_id_hotels": "r",
        "fk_audit_events_actor_user_id_users": "r",
    }


# ======================================================================================
# Append-only, enforced by PostgreSQL
# ======================================================================================


def test_an_update_is_refused_by_the_database(
    api: TestClient, world: dict, session: Session
) -> None:
    """The application offers no way to edit an event. This is what stops everything else --
    a fixture script, a migration, a psql session."""
    session.commit()
    with pytest.raises(sa.exc.DatabaseError, match="append-only"):
        session.execute(sa.text("UPDATE audit_events SET action = 'booking.created'"))
    session.rollback()


def test_a_delete_is_refused_by_the_database(
    api: TestClient, world: dict, session: Session
) -> None:
    session.commit()
    with pytest.raises(sa.exc.DatabaseError, match="append-only"):
        session.execute(sa.text("DELETE FROM audit_events"))
    session.rollback()


def test_the_refusal_names_no_row_and_no_column(
    api: TestClient, world: dict, session: Session
) -> None:
    """The trigger's message says what is not permitted, not what was in the row."""
    session.commit()
    with pytest.raises(sa.exc.DatabaseError) as caught:
        session.execute(sa.text("DELETE FROM audit_events"))
    session.rollback()

    message = str(caught.value)
    assert "booking.created" not in message
    assert world["booking"] not in message


def test_an_invented_action_is_refused_by_the_check(session: Session) -> None:
    """The vocabulary is closed at the database, not merely in Python -- so the out-of-band
    INSERT that a real installation eventually performs cannot invent one either.

    The example is DERIVED from the enum rather than written down. This test originally used
    ``booking.deleted``, which was a good choice at the time -- plausible, and genuinely not in
    the vocabulary -- right up until Stage 4.5.15 added it and migration 0009 taught the
    database to accept it. A hard-coded counter-example to a growing list is a test with an
    expiry date on it; asking the enum what it does NOT contain has none.
    """
    from app.models.enums import AuditAction

    invented = "booking.vaporised"
    assert invented not in AuditAction.values(), "pick an action the vocabulary really lacks"

    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
            sa.text(
                "INSERT INTO audit_events (action, resource_type, resource_reference) "
                "VALUES (:action, 'booking', 'x')"
            ),
            {"action": invented},
        )
    session.rollback()


def test_a_malformed_request_id_is_refused_by_the_check(session: Session) -> None:
    """A newline in this column would let a forged log line be replayed out of an audit
    report. The middleware already filters it; the CHECK is the second lock."""
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
            sa.text(
                "INSERT INTO audit_events (action, resource_type, resource_reference, request_id) "
                "VALUES ('booking.created', 'booking', 'x', 'abc\ndef')"
            )
        )
    session.rollback()


def test_details_must_be_an_object(session: Session) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
            sa.text(
                "INSERT INTO audit_events (action, resource_type, resource_reference, details) "
                "VALUES ('booking.created', 'booking', 'x', '\"a string\"'::jsonb)"
            )
        )
    session.rollback()


def test_a_resource_reference_longer_than_the_bound_is_refused(session: Session) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
            sa.text(
                "INSERT INTO audit_events (action, resource_type, resource_reference) "
                "VALUES ('booking.created', 'booking', :long)"
            ),
            {"long": "x" * 65},
        )
    session.rollback()


# ======================================================================================
# Booking
# ======================================================================================


def test_creating_a_booking_is_audited(api: TestClient, world: dict) -> None:
    found = events(api, world["hotel"], action="booking.created")

    assert len(found) == 1
    assert found[0]["resource_type"] == "booking"
    assert found[0]["resource_reference"] == world["booking"]


def test_the_creation_event_records_the_shape_of_the_stay(api: TestClient, world: dict) -> None:
    details = events(api, world["hotel"], action="booking.created")[0]["details"]

    assert details["check_in_date"] == str(CHECK_IN)
    assert details["check_out_date"] == str(CHECK_OUT)
    assert details["status"] == "confirmed"
    assert details["rooms"] == 1


def test_a_status_transition_is_audited_with_both_statuses(api: TestClient, world: dict) -> None:
    api.patch(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}",
        json={"status": "checked_in"},
    )

    found = events(api, world["hotel"], action="booking.status_changed")

    assert len(found) == 1
    assert found[0]["details"] == {"old_status": "confirmed", "new_status": "checked_in"}


def test_an_idempotent_status_patch_records_nothing(api: TestClient, world: dict) -> None:
    """``confirmed -> confirmed`` is permitted and changes nothing. An event for it would be
    a claim that something happened."""
    response = api.patch(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}",
        json={"status": "confirmed"},
    )

    assert response.status_code == 200
    assert events(api, world["hotel"], action="booking.status_changed") == []


def test_a_non_status_update_records_nothing(api: TestClient, world: dict) -> None:
    """Editing occupancy is not a lifecycle change, and the audit boundary is intentional
    rather than "every write"."""
    api.patch(f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}", json={"adults": 3})

    assert events(api, world["hotel"], action="booking.status_changed") == []


def test_a_refused_transition_records_nothing(api: TestClient, world: dict) -> None:
    api.patch(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}",
        json={"status": "cancelled"},
    )
    refused = api.patch(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}",
        json={"status": "checked_in"},
    )

    assert refused.status_code == 409
    changes = events(api, world["hotel"], action="booking.status_changed")
    assert [c["details"]["new_status"] for c in changes] == ["cancelled"]


def test_modifying_a_stay_is_audited_with_the_fields_that_moved(
    api: TestClient, world: dict
) -> None:
    response = api.patch(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/stay",
        json={
            "check_in_date": str(CHECK_IN + dt.timedelta(days=1)),
            "check_out_date": str(CHECK_OUT + dt.timedelta(days=1)),
            "rooms": [
                {
                    "room_number": "102",
                    "nights": [
                        {
                            "stay_date": str(CHECK_IN + dt.timedelta(days=1 + n)),
                        }
                        for n in range(3)
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200, response.text

    found = events(api, world["hotel"], action="booking.stay_modified")

    assert len(found) == 1
    assert found[0]["details"]["changed_fields"] == ["check_in_date", "check_out_date", "rooms"]
    assert found[0]["resource_reference"] == world["booking"]


def test_a_stay_modification_that_moves_nothing_records_no_changed_fields(
    api: TestClient, world: dict
) -> None:
    """Restating the current stay is still a modification -- every night row is rewritten --
    but nothing about the SHAPE of it moved, and the event says so rather than guessing."""
    response = api.patch(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/stay",
        json={
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [
                        {"stay_date": str(CHECK_IN + dt.timedelta(days=n))} for n in range(3)
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200, response.text

    assert (
        events(api, world["hotel"], action="booking.stay_modified")[0]["details"]["changed_fields"]
        == []
    )


def test_a_failed_booking_creation_leaves_no_event(api: TestClient, world: dict) -> None:
    """The exclusion constraint refuses the second booking on room 101, and the rollback
    takes the audit row with it."""
    guest = make_guest(api, world["hotel"])
    refused = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings", json=booking_body(guest, room="101")
    )

    assert refused.status_code == 409, refused.text
    assert len(events(api, world["hotel"], action="booking.created")) == 1


# ======================================================================================
# Transaction integrity: the mandated rollback proof
# ======================================================================================


def test_a_commit_time_failure_leaves_neither_the_booking_nor_its_event(
    api: TestClient, world: dict, session: Session
) -> None:
    """The mandated rollback proof, and the reason this suite needs PostgreSQL.

    Driven one layer below HTTP on purpose. ``BookingCreate`` refuses an incomplete night set
    at the schema, with a 422, so the API cannot reach a commit-time failure at all -- which
    is good, and is exactly why the scenario has to be built here instead. The point being
    tested is not how a bad payload is refused; it is what happens to an audit row when a
    transaction that had already staged it fails at COMMIT.

    The sequence is the one the requirement names:

    1. the business mutation starts -- a booking, its allocation and two of its three nights;
    2. the audit event is created, and FLUSHES without complaint;
    3. it is then observed to exist inside the transaction, so this cannot pass merely because
       the row was never written;
    4. the COMMIT fails -- ``trg_booking_room_nights_complete`` is DEFERRABLE INITIALLY
       DEFERRED and judges the aggregate only then;
    5. neither the booking nor the event remains.

    An event written on a second connection, after the commit, or by an after-commit hook
    would survive step 5. That is what this design refuses to do.
    """
    from app.models.booking import Booking, BookingRoom, BookingRoomNight
    from app.models.enums import AuditAction, AuditResourceType
    from app.repositories.audit import AuditRepository
    from app.repositories.booking import BookingRepository
    from app.services.audit import AuditTrail

    session.rollback()
    hotel_id, room_id = session.execute(
        sa.text(
            "SELECT h.id, r.id FROM hotels h JOIN rooms r ON r.hotel_id = h.id "
            "WHERE h.public_id = CAST(:h AS uuid) AND r.room_number = '102'"
        ),
        {"h": world["hotel"]},
    ).one()
    guest_id = session.scalar(
        sa.text("SELECT id FROM guests WHERE public_id = CAST(:g AS uuid)"),
        {"g": world["guest"]},
    )

    bookings = BookingRepository(session)
    trail = AuditTrail(AuditRepository(session))
    reference = f"BK-ROLLBACK-{uuid.uuid4().hex[:6].upper()}"

    booking = bookings.add_booking(
        Booking(
            hotel_id=hotel_id,
            guest_id=guest_id,
            reference=reference,
            check_in_date=CHECK_IN,
            check_out_date=CHECK_OUT,
            status="confirmed",
            adults=1,
            children=0,
            source="direct",
            total_amount=Decimal("360.00"),
            currency="EUR",
        )
    )
    allocation = bookings.add_room(
        BookingRoom(
            booking_id=booking.id,
            room_id=room_id,
            hotel_id=hotel_id,
            check_in_date=CHECK_IN,
            check_out_date=CHECK_OUT,
            booking_status="confirmed",
            adults=1,
            children=0,
        )
    )
    # Two priced nights for a three-night stay. Legal until COMMIT; that is what deferring the
    # trigger buys, and it is what makes this scenario reachable at all.
    bookings.add_nights(
        [
            BookingRoomNight(
                booking_room_id=allocation.id,
                hotel_id=hotel_id,
                check_in_date=CHECK_IN,
                check_out_date=CHECK_OUT,
                stay_date=CHECK_IN + dt.timedelta(days=n),
                rate=Decimal("120.00"),
            )
            for n in range(2)
        ]
    )
    event = trail.record(
        AuditAction.BOOKING_CREATED,
        AuditResourceType.BOOKING,
        str(booking.public_id),
        hotel_id=hotel_id,
        details={"reference": reference},
    )

    # Step 3: the row really is there, on this connection, before the commit is attempted.
    assert (
        session.scalar(
            sa.text("SELECT count(*) FROM audit_events WHERE public_id = CAST(:p AS uuid)"),
            {"p": str(event.public_id)},
        )
        == 1
    )

    with pytest.raises(sa.exc.DatabaseError):
        session.commit()
    session.rollback()

    assert (
        session.scalar(
            sa.text("SELECT count(*) FROM bookings WHERE reference = :r"), {"r": reference}
        )
        == 0
    )
    assert rows(session, resource_reference=str(booking.public_id)) == [], (
        "the audit event outlived the transaction that staged it"
    )


def test_a_refused_refund_leaves_no_event(api: TestClient, world: dict) -> None:
    """`_require_refundable` raises before anything is written, so this proves the other
    half: not every failure is a rollback, and the ones that are not must never reach the
    recording call at all."""
    charge = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={"amount": "100.00", "currency": "EUR", "method": "card"},
    ).json()
    refused = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments/refunds",
        json={
            "amount": "500.00",
            "currency": "EUR",
            "method": "card",
            "refunds_public_id": charge["public_id"],
        },
    )

    assert refused.status_code == 409
    assert events(api, world["hotel"], action="payment.refund_created") == []


def test_a_refused_member_removal_leaves_no_event(
    api: TestClient, engine: Engine, world: dict
) -> None:
    """Removing the last owner is refused by MembershipPolicy before the delete is staged."""
    me = api.get("/api/v1/auth/me").json()["public_id"]

    refused = api.delete(f"/api/v1/hotels/{world['hotel']}/members/{me}")

    assert refused.status_code == 409
    assert events(api, world["hotel"], action="membership.removed") == []


# ======================================================================================
# Payments
# ======================================================================================


def test_a_charge_is_audited(api: TestClient, world: dict) -> None:
    charge = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={"amount": "150.00", "currency": "EUR", "method": "card"},
    ).json()

    found = events(api, world["hotel"], action="payment.created")

    assert len(found) == 1
    assert found[0]["resource_reference"] == charge["public_id"]
    assert found[0]["details"]["amount"] == "150.00"
    assert found[0]["details"]["currency"] == "EUR"
    assert found[0]["details"]["booking_public_id"] == world["booking"]


def test_the_recorded_amount_is_a_decimal_string_not_a_float(api: TestClient, world: dict) -> None:
    """``Numeric(14, 2)`` through ``float`` is how 0.1 + 0.2 gets into an audit report."""
    api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={"amount": "0.30", "currency": "EUR", "method": "cash"},
    )

    amount = events(api, world["hotel"], action="payment.created")[0]["details"]["amount"]

    assert amount == "0.30"
    assert isinstance(amount, str)


def test_a_refund_is_audited_and_names_the_charge_it_reverses(api: TestClient, world: dict) -> None:
    charge = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={"amount": "100.00", "currency": "EUR", "method": "card"},
    ).json()
    refund = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments/refunds",
        json={
            "amount": "40.00",
            "currency": "EUR",
            "method": "card",
            "refunds_public_id": charge["public_id"],
        },
    )
    assert refund.status_code == 201, refund.text

    found = events(api, world["hotel"], action="payment.refund_created")

    assert len(found) == 1
    assert found[0]["details"]["refunds_public_id"] == charge["public_id"]
    assert found[0]["details"]["amount"] == "40.00"


def test_no_payment_event_stores_processor_or_card_data(api: TestClient, world: dict) -> None:
    """The one place financial auditing goes wrong. The payment row legitimately holds a card
    fragment and a processor reference; the audit row must not duplicate them, because it is
    read by more people than the ledger is."""
    api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={
            "amount": "100.00",
            "currency": "EUR",
            "method": "card",
            "provider": "stripe",
            "transaction_reference": "pi_3ABCDEF1234567890",
            "card_last_four": "4242",
        },
    )

    details = events(api, world["hotel"], action="payment.created")[0]["details"]
    rendered = str(details)

    assert set(details) == {"amount", "currency", "method", "status", "booking_public_id"}
    for secret in ("stripe", "pi_3ABCDEF1234567890", "4242"):
        assert secret not in rendered, f"{secret!r} reached the audit trail"


def test_concurrent_refunds_record_exactly_one_success(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """Two requests both believe 100 is refundable; the row lock lets one commit.

    The audit trail must agree with what committed, not with what was attempted. A design
    that recorded before deciding, or on a second connection, would show two refunds here.
    """
    charge = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={"amount": "100.00", "currency": "EUR", "method": "card"},
    ).json()

    both_ready = threading.Barrier(2, timeout=30)
    codes: dict[int, int] = {}

    def attempt(index: int) -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        both_ready.wait()
        codes[index] = client.post(
            f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments/refunds",
            json={
                "amount": "100.00",
                "currency": "EUR",
                "method": "card",
                "refunds_public_id": charge["public_id"],
            },
        ).status_code

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    assert sorted(codes.values()) == [201, 409], codes
    session.rollback()
    assert len(rows(session, action="payment.refund_created")) == 1


def test_concurrent_status_changes_record_only_what_committed(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """``confirmed`` can become ``checked_in`` or ``no_show`` but not both. Whichever loses is
    refused under the row lock, and the trail must contain one event, not two."""
    both_ready = threading.Barrier(2, timeout=30)
    codes: dict[int, int] = {}

    def attempt(index: int, status: str) -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        both_ready.wait()
        codes[index] = client.patch(
            f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}",
            json={"status": status},
        ).status_code

    threads = [
        threading.Thread(target=attempt, args=(0, "checked_in")),
        threading.Thread(target=attempt, args=(1, "no_show")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    session.rollback()
    recorded = rows(session, action="booking.status_changed")
    assert len(recorded) == sum(1 for code in codes.values() if code == 200)
    final = session.scalar(
        sa.text("SELECT status FROM bookings WHERE public_id = CAST(:b AS uuid)"),
        {"b": world["booking"]},
    )
    assert recorded[-1]["details"]["new_status"] == final


def test_concurrent_stay_modifications_record_what_each_committed(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """Both modifications of the same booking serialise on its row lock and both succeed. The
    trail must show two events and the booking must be coherent, not a hybrid."""
    both_ready = threading.Barrier(2, timeout=30)
    codes: dict[int, int] = {}

    def attempt(index: int, offset: int) -> None:
        client = authenticated_client(engine, email=SUITE_EMAIL)
        start = CHECK_IN + dt.timedelta(days=offset)
        both_ready.wait()
        codes[index] = client.patch(
            f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/stay",
            json={
                "check_in_date": str(start),
                "check_out_date": str(start + dt.timedelta(days=2)),
                "rooms": [
                    {
                        "room_number": "101",
                        "nights": [
                            {"stay_date": str(start + dt.timedelta(days=n))} for n in range(2)
                        ],
                    }
                ],
            },
        ).status_code

    threads = [
        threading.Thread(target=attempt, args=(0, 10)),
        threading.Thread(target=attempt, args=(1, 20)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    session.rollback()
    recorded = rows(session, action="booking.stay_modified")
    assert len(recorded) == sum(1 for code in codes.values() if code == 200)
    assert len(recorded) >= 1


# ======================================================================================
# Membership
# ======================================================================================


def test_adding_a_member_is_audited(api: TestClient, engine: Engine, world: dict) -> None:
    bootstrap = TestClient(create_test_app(engine))
    register_and_login(bootstrap, OTHER_EMAIL)

    response = api.post(
        f"/api/v1/hotels/{world['hotel']}/members",
        json={"email": OTHER_EMAIL, "role": "staff"},
    )
    assert response.status_code == 201, response.text

    found = events(api, world["hotel"], action="membership.created")

    assert len(found) == 1
    assert found[0]["details"] == {"role": "staff"}
    assert found[0]["resource_reference"] == response.json()["user_public_id"]


def test_a_role_change_is_audited_with_both_roles(
    api: TestClient, engine: Engine, world: dict
) -> None:
    bootstrap = TestClient(create_test_app(engine))
    register_and_login(bootstrap, OTHER_EMAIL)
    member = api.post(
        f"/api/v1/hotels/{world['hotel']}/members",
        json={"email": OTHER_EMAIL, "role": "staff"},
    ).json()["user_public_id"]

    api.patch(f"/api/v1/hotels/{world['hotel']}/members/{member}", json={"role": "manager"})

    found = events(api, world["hotel"], action="membership.role_changed")

    assert len(found) == 1
    assert found[0]["details"] == {"old_role": "staff", "new_role": "manager"}


def test_removing_a_member_is_audited_with_the_role_they_held(
    api: TestClient, engine: Engine, world: dict
) -> None:
    bootstrap = TestClient(create_test_app(engine))
    register_and_login(bootstrap, OTHER_EMAIL)
    member = api.post(
        f"/api/v1/hotels/{world['hotel']}/members",
        json={"email": OTHER_EMAIL, "role": "staff"},
    ).json()["user_public_id"]

    assert api.delete(f"/api/v1/hotels/{world['hotel']}/members/{member}").status_code == 204

    found = events(api, world["hotel"], action="membership.removed")

    assert len(found) == 1
    assert found[0]["details"] == {"role": "staff"}


def test_a_membership_event_records_no_email(api: TestClient, engine: Engine, world: dict) -> None:
    """The public id identifies the person to anyone entitled to resolve it. The address is a
    second copy of the same fact in a table that is read more widely."""
    bootstrap = TestClient(create_test_app(engine))
    register_and_login(bootstrap, OTHER_EMAIL)
    api.post(
        f"/api/v1/hotels/{world['hotel']}/members",
        json={"email": OTHER_EMAIL, "role": "staff"},
    )

    found = events(api, world["hotel"], action="membership.created")[0]

    assert OTHER_EMAIL not in str(found["details"])
    assert OTHER_EMAIL not in found["resource_reference"]


def test_concurrent_owner_demotions_record_only_the_one_that_committed(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """The Stage 4.4 race, seen from the audit trail.

    Two owners, each being demoted by a different request. Both read "there are 2 owners" and
    both would be right on their own; ``lock_owner_ids`` serialises them, so the second finds a
    hotel that would be left unadministered and is refused. The hotel keeps an owner -- and the
    trail must show ONE role change, not two.

    A design that recorded before the ownership rule decided, or on its own connection, would
    show two demotions here and leave an audit report claiming a hotel was left unadministered
    when it never was.
    """
    seconds = ["audit-owner-two@example.test", "audit-owner-three@example.test"]
    for email in seconds:
        bootstrap = TestClient(create_test_app(engine))
        register_and_login(bootstrap, email)
        response = api.post(
            f"/api/v1/hotels/{world['hotel']}/members", json={"email": email, "role": "owner"}
        )
        assert response.status_code == 201, response.text
    # The suite's own account is the third owner; demote it so exactly two remain and the
    # rule is genuinely at stake.
    me = api.get("/api/v1/auth/me").json()["public_id"]
    assert (
        api.patch(
            f"/api/v1/hotels/{world['hotel']}/members/{me}", json={"role": "manager"}
        ).status_code
        == 200
    )

    member_ids = [
        item["user_public_id"]
        for item in api.get(f"/api/v1/hotels/{world['hotel']}/members").json()["items"]
        if item["role"] == "owner"
    ]
    assert len(member_ids) == 2, member_ids

    session.rollback()
    before = len(rows(session, action="membership.role_changed"))

    both_ready = threading.Barrier(2, timeout=30)
    codes: dict[int, int] = {}

    def attempt(index: int, target: str) -> None:
        client = authenticated_client(engine, email=seconds[index])
        both_ready.wait()
        codes[index] = client.patch(
            f"/api/v1/hotels/{world['hotel']}/members/{target}", json={"role": "manager"}
        ).status_code

    threads = [
        threading.Thread(target=attempt, args=(0, member_ids[0])),
        threading.Thread(target=attempt, args=(1, member_ids[1])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    # Exactly one demotion commits. The other is REFUSED, and which refusal it gets depends on
    # who won -- a distinction that is scaffolding here rather than the thing under test:
    #
    #   409  the ownership rule caught it: two owners remained when it started, one when it
    #        committed, and MembershipPolicy refused to remove the last;
    #   403  the winner had already demoted the loser's own account, so by the time the loser's
    #        request was authorized it was no longer an OWNER and never reached the rule.
    #
    # Both are correct refusals and both leave the hotel administrable. An earlier draft of
    # this test asserted 409 specifically and passed for two stages before the second ordering
    # happened to occur -- it was asserting a coin flip. What matters, and what is asserted
    # below, is the invariant: one commit, one audit event, one surviving owner.
    outcomes = sorted(codes.values())
    assert outcomes[0] == 200, codes
    assert outcomes[1] in (403, 409), codes
    session.rollback()
    after = rows(session, action="membership.role_changed")
    assert len(after) - before == 1, [row["details"] for row in after]

    owners = session.scalar(
        sa.text(
            "SELECT count(*) FROM user_hotels uh JOIN hotels h ON h.id = uh.hotel_id "
            "WHERE h.public_id = CAST(:h AS uuid) AND uh.role = 'owner'"
        ),
        {"h": world["hotel"]},
    )
    assert owners == 1


# ======================================================================================
# Authentication and the actor
# ======================================================================================


def test_the_authenticated_actor_is_recorded(api: TestClient, world: dict) -> None:
    me = api.get("/api/v1/auth/me").json()

    found = events(api, world["hotel"], action="booking.created")[0]

    assert found["actor_public_id"] == me["public_id"]
    assert found["actor_email"] == SUITE_EMAIL


def test_an_unauthenticated_mutation_records_nothing(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """Authentication runs before any service is assembled, so a mutation without a token
    never reaches the recording call."""
    anonymous = TestClient(create_test_app(engine))
    guest = make_guest(api, world["hotel"])

    refused = anonymous.post(
        f"/api/v1/hotels/{world['hotel']}/bookings", json=booking_body(guest, room="102")
    )

    assert refused.status_code == 401
    session.rollback()
    assert len(rows(session, action="booking.created")) == 1


def test_a_password_change_is_audited(api: TestClient, engine: Engine, session: Session) -> None:
    response = api.post(
        "/api/v1/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 200, response.text

    session.rollback()
    recorded = rows(session, action="auth.password_changed")

    assert len(recorded) == 1
    assert recorded[0]["resource_type"] == "user"
    assert recorded[0]["actor_user_id"] is not None


def test_a_password_change_event_carries_no_hotel(api: TestClient, session: Session) -> None:
    """A password belongs to the account. A user may belong to no property or to several, so
    attributing the change to one would be an invention."""
    api.post(
        "/api/v1/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD},
    )

    session.rollback()
    assert rows(session, action="auth.password_changed")[0]["hotel_id"] is None


def test_a_password_change_event_stores_no_credential(api: TestClient, session: Session) -> None:
    """The single most important privacy assertion in this stage."""
    api.post(
        "/api/v1/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD},
    )

    session.rollback()
    rendered = str(rows(session, action="auth.password_changed")[0])

    assert rendered.count("$argon2") == 0
    for secret in (TEST_PASSWORD, NEW_PASSWORD, "password_hash", "argon2"):
        assert secret not in rendered, f"{secret!r} reached the audit trail"
    assert rows(session, action="auth.password_changed")[0]["details"] == {}


def test_a_failed_password_change_records_nothing(api: TestClient, session: Session) -> None:
    refused = api.post(
        "/api/v1/auth/change-password",
        json={"current_password": "not-the-password", "new_password": "a-brand-new-password"},
    )

    assert refused.status_code == 401
    session.rollback()
    assert rows(session, action="auth.password_changed") == []


# ======================================================================================
# Request correlation
# ======================================================================================


def test_the_event_carries_the_request_id_of_the_request_that_caused_it(
    api: TestClient, world: dict
) -> None:
    guest = make_guest(api, world["hotel"])
    response = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings",
        json=booking_body(guest, room="102"),
        headers={"X-Request-ID": "audit-correlation-1"},
    )
    assert response.status_code == 201, response.text

    assert response.headers["x-request-id"] == "audit-correlation-1"
    found = events(api, world["hotel"], request_id="audit-correlation-1")
    assert [e["resource_reference"] for e in found] == [response.json()["public_id"]]


def test_a_generated_request_id_is_recorded_and_matches_the_response_header(
    api: TestClient, world: dict
) -> None:
    """No header supplied: the middleware mints one, returns it, and the audit row carries
    the same value -- which is what makes a row and a log line joinable."""
    guest = make_guest(api, world["hotel"])
    response = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings", json=booking_body(guest, room="102")
    )
    generated = response.headers["x-request-id"]

    found = events(api, world["hotel"], request_id=generated)

    assert [e["request_id"] for e in found] == [generated]


def test_a_rejected_request_id_is_replaced_not_stored(api: TestClient, world: dict) -> None:
    """An id that fails the middleware's filter is discarded there and never reaches the
    column, whose own CHECK would refuse it anyway."""
    guest = make_guest(api, world["hotel"])
    response = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings",
        json=booking_body(guest, room="102"),
        headers={"X-Request-ID": "not a valid id"},
    )
    assert response.status_code == 201

    created = events(api, world["hotel"], action="booking.created")
    stored = {e["request_id"] for e in created}

    assert "not a valid id" not in stored
    assert all(len(value) <= 64 for value in stored if value)


def test_one_request_that_writes_twice_shares_one_id(api: TestClient, world: dict) -> None:
    """Two audit rows from one request must carry the same id, or correlation is a lie.

    Booking creation writes one event; the identity is asserted across two requests here
    because no single endpoint in this codebase records twice -- which is itself worth
    stating, so the day one does, this test is where it is thought about.
    """
    ids = {e["request_id"] for e in events(api, world["hotel"])}

    assert len(ids) == len(events(api, world["hotel"]))


# ======================================================================================
# Tenant isolation
# ======================================================================================


def test_one_hotels_history_never_contains_anothers(api: TestClient, world: dict) -> None:
    other = build_hotel(api, slug="audit-hotel-two")
    guest = make_guest(api, other)
    booking = api.post(f"/api/v1/hotels/{other}/bookings", json=booking_body(guest)).json()

    first = {e["resource_reference"] for e in events(api, world["hotel"])}
    second = {e["resource_reference"] for e in events(api, other)}

    assert world["booking"] in first
    assert str(booking["public_id"]) not in first
    assert str(booking["public_id"]) in second
    assert world["booking"] not in second


def test_a_non_member_cannot_read_a_hotels_history(
    api: TestClient, engine: Engine, world: dict
) -> None:
    """The hotel's own 404, byte-identical to the one an invented id produces, so this
    endpoint cannot be used to discover that a property exists."""
    stranger = authenticated_client(engine, email="audit-stranger@example.test")

    refused = stranger.get(audit_url(world["hotel"]))
    invented = stranger.get(audit_url(str(uuid.uuid4())))

    assert refused.status_code == 404
    assert refused.json() == invented.json()


def test_every_returned_event_belongs_to_the_hotel_in_the_url(api: TestClient, world: dict) -> None:
    build_hotel(api, slug="audit-hotel-three")

    for event in events(api, world["hotel"]):
        assert event["hotel_public_id"] == world["hotel"]


def test_a_cross_hotel_actor_filter_reveals_nothing(
    api: TestClient, engine: Engine, world: dict
) -> None:
    """Filtering by somebody who acted at another property returns an empty page, not their
    events and not an error."""
    other = build_hotel(api, slug="audit-hotel-four")
    guest = make_guest(api, other)
    api.post(f"/api/v1/hotels/{other}/bookings", json=booking_body(guest))
    stranger = authenticated_client(engine, email="audit-elsewhere@example.test")
    stranger_id = stranger.get("/api/v1/auth/me").json()["public_id"]

    found = events(api, world["hotel"], actor_public_id=stranger_id)

    assert found == []


def test_an_unknown_actor_filter_is_an_empty_page_not_an_error(
    api: TestClient, world: dict
) -> None:
    """A different answer for a real account than for an invented one would be an
    enumeration oracle wearing a filter's clothes."""
    real = authenticated_client
    del real

    known_shape = api.get(audit_url(world["hotel"]), params={"actor_public_id": str(uuid.uuid4())})

    assert known_shape.status_code == 200
    assert known_shape.json()["items"] == []
    assert known_shape.json()["total"] == 0


# ======================================================================================
# Authorization
# ======================================================================================


def test_anonymous_is_refused_with_401(api: TestClient, engine: Engine, world: dict) -> None:
    anonymous = TestClient(create_test_app(engine))

    assert anonymous.get(audit_url(world["hotel"])).status_code == 401


@pytest.mark.parametrize(("role", "expected"), [("viewer", 403), ("staff", 403)])
def test_roles_below_manager_are_refused(
    api: TestClient, engine: Engine, world: dict, role: str, expected: int
) -> None:
    """STAFF generate most of these events. Reviewing the trail of one's colleagues is a
    different capability from doing the work, and this stage starts narrow."""
    client = manager(engine, world["hotel"], f"audit-{role}@example.test", role)

    assert client.get(audit_url(world["hotel"])).status_code == expected


@pytest.mark.parametrize("role", ["manager", "owner"])
def test_manager_and_owner_may_read(
    api: TestClient, engine: Engine, world: dict, role: str
) -> None:
    client = manager(engine, world["hotel"], f"audit-{role}@example.test", role)

    response = client.get(audit_url(world["hotel"]))

    assert response.status_code == 200
    assert response.json()["total"] >= 1


def test_the_refusal_for_a_low_role_names_no_role_of_the_caller(
    api: TestClient, engine: Engine, world: dict
) -> None:
    client = manager(engine, world["hotel"], "audit-viewer-msg@example.test", "viewer")

    body = client.get(audit_url(world["hotel"])).json()

    assert "viewer" not in body["error"]["message"]
    assert "manager" in body["error"]["message"]


def test_a_platform_administrator_is_not_admitted_by_that_privilege(
    api: TestClient, engine: Engine, world: dict
) -> None:
    """The standing rule of this project, applied to its most sensitive table.

    Platform authority governs the three global catalogues, which belong to no hotel. It
    confers nothing hotel-scoped -- ``HotelAccessPolicy`` never consults ``platform_admins``,
    and a structural test asserts it has no path to. So the administrator who may rename an
    amenity for every property still meets the same 404 wall here as any other stranger.
    """
    admin_email = "audit-platform-admin@example.test"
    admin = authenticated_client(engine, email=admin_email)
    grant_platform_admin(engine, admin_email)

    refused = admin.get(audit_url(world["hotel"]))

    assert refused.status_code == 404
    assert refused.json() == admin.get(audit_url(str(uuid.uuid4()))).json()


def test_a_platform_administrator_who_is_a_member_is_admitted_by_the_membership(
    api: TestClient, engine: Engine, world: dict
) -> None:
    """The other half, so the rule above is shown to be about the GRANT rather than about
    platform administrators being refused everywhere."""
    admin_email = "audit-platform-member@example.test"
    admin = authenticated_client(engine, email=admin_email)
    grant_platform_admin(engine, admin_email)
    grant_membership(engine, admin_email, world["hotel"], "manager")

    assert admin.get(audit_url(world["hotel"])).status_code == 200


def test_the_audit_endpoint_offers_no_write_verb(api: TestClient, world: dict) -> None:
    url = audit_url(world["hotel"])

    assert api.post(url, json={}).status_code == 405
    assert api.patch(url, json={}).status_code == 405
    assert api.delete(url).status_code == 405
    assert api.put(url, json={}).status_code == 405


# ======================================================================================
# Listing behaviour
# ======================================================================================


def test_events_are_returned_newest_first(api: TestClient, world: dict) -> None:
    api.patch(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}",
        json={"status": "checked_in"},
    )
    api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={"amount": "10.00", "currency": "EUR", "method": "cash"},
    )

    found = events(api, world["hotel"])

    assert [e["action"] for e in found] == [
        "payment.created",
        "booking.status_changed",
        "booking.created",
    ]


def test_pagination_partitions_the_history_without_repeating_a_row(
    api: TestClient, world: dict
) -> None:
    """The tie-break on ``id`` is what makes this hold: several of these events share an
    ``occurred_at`` to the microsecond."""
    for amount in ("1.00", "2.00", "3.00", "4.00"):
        api.post(
            f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
            json={"amount": amount, "currency": "EUR", "method": "cash"},
        )

    first = events(api, world["hotel"], page=1, page_size=2)
    second = events(api, world["hotel"], page=2, page_size=2)
    third = events(api, world["hotel"], page=3, page_size=2)

    ids = [e["public_id"] for e in first + second + third]
    assert len(ids) == 5
    assert len(set(ids)) == 5


def test_the_total_matches_the_filter_it_accompanies(api: TestClient, world: dict) -> None:
    api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={"amount": "10.00", "currency": "EUR", "method": "cash"},
    )

    body = api.get(audit_url(world["hotel"]), params={"action": "payment.created"}).json()

    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_the_resource_type_filter_narrows_the_page(api: TestClient, world: dict) -> None:
    api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={"amount": "10.00", "currency": "EUR", "method": "cash"},
    )

    found = events(api, world["hotel"], resource_type="payment")

    assert {e["resource_type"] for e in found} == {"payment"}


def test_the_actor_filter_selects_that_persons_events(
    api: TestClient, engine: Engine, world: dict
) -> None:
    colleague = manager(engine, world["hotel"], "audit-colleague@example.test", "manager")
    colleague_id = colleague.get("/api/v1/auth/me").json()["public_id"]
    me = api.get("/api/v1/auth/me").json()["public_id"]

    assert events(api, world["hotel"], actor_public_id=colleague_id) == []
    mine = events(api, world["hotel"], actor_public_id=me)
    assert mine and {e["actor_public_id"] for e in mine} == {me}


def test_the_date_window_selects_by_occurrence(api: TestClient, world: dict) -> None:
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    past = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)

    assert events(api, world["hotel"], occurred_from=future.isoformat()) == []
    assert events(api, world["hotel"], occurred_from=past.isoformat())
    assert events(api, world["hotel"], occurred_to=past.isoformat()) == []


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
        {"action": "booking.exploded"},
        {"resource_type": "spaceship"},
        {"actor_public_id": "not-a-uuid"},
        {"request_id": "not a valid id"},
        {"request_id": "x" * 65},
        {"occurred_from": "yesterday"},
    ],
)
def test_invalid_filters_are_rejected(api: TestClient, world: dict, params: dict) -> None:
    response = api.get(audit_url(world["hotel"]), params=params)

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}


def test_a_validation_failure_echoes_no_input_value(api: TestClient, world: dict) -> None:
    """Pydantic includes the offending input; the shared handler drops it, because a rejected
    payload may carry a secret."""
    body = api.get(audit_url(world["hotel"]), params={"action": "sekrit-value-1234"}).json()

    assert "sekrit-value-1234" not in str(body)


# ======================================================================================
# Privacy and leakage, asserted over every row the suite produced
# ======================================================================================


#: Substrings that must never appear in the DATA an audit row carries.
#:
#: Deliberately checked against every column EXCEPT ``action``. ``auth.password_changed`` is
#: the NAME of an event, not a credential inside one -- and a sweep that could not tell the
#: two apart would flag the audit trail for correctly recording that a password changed. The
#: literal secrets are checked separately below, over every column including that one.
BANNED_SUBSTRINGS = [
    "password",
    "argon2",
    "$2b$",
    "bearer ",
    "eyj",  # a JWT's base64 header, lowercased with the rest
    "authorization",
    "cookie",
    "secret",
    "select ",
    "insert ",
    "update ",
    "traceback",
    "sqlalchemy",
    "psycopg",
]

#: Values this suite actually put into the system. None may appear anywhere at all.
LITERAL_SECRETS = [TEST_PASSWORD, NEW_PASSWORD, "4242"]


def produce_a_broad_sample(api: TestClient, world: dict) -> None:
    """Write one event of as many kinds as a single fixture can reach."""
    api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
        json={"amount": "10.00", "currency": "EUR", "method": "card", "card_last_four": "4242"},
    )
    api.patch(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}",
        json={"status": "checked_in"},
    )
    api.post(
        "/api/v1/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD},
    )


def test_no_audit_row_carries_a_credential_or_sql_in_its_data(
    api: TestClient, world: dict, session: Session
) -> None:
    """A sweep over everything this fixture produced, not a spot check on one row."""
    produce_a_broad_sample(api, world)

    session.rollback()
    sampled = rows(session)
    assert sampled, "no audit rows were produced -- this sweep would be vacuous"
    rendered = str([{k: v for k, v in row.items() if k != "action"} for row in sampled]).lower()

    for banned in BANNED_SUBSTRINGS:
        assert banned not in rendered, f"{banned!r} reached the audit trail"


def test_no_audit_row_contains_a_value_this_suite_treated_as_secret(
    api: TestClient, world: dict, session: Session
) -> None:
    """Every column this time, ``action`` included: an action name may say that a password
    changed, and may never be one."""
    produce_a_broad_sample(api, world)

    session.rollback()
    rendered = str(rows(session))

    for secret in LITERAL_SECRETS:
        assert secret not in rendered, f"{secret!r} reached the audit trail"


def test_the_only_row_naming_a_password_names_the_event_and_nothing_else(
    api: TestClient, world: dict, session: Session
) -> None:
    """The one legitimate occurrence, pinned, so the sweep's exemption cannot widen."""
    produce_a_broad_sample(api, world)

    session.rollback()
    naming = [row for row in rows(session) if "password" in str(row).lower()]

    assert [row["action"] for row in naming] == ["auth.password_changed"]
    assert all(row["details"] == {} for row in naming)


def test_the_api_response_exposes_no_internal_identifier(api: TestClient, world: dict) -> None:
    body = api.get(audit_url(world["hotel"])).json()

    for item in body["items"]:
        assert "id" not in item
        assert "hotel_id" not in item
        assert "actor_user_id" not in item
        for key, value in item.items():
            if key.endswith("_id") and not key.endswith("public_id"):
                continue
            assert not isinstance(value, int), f"{key} is an integer identifier"


def test_the_response_carries_public_identifiers_only(api: TestClient, world: dict) -> None:
    item = api.get(audit_url(world["hotel"])).json()["items"][0]

    assert uuid.UUID(item["public_id"])
    assert uuid.UUID(item["hotel_public_id"])
    assert uuid.UUID(item["actor_public_id"])
    assert uuid.UUID(item["resource_reference"])


def test_an_error_from_this_endpoint_names_no_table(
    api: TestClient, engine: Engine, world: dict
) -> None:
    stranger = authenticated_client(engine, email="audit-error-shape@example.test")

    body = stranger.get(audit_url(world["hotel"])).text

    for leak in ("audit_events", "hotel_id", "SELECT", "psycopg", "sqlalchemy"):
        assert leak not in body


# ======================================================================================
# Performance
# ======================================================================================


def test_the_listing_issues_a_bounded_number_of_statements(
    api: TestClient, engine: Engine, world: dict
) -> None:
    """One count, one page, and one actor-filter lookup at most. An actor resolved per row
    would be the N+1 this project has already had to fix twice."""
    for amount in ("1.00", "2.00", "3.00", "4.00", "5.00"):
        api.post(
            f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/payments",
            json={"amount": amount, "currency": "EUR", "method": "cash"},
        )

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if "audit_events" in statement:
            statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        found = events(api, world["hotel"], page_size=100)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert len(found) == 6
    assert len(statements) <= 2, statements


# ======================================================================================
# Stage 4.5.22 -- the reference filter, and the window that has to make sense
#
# The hotel read surface has existed since Stage 4.5.12. Two things it did not do:
#
#   * it could narrow to a KIND of resource but not to ONE resource, although migration 0007
#     had already built ``ix_audit_events_resource_type_resource_reference`` for exactly that
#     question -- "what happened to this booking";
#   * it accepted a window that ended before it began and answered with an empty page, which
#     is a true statement about an impossible interval and reads like a clean history.
#
# The filter is tenant-scoped by construction: it is applied by the same ``_filtered`` helper
# that applies the scope, and the scope is a positional argument no caller can omit. The tests
# below do not take that on trust.
# ======================================================================================


def other_hotel_with_a_booking(api: TestClient, slug: str) -> tuple[str, str]:
    """A second property with a booking of its own, and both public ids."""
    hotel = build_hotel(api, slug=slug)
    guest = make_guest(api, hotel)
    booking = api.post(f"/api/v1/hotels/{hotel}/bookings", json=booking_body(guest))
    assert booking.status_code == 201, booking.text
    return hotel, str(booking.json()["public_id"])


def test_the_reference_filter_selects_one_resources_history(api: TestClient, world: dict) -> None:
    """Every event returned is about the booking that was named, and there is at least one."""
    selected = events(api, world["hotel"], resource_reference=world["booking"])

    assert selected
    assert {event["resource_reference"] for event in selected} == {world["booking"]}


def test_the_reference_filter_narrows_what_the_unfiltered_page_shows(
    api: TestClient, world: dict
) -> None:
    """Non-vacuity for the filter itself.

    A second booking is made at the SAME property first, so the unfiltered page holds two
    references. Without it the fixture's history is about one resource, and a filter that
    did nothing at all would return exactly what a working one returns.
    """
    second = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings",
        json=booking_body(
            make_guest(api, world["hotel"]),
            check_in=CHECK_IN + dt.timedelta(days=30),
            check_out=CHECK_OUT + dt.timedelta(days=30),
        ),
    )
    assert second.status_code == 201, second.text

    everything = events(api, world["hotel"])
    selected = events(api, world["hotel"], resource_reference=world["booking"])

    assert len({event["resource_reference"] for event in everything}) > 1
    assert len(selected) < len(everything)
    assert {event["resource_reference"] for event in selected} == {world["booking"]}


def test_the_reference_is_matched_exactly_and_not_as_a_pattern(
    api: TestClient, world: dict
) -> None:
    """A prefix of a real reference selects nothing.

    A ``LIKE`` here would turn the filter into a way to sweep the column for references the
    caller was never given -- a slow enumeration oracle over other people's resources.
    """
    prefix = world["booking"][:8]

    assert prefix
    assert events(api, world["hotel"], resource_reference=prefix) == []
    assert events(api, world["hotel"], resource_reference=world["booking"])


def test_an_unknown_reference_is_an_empty_page_not_an_error(api: TestClient, world: dict) -> None:
    """Same shape as the unknown-actor filter: a reference that names nothing cannot be used
    to learn whether it names something."""
    response = api.get(audit_url(world["hotel"]), params={"resource_reference": str(uuid.uuid4())})

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


def test_a_reference_from_another_hotel_reveals_nothing(api: TestClient, world: dict) -> None:
    """The tenant test for the new filter, and the one that would catch it being applied
    outside the scope.

    Two properties, each with a real booking. Naming one property's booking while asking the
    other property's history returns an empty page -- not the event, and not an error that
    would confirm the reference exists somewhere.
    """
    other, other_booking = other_hotel_with_a_booking(api, "audit-hotel-reference")

    assert events(api, world["hotel"], resource_reference=other_booking) == []
    assert events(api, other, resource_reference=world["booking"]) == []
    assert events(api, world["hotel"], resource_reference=world["booking"])
    assert events(api, other, resource_reference=other_booking)


def test_the_type_and_reference_filters_combine(api: TestClient, world: dict) -> None:
    """Both applied, and the pair is the index's own column order."""
    matching = events(
        api, world["hotel"], resource_type="booking", resource_reference=world["booking"]
    )
    mismatched = events(
        api, world["hotel"], resource_type="payment", resource_reference=world["booking"]
    )

    assert matching
    assert {event["resource_type"] for event in matching} == {"booking"}
    assert mismatched == []


def test_an_inverted_window_is_refused(api: TestClient, world: dict) -> None:
    """It used to be an empty page. No event occurs after the later bound and before the
    earlier one, so the empty page was true -- and indistinguishable from a clean history."""
    now = dt.datetime.now(dt.UTC)
    response = api.get(
        audit_url(world["hotel"]),
        params={
            "occurred_from": (now + dt.timedelta(days=1)).isoformat(),
            "occurred_to": (now - dt.timedelta(days=1)).isoformat(),
        },
    )

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}


def test_a_window_whose_bounds_are_equal_is_accepted(api: TestClient, world: dict) -> None:
    """Both bounds are inclusive, so a single instant is a legal window rather than an
    inverted one. The boundary is tested because ``<`` and ``<=`` differ exactly here."""
    moment = dt.datetime.now(dt.UTC).isoformat()
    response = api.get(
        audit_url(world["hotel"]), params={"occurred_from": moment, "occurred_to": moment}
    )

    assert response.status_code == 200, response.text


def test_one_sided_windows_are_still_accepted(api: TestClient, world: dict) -> None:
    """The check needs both bounds to have anything to compare, and must not reject a window
    that has only one."""
    now = dt.datetime.now(dt.UTC)

    assert (
        api.get(
            audit_url(world["hotel"]),
            params={"occurred_from": (now - dt.timedelta(days=1)).isoformat()},
        ).status_code
        == 200
    )
    assert (
        api.get(
            audit_url(world["hotel"]),
            params={"occurred_to": (now + dt.timedelta(days=1)).isoformat()},
        ).status_code
        == 200
    )


def test_a_non_member_meets_the_wall_before_the_window_is_judged(
    api: TestClient, engine: Engine, world: dict
) -> None:
    """Authorization first, validation second -- the order :class:`AnalyticsService` uses.

    A caller who is not a member gets the hotel's 404 whatever they put in the query string.
    If the window were judged first, a malformed one would answer 422 for a property the
    caller may not know exists, and the difference between 404 and 422 would say it does.
    """
    now = dt.datetime.now(dt.UTC)
    inverted = {
        "occurred_from": (now + dt.timedelta(days=1)).isoformat(),
        "occurred_to": (now - dt.timedelta(days=1)).isoformat(),
    }
    stranger = authenticated_client(engine, email="audit-window-stranger@example.test")

    refused = stranger.get(audit_url(world["hotel"]), params=inverted)
    invented = stranger.get(audit_url(str(uuid.uuid4())), params=inverted)

    assert refused.status_code == 404, refused.text
    assert refused.json() == invented.json()


def test_the_window_refusal_echoes_neither_bound(api: TestClient, world: dict) -> None:
    """The message names the two parameters, never the values -- the same rule the shared
    validation handler applies to every other rejected input."""
    now = dt.datetime.now(dt.UTC)
    later = (now + dt.timedelta(days=365)).isoformat()
    earlier = (now - dt.timedelta(days=365)).isoformat()

    body = api.get(
        audit_url(world["hotel"]),
        params={"occurred_from": later, "occurred_to": earlier},
    ).json()

    assert later not in str(body)
    assert earlier not in str(body)


@pytest.mark.parametrize(
    "params",
    [
        {"resource_reference": ""},
        {"resource_reference": "x" * 65},
    ],
    ids=["empty", "too-long"],
)
def test_a_reference_outside_the_columns_bounds_is_rejected(
    api: TestClient, world: dict, params: dict
) -> None:
    """1..64 characters, matching ``ck_audit_events_resource_reference_bounded``. A value the
    column could not hold cannot name a row, and is refused rather than queried."""
    response = api.get(audit_url(world["hotel"]), params=params)

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}


def test_the_reference_filter_exposes_no_internal_identifier(api: TestClient, world: dict) -> None:
    """The filter is expressed in a value the client already holds, and the page it returns
    carries no key the client did not."""
    body = api.get(
        audit_url(world["hotel"]), params={"resource_reference": world["booking"]}
    ).json()

    for event in body["items"]:
        assert "id" not in event
        assert "hotel_id" not in event
        assert "actor_user_id" not in event
