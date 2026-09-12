"""Booking deletion, and the audit event that outlives it. Real PostgreSQL.

Stage 4.5.15. Deleting a booking is the one operation in this domain whose audit event is the
only thing that survives its subject. Cancelling leaves a row somebody can go and look at;
DELETE removes the booking, its allocations and its priced nights outright, so if the event is
missing there is nothing at all to say the stay existed.

That makes the invariant sharper than "an event is written". It is:

    exactly one ``booking.deleted`` event per successful deletion, in the deletion's own
    transaction, and none at all for any deletion that did not happen.

Both halves need a live database. The "none at all" half is the one a mock cannot test: it
depends on PostgreSQL rolling back a staged INSERT, on a foreign key firing at flush, and on
two real connections contending for one row.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import (
    authenticated_client,
    create_test_app,
    grant_membership,
    requires_postgres,
)

pytestmark = requires_postgres

OWNER_EMAIL = "deletion-audit-owner@example.test"

CHECK_IN = dt.date(2027, 9, 6)
CHECK_OUT = dt.date(2027, 9, 9)


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
    client = authenticated_client(engine, email=OWNER_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE audit_events_archive"))
        cleanup.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build_hotel(api: TestClient, slug: str = "deletion-audit-hotel") -> str:
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


def make_booking(
    api: TestClient, hotel: str, *, room: str = "101", status: str = "confirmed"
) -> tuple[str, str]:
    """A booking on *hotel*. Returns (public_id, reference)."""
    guest = api.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    reference = f"BK-{uuid.uuid4().hex[:8].upper()}"
    response = api.post(
        f"/api/v1/hotels/{hotel}/bookings",
        json={
            "guest_public_id": guest,
            "reference": reference,
            "check_in_date": str(CHECK_IN),
            "check_out_date": str(CHECK_OUT),
            "status": status,
            "total_amount": "360.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": room,
                    "nights": [
                        {"stay_date": str(CHECK_IN + dt.timedelta(days=n))} for n in range(3)
                    ],
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["public_id"]), reference


@pytest.fixture
def world(api: TestClient) -> dict[str, str]:
    hotel = build_hotel(api)
    booking, reference = make_booking(api, hotel)
    return {"hotel": hotel, "booking": booking, "reference": reference}


def booking_url(hotel: str, booking: str) -> str:
    return f"/api/v1/hotels/{hotel}/bookings/{booking}"


def deletion_events(session: Session, **where: object) -> list[dict[str, Any]]:
    """`booking.deleted` rows read straight from the table, bypassing every API."""
    session.rollback()
    clause = "".join(f" AND {column} = :{column}" for column in where)
    return [
        dict(row)
        for row in session.execute(
            sa.text(
                f"SELECT * FROM audit_events WHERE action = 'booking.deleted'{clause} ORDER BY id"
            ),
            where,
        ).mappings()
    ]


def booking_exists(session: Session, public_id: str) -> bool:
    session.rollback()
    return bool(
        session.scalar(
            sa.text("SELECT count(*) FROM bookings WHERE public_id = CAST(:p AS uuid)"),
            {"p": public_id},
        )
    )


def member(engine: Engine, hotel: str, role: str) -> TestClient:
    email = f"deletion-audit-{role}@example.test"
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, hotel, role)
    return client


# ======================================================================================
# The event is written
# ======================================================================================


def test_a_successful_deletion_writes_exactly_one_event(
    api: TestClient, world: dict, session: Session
) -> None:
    response = api.delete(booking_url(world["hotel"], world["booking"]))

    assert response.status_code == 204
    assert not booking_exists(session, world["booking"])
    assert len(deletion_events(session)) == 1


def test_the_event_names_the_deleted_booking(
    api: TestClient, world: dict, session: Session
) -> None:
    """The public id is captured BEFORE the delete -- the repository expunges the instance, so
    reading it afterwards would raise."""
    api.delete(booking_url(world["hotel"], world["booking"]))

    event = deletion_events(session)[0]
    assert event["resource_reference"] == world["booking"]
    assert event["resource_type"] == "booking"
    assert event["action"] == "booking.deleted"


def test_the_event_carries_the_hotel_of_the_deleted_booking(
    api: TestClient, world: dict, session: Session
) -> None:
    api.delete(booking_url(world["hotel"], world["booking"]))

    session.rollback()
    hotel_id = session.scalar(
        sa.text("SELECT id FROM hotels WHERE public_id = CAST(:p AS uuid)"),
        {"p": world["hotel"]},
    )
    assert deletion_events(session)[0]["hotel_id"] == hotel_id


def test_the_event_carries_the_authenticated_actor(
    api: TestClient, world: dict, session: Session
) -> None:
    """From the request context, not from the payload -- the DELETE has no body at all."""
    me = api.get("/api/v1/auth/me").json()["public_id"]
    api.delete(booking_url(world["hotel"], world["booking"]))

    session.rollback()
    actor_id = session.scalar(
        sa.text("SELECT id FROM users WHERE public_id = CAST(:p AS uuid)"), {"p": me}
    )
    assert deletion_events(session)[0]["actor_user_id"] == actor_id


def test_the_event_carries_the_request_id(api: TestClient, world: dict, session: Session) -> None:
    response = api.delete(
        booking_url(world["hotel"], world["booking"]),
        headers={"X-Request-ID": "deletion-correlation-1"},
    )

    assert response.headers["x-request-id"] == "deletion-correlation-1"
    assert deletion_events(session)[0]["request_id"] == "deletion-correlation-1"


def test_the_event_records_the_status_and_reference_it_had(
    api: TestClient, world: dict, session: Session
) -> None:
    """Enough to recognise the stay in a forensic review; not a copy of the booking."""
    api.delete(booking_url(world["hotel"], world["booking"]))

    details = deletion_events(session)[0]["details"]
    assert details == {"status": "confirmed", "reference": world["reference"]}


def test_a_cancelled_booking_records_the_status_it_was_deleted_in(
    api: TestClient, world: dict, session: Session
) -> None:
    api.patch(booking_url(world["hotel"], world["booking"]), json={"status": "cancelled"})

    api.delete(booking_url(world["hotel"], world["booking"]))

    assert deletion_events(session)[0]["details"]["status"] == "cancelled"


def test_occurred_at_is_stamped_by_the_database(
    api: TestClient, world: dict, session: Session
) -> None:
    api.delete(booking_url(world["hotel"], world["booking"]))

    assert deletion_events(session)[0]["occurred_at"] is not None


def test_deleting_two_bookings_writes_two_distinct_events(
    api: TestClient, world: dict, session: Session
) -> None:
    second, _ = make_booking(api, world["hotel"], room="102")

    api.delete(booking_url(world["hotel"], world["booking"]))
    api.delete(booking_url(world["hotel"], second))

    events = deletion_events(session)
    assert len(events) == 2
    assert {e["resource_reference"] for e in events} == {world["booking"], second}


# ======================================================================================
# No deletion, no event
# ======================================================================================


def test_an_anonymous_deletion_writes_no_event(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    anonymous = TestClient(create_test_app(engine))

    response = anonymous.delete(booking_url(world["hotel"], world["booking"]))

    assert response.status_code == 401
    assert booking_exists(session, world["booking"])
    assert deletion_events(session) == []


def test_a_non_member_deletion_writes_no_event(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """The hotel's own 404 wall: a stranger cannot even learn the property exists."""
    stranger = authenticated_client(engine, email="deletion-audit-stranger@example.test")

    response = stranger.delete(booking_url(world["hotel"], world["booking"]))

    assert response.status_code == 404
    assert booking_exists(session, world["booking"])
    assert deletion_events(session) == []


@pytest.mark.parametrize("role", ["viewer", "staff"])
def test_a_role_below_manager_writes_no_event(
    api: TestClient, engine: Engine, world: dict, session: Session, role: str
) -> None:
    """DELETE requires MANAGER, unchanged by this stage. Authorization runs before the service
    is assembled, so the recording call is never reached."""
    client = member(engine, world["hotel"], role)

    response = client.delete(booking_url(world["hotel"], world["booking"]))

    assert response.status_code == 403
    assert booking_exists(session, world["booking"])
    assert deletion_events(session) == []


@pytest.mark.parametrize("role", ["manager", "owner"])
def test_manager_and_owner_may_still_delete(
    api: TestClient, engine: Engine, world: dict, session: Session, role: str
) -> None:
    client = member(engine, world["hotel"], role)

    assert client.delete(booking_url(world["hotel"], world["booking"])).status_code == 204
    assert len(deletion_events(session)) == 1


def test_an_unknown_booking_writes_no_event(api: TestClient, world: dict, session: Session) -> None:
    response = api.delete(booking_url(world["hotel"], str(uuid.uuid4())))

    assert response.status_code == 404
    assert deletion_events(session) == []


def test_a_refused_deletion_writes_no_event(api: TestClient, world: dict, session: Session) -> None:
    """A booking with a payment is refused by ``payments``' ON DELETE RESTRICT.

    The refusal fires at the DELETE statement, before the recording call is reached -- so this
    is not a rollback, it is a path the event never reaches at all.
    """
    api.post(
        f"{booking_url(world['hotel'], world['booking'])}/payments",
        json={"amount": "100.00", "currency": "EUR", "method": "card"},
    )

    response = api.delete(booking_url(world["hotel"], world["booking"]))

    assert response.status_code == 409
    assert booking_exists(session, world["booking"])
    assert deletion_events(session) == []


def test_a_second_deletion_writes_no_second_event(
    api: TestClient, world: dict, session: Session
) -> None:
    """Idempotency, as the existing DELETE semantics already define it: the booking is gone, so
    the second attempt is a 404 and records nothing."""
    assert api.delete(booking_url(world["hotel"], world["booking"])).status_code == 204

    second = api.delete(booking_url(world["hotel"], world["booking"]))

    assert second.status_code == 404
    assert len(deletion_events(session)) == 1


# ======================================================================================
# Tenant isolation
# ======================================================================================


def test_one_hotels_member_cannot_delete_anothers_booking(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """A manager at hotel B, aiming at hotel A's booking through B's own URL and through A's."""
    other_hotel = build_hotel(api, slug="deletion-audit-hotel-two")
    intruder = member(engine, other_hotel, "manager")

    through_own = intruder.delete(booking_url(other_hotel, world["booking"]))
    through_theirs = intruder.delete(booking_url(world["hotel"], world["booking"]))

    assert through_own.status_code == 404
    assert through_theirs.status_code == 404
    assert booking_exists(session, world["booking"])
    assert deletion_events(session) == []


def test_the_event_belongs_to_the_deleting_hotel_only(
    api: TestClient, world: dict, session: Session
) -> None:
    other_hotel = build_hotel(api, slug="deletion-audit-hotel-three")
    other_booking, _ = make_booking(api, other_hotel)

    api.delete(booking_url(other_hotel, other_booking))

    session.rollback()
    other_id = session.scalar(
        sa.text("SELECT id FROM hotels WHERE public_id = CAST(:p AS uuid)"), {"p": other_hotel}
    )
    events = deletion_events(session)
    assert len(events) == 1
    assert events[0]["hotel_id"] == other_id


# ======================================================================================
# Transaction integrity
# ======================================================================================


def test_an_audit_insert_failure_rolls_the_deletion_back(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """The mandated case, produced by a REAL database failure rather than a raised mock.

    ``audit_events.actor_user_id`` is a foreign key to ``users`` with ON DELETE RESTRICT. An
    ``AuditTrail`` bound to an actor whose row does not exist makes the audit INSERT fail at
    flush with a genuine foreign-key violation -- after the booking's DELETE has already been
    staged in the same transaction.

    Required outcome: the booking is still there. If the deletion could commit without its
    event, the invariant this stage exists for would be a hope rather than a guarantee.
    """
    from app.models.user import User
    from app.repositories.audit import AuditRepository
    from app.repositories.booking import BookingRepository
    from app.repositories.guest import GuestRepository
    from app.repositories.hotel import HotelRepository
    from app.repositories.membership import MembershipRepository
    from app.repositories.payment import PaymentRepository
    from app.repositories.pricing import PricingRepository
    from app.repositories.room_type import RoomTypeRepository
    from app.services.audit import AuditTrail
    from app.services.authorization import HotelAccessPolicy
    from app.services.booking import BookingService
    from app.services.pricing import PricingService
    from app.services.scope import HotelScopeResolver

    session.rollback()
    caller = session.scalars(sa.select(User).where(User.email == OWNER_EMAIL)).one()

    # An actor that is not in `users`. Nothing else about the request changes.
    phantom = User(
        email="phantom@example.test", password_hash="x", full_name="Phantom", is_active=True
    )
    phantom.id = 9_999_999

    policy = HotelAccessPolicy(caller, MembershipRepository(session))
    scope = HotelScopeResolver(HotelRepository(session), RoomTypeRepository(session), policy)
    service = BookingService(
        session,
        BookingRepository(session),
        GuestRepository(session),
        scope,
        AuditTrail(AuditRepository(session), phantom),
        # Stage 4.5.23: the booking service prices its own nights, so a hand-built
        # one needs the same collaborator the dependency wiring gives it.
        PricingService(PricingRepository(session)),
        # Stage 4.5.24: repricing reads the ledger to say what is refundable, so a
        # hand-built service needs the same collaborator the wiring gives it.
        PaymentRepository(session),
    )

    from app.core.errors import InternalFaultError

    # Stage 4.5.17: this is a server fault, not a client conflict. What Stage 4.5.15 asserts
    # here is unchanged -- the deletion must not commit without its event.
    with pytest.raises(InternalFaultError):
        service.delete(uuid.UUID(world["hotel"]), uuid.UUID(world["booking"]))

    assert booking_exists(session, world["booking"]), "the deletion committed without its event"
    assert deletion_events(session) == []


def test_the_failure_response_leaks_no_database_internals(
    api: TestClient, world: dict, session: Session
) -> None:
    """A refused deletion is translated by the existing handler, whatever refused it."""
    api.post(
        f"{booking_url(world['hotel'], world['booking'])}/payments",
        json={"amount": "50.00", "currency": "EUR", "method": "cash"},
    )

    body = api.delete(booking_url(world["hotel"], world["booking"])).text

    for leak in (
        "audit_events",
        "bookings",
        "fk_",
        "ck_",
        "23503",
        "23001",
        "SQLSTATE",
        "psycopg",
        "sqlalchemy",
        "Traceback",
    ):
        assert leak not in body, f"{leak!r} reached the client"


def test_a_rolled_back_transaction_leaves_neither_the_deletion_nor_the_event(
    api: TestClient, world: dict, session: Session
) -> None:
    """Real PostgreSQL transaction semantics, driven directly.

    The delete and the event are staged on one session, then the transaction is rolled back
    instead of committed. Both must be gone -- which is only true because they were never two
    transactions.
    """
    from app.models.audit import AuditEvent
    from app.models.booking import Booking

    session.rollback()
    booking = session.scalars(
        sa.select(Booking).where(Booking.public_id == uuid.UUID(world["booking"]))
    ).one()
    hotel_id = booking.hotel_id
    public_id = booking.public_id

    session.execute(sa.delete(Booking).where(Booking.id == booking.id))
    session.add(
        AuditEvent(
            hotel_id=hotel_id,
            actor_user_id=None,
            action="booking.deleted",
            resource_type="booking",
            resource_reference=str(public_id),
            request_id=None,
            details={},
        )
    )
    session.flush()

    # Both are visible on this connection, inside the transaction.
    assert (
        session.scalar(
            sa.text("SELECT count(*) FROM audit_events WHERE action = 'booking.deleted'")
        )
        == 1
    )

    session.rollback()

    assert booking_exists(session, world["booking"])
    assert deletion_events(session) == []


# ======================================================================================
# Concurrency
# ======================================================================================


def test_two_concurrent_deletions_produce_one_deletion_and_one_event(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """Two authorized DELETEs for one booking, forced to interleave.

    The second blocks on the first's row lock; when it wakes, its ``DELETE`` removes nothing.
    That is not an error in SQL, so without the row-count check the service would happily
    record a second ``booking.deleted`` for a booking it did not delete. The repository returns
    the count and the service turns zero into the 404 it is.
    """
    both_ready = threading.Barrier(2, timeout=30)
    codes: dict[int, int] = {}

    def attempt(index: int) -> None:
        client = authenticated_client(engine, email=OWNER_EMAIL)
        both_ready.wait()
        codes[index] = client.delete(booking_url(world["hotel"], world["booking"])).status_code

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    assert sorted(codes.values()) == [204, 404], codes
    assert not booking_exists(session, world["booking"])
    assert len(deletion_events(session)) == 1


def test_concurrent_deletions_of_different_bookings_each_record_once(
    api: TestClient, engine: Engine, world: dict, session: Session
) -> None:
    """Contention on one booking must not suppress an unrelated one."""
    second, _ = make_booking(api, world["hotel"], room="102")
    both_ready = threading.Barrier(2, timeout=30)
    codes: dict[str, int] = {}

    def attempt(booking: str) -> None:
        client = authenticated_client(engine, email=OWNER_EMAIL)
        both_ready.wait()
        codes[booking] = client.delete(booking_url(world["hotel"], booking)).status_code

    threads = [
        threading.Thread(target=attempt, args=(world["booking"],)),
        threading.Thread(target=attempt, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)

    assert set(codes.values()) == {204}
    events = deletion_events(session)
    assert len(events) == 2
    assert {e["resource_reference"] for e in events} == {world["booking"], second}


# ======================================================================================
# The read surface
# ======================================================================================


def test_the_event_appears_in_the_hotels_audit_listing(
    api: TestClient, engine: Engine, world: dict
) -> None:
    manager = member(engine, world["hotel"], "manager")
    me = api.get("/api/v1/auth/me").json()["public_id"]
    api.delete(
        booking_url(world["hotel"], world["booking"]),
        headers={"X-Request-ID": "deletion-listing-1"},
    )

    listed = manager.get(
        f"/api/v1/hotels/{world['hotel']}/audit-events", params={"action": "booking.deleted"}
    ).json()["items"]

    assert len(listed) == 1
    assert listed[0]["resource_type"] == "booking"
    assert listed[0]["resource_reference"] == world["booking"]
    assert listed[0]["actor_public_id"] == me
    assert listed[0]["request_id"] == "deletion-listing-1"
    assert listed[0]["hotel_public_id"] == world["hotel"]


def test_another_hotel_cannot_see_the_deletion_event(
    api: TestClient, engine: Engine, world: dict
) -> None:
    other_hotel = build_hotel(api, slug="deletion-audit-hotel-four")
    outsider = member(engine, other_hotel, "manager")
    api.delete(booking_url(world["hotel"], world["booking"]))

    own = outsider.get(f"/api/v1/hotels/{other_hotel}/audit-events", params={"page_size": 100})
    theirs = outsider.get(f"/api/v1/hotels/{world['hotel']}/audit-events")

    assert own.status_code == 200
    assert not any(item["action"] == "booking.deleted" for item in own.json()["items"])
    assert theirs.status_code == 404


def test_the_platform_audit_scope_is_unchanged(
    api: TestClient, engine: Engine, world: dict
) -> None:
    """A hotel-scoped event must not appear on the platform surface. Stage 4.5.13's partition
    is not broadened by adding an action to it."""
    from tests.integration.conftest import grant_platform_admin

    admin_email = "deletion-audit-platform@example.test"
    admin = authenticated_client(engine, email=admin_email)
    grant_platform_admin(engine, admin_email)
    api.delete(booking_url(world["hotel"], world["booking"]))

    listed = admin.get("/api/v1/platform/audit-events", params={"page_size": 100})

    assert listed.status_code == 200
    assert not any(item["action"] == "booking.deleted" for item in listed.json()["items"])


# ======================================================================================
# Immutability and leakage
# ======================================================================================


def test_the_generated_event_cannot_be_updated(
    api: TestClient, world: dict, session: Session
) -> None:
    api.delete(booking_url(world["hotel"], world["booking"]))

    with pytest.raises(sa.exc.DatabaseError, match="append-only"):
        session.execute(sa.text("UPDATE audit_events SET action = 'booking.created' WHERE id > 0"))
    session.rollback()


def test_the_generated_event_cannot_be_deleted(
    api: TestClient, world: dict, session: Session
) -> None:
    api.delete(booking_url(world["hotel"], world["booking"]))

    with pytest.raises(sa.exc.DatabaseError, match="append-only"):
        session.execute(sa.text("DELETE FROM audit_events WHERE action = 'booking.deleted'"))
    session.rollback()


def test_the_event_carries_no_guest_or_payment_information(
    api: TestClient, world: dict, session: Session
) -> None:
    """The booking had a guest named Ada Lovelace. The event says nothing about her."""
    api.delete(booking_url(world["hotel"], world["booking"]))

    rendered = str(deletion_events(session)[0])
    for banned in ("Ada", "Lovelace", "guest", "120.00", "360.00", "card", "password", "Bearer "):
        assert banned not in rendered, f"{banned!r} reached the audit trail"


def test_the_listed_event_exposes_no_internal_identifier(
    api: TestClient, engine: Engine, world: dict
) -> None:
    manager = member(engine, world["hotel"], "manager")
    api.delete(booking_url(world["hotel"], world["booking"]))

    item = manager.get(
        f"/api/v1/hotels/{world['hotel']}/audit-events", params={"action": "booking.deleted"}
    ).json()["items"][0]

    assert "id" not in item
    assert "hotel_id" not in item
    assert "actor_user_id" not in item
    for key, value in item.items():
        assert not isinstance(value, int), f"{key} is an integer identifier"


def test_the_delete_response_carries_no_body(api: TestClient, world: dict) -> None:
    """204 and nothing else. The audit event is an internal side effect, not a payload."""
    response = api.delete(booking_url(world["hotel"], world["booking"]))

    assert response.status_code == 204
    assert response.content == b""


# ======================================================================================
# The schema really does permit the action
# ======================================================================================


def test_the_check_constraint_permits_booking_deleted(session: Session) -> None:
    """Migration 0009's whole purpose, asserted against the live constraint.

    Before it, this INSERT failed with SQLSTATE 23514 -- which is why the stage stopped and
    asked before writing a migration.
    """
    definition = session.scalar(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = 'audit_events' AND c.conname = 'ck_audit_events_action_valid'"
        )
    )

    assert "booking.deleted" in str(definition)


def test_the_constraint_still_refuses_an_invented_action(session: Session) -> None:
    """Widened by exactly one value, not opened."""
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
            sa.text(
                "INSERT INTO audit_events (action, resource_type, resource_reference) "
                "VALUES ('booking.exploded', 'booking', 'x')"
            )
        )
    session.rollback()


def test_the_append_only_trigger_survived_the_migration(session: Session) -> None:
    """0009 altered a CHECK. It must not have disturbed the trigger beside it."""
    triggers = {
        row[0]
        for row in session.execute(
            sa.text(
                "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'audit_events' AND NOT t.tgisinternal"
            )
        )
    }

    assert triggers == {"trg_audit_events_append_only"}
