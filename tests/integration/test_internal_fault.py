"""An internal fault, end to end. Real PostgreSQL, real HTTP.

Stage 4.5.17. Stage 4.5.16 stopped an audit-layer integrity failure being described as a
booking dependency, which removed a false statement and left a misleading one: the client still
received 409. A conflict tells a caller that their request disagrees with stored data and
invites them to change it and retry -- advice that is wrong in every particular when the fault
is in the server's own audit subsystem. It also files a server fault in the 4xx class, where
the monitoring that watches 5xx never sees it.

This suite drives the real endpoint over HTTP with a poisoned audit actor and asserts the whole
contract at once: **500, a generic body, the booking still present, no audit event, and a
request id to trace it by.**

The failure is produced by injecting an actor whose ``users`` row does not exist, through the
application's own dependency override -- so the audit INSERT fails on a genuine foreign key
inside the deletion's transaction. Nothing is mocked and no exception is raised by hand.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import GENERIC_SERVER_MESSAGE
from tests.integration.conftest import (
    TEST_SECRET,
    authenticated_client,
    register_and_login,
    requires_postgres,
)

pytestmark = requires_postgres

OWNER_EMAIL = "internal-fault-owner@example.test"

CHECK_IN = dt.date(2028, 7, 3)
CHECK_OUT = dt.date(2028, 7, 6)

#: The actor id injected to break the audit INSERT. Asserted absent from every response.
PHANTOM_ACTOR_ID = 9_876_543


# ======================================================================================
# Scaffolding
# ======================================================================================


def hotel_payload() -> dict[str, object]:
    return {
        "slug": "internal-fault-hotel",
        "name": "Internal Fault Hotel",
        "address_line1": "1 Dionysiou Areopagitou",
        "city": "Athens",
        "country_code": "GR",
        "timezone": "Europe/Athens",
        "currency": "EUR",
    }


# Cleanup is the shared ``session`` fixture's, which already truncates every table this module
# writes to. Every test therefore requests it, whether or not it queries anything.
#
# An earlier draft added a second cleanup fixture of its own, and it deadlocked: fixtures are
# finalised in reverse setup order, so that fixture's TRUNCATE ran while the ``session``
# fixture still held an open read transaction, and TRUNCATE needs ACCESS EXCLUSIVE. The suite
# did not fail -- it hung. One cleanup owner, and the problem cannot recur.


def build_world(client: TestClient) -> dict[str, str]:
    hotel = str(client.post("/api/v1/hotels", json=hotel_payload()).json()["public_id"])
    client.post(
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
    client.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": "101"})
    guest = client.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    booking = client.post(
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
    return {"hotel": hotel, "guest": str(guest), "booking": str(booking)}


def poisoned_client(engine: Engine) -> tuple[TestClient, dict[str, str]]:
    """An authenticated client whose audit trail is bound to an actor that does not exist.

    The override is on ``get_audit_trail`` -- the application's own seam -- so everything else
    about the request is real: the same route, the same authorization, the same service, the
    same transaction. Only the audit actor is impossible, which makes the audit INSERT fail on
    ``fk_audit_events_actor_user_id_users`` inside the deletion's transaction.
    """
    from fastapi import Depends

    from app.api.deps import get_audit_trail, get_db
    from app.core.config import Settings
    from app.main import create_app
    from app.models.user import User
    from app.repositories.audit import AuditRepository
    from app.services.audit import AuditTrail

    app = create_app(Settings(environment="test", secret_key=TEST_SECRET))
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    bootstrap = TestClient(app)
    token = register_and_login(bootstrap, OWNER_EMAIL)
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    world = build_world(client)

    def phantom_trail(db: Session = Depends(get_db)) -> AuditTrail:
        """The real trail, on the REQUEST's session, bound to an actor that does not exist.

        Taking the session through ``Depends(get_db)`` rather than opening one is the whole
        point: the audit write must land in the deletion's own transaction, which is what makes
        its failure roll the deletion back. An earlier draft built its own session here -- the
        audit INSERT then ran on a second connection, so it neither joined the transaction nor
        released it, and the leaked open transaction blocked the cleanup TRUNCATE until the
        suite hung.
        """
        ghost = User(email="ghost@example.test", password_hash="x", full_name="Ghost")
        ghost.id = PHANTOM_ACTOR_ID
        return AuditTrail(AuditRepository(db), ghost)

    # Installed only AFTER the world is built, so creating the booking is audited normally and
    # only the deletion meets the broken actor.
    app.dependency_overrides[get_audit_trail] = phantom_trail
    return client, world


def booking_url(world: dict[str, str]) -> str:
    return f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}"


def rows(session: Session, sql: str, **params: Any) -> int:
    session.rollback()
    return int(session.scalar(sa.text(sql), params) or 0)


# ======================================================================================
# The error class
# ======================================================================================


def test_the_internal_fault_maps_to_500() -> None:
    from app.core.errors import InternalFaultError

    assert InternalFaultError().status_code == 500


def test_the_internal_fault_is_not_a_client_conflict() -> None:
    """Distinct from every error class that describes something the caller can act on."""
    from app.core.errors import (
        AppError,
        ConflictError,
        ForbiddenError,
        InternalFaultError,
        NotFoundError,
        ValidationError,
    )

    fault = InternalFaultError()

    assert isinstance(fault, AppError)
    for client_error in (ConflictError, NotFoundError, ValidationError, ForbiddenError):
        assert not isinstance(fault, client_error)
        assert not issubclass(client_error, InternalFaultError)


def test_the_internal_fault_carries_the_standard_generic_sentence() -> None:
    from app.core.errors import InternalFaultError

    fault = InternalFaultError()

    assert fault.message == GENERIC_SERVER_MESSAGE
    assert fault.code == "INTERNAL_ERROR"


def test_the_internal_fault_is_indistinguishable_from_any_other_500() -> None:
    """Deliberate. A client able to tell an audit failure from an unexpected exception would be
    learning about the server's internals from an error response."""
    from app.core.errors import InternalFaultError

    # The catch-all handler for an unexpected exception uses this same code and this same
    # sentence. That is the point: from outside, the two are one response.
    assert InternalFaultError().code == "INTERNAL_ERROR"
    assert InternalFaultError().message == GENERIC_SERVER_MESSAGE


def test_the_factory_returns_the_fault_without_a_custom_message() -> None:
    from app.core.errors import InternalFaultError, internal_fault

    class _Diag:
        constraint_name = "fk_audit_events_actor_user_id_users"
        table_name = "audit_events"

    class _Orig:
        diag = _Diag()
        sqlstate = "23503"

    class _Error(Exception):
        orig = _Orig()

    fault = internal_fault(_Error())

    assert isinstance(fault, InternalFaultError)
    assert fault.message == GENERIC_SERVER_MESSAGE
    assert "audit_events" not in str(fault)
    assert "fk_audit_events_actor_user_id_users" not in str(fault)


# ======================================================================================
# The endpoint, end to end
# ======================================================================================


def test_an_audit_failure_returns_500(engine: Engine, session: Session) -> None:
    client, world = poisoned_client(engine)

    response = client.delete(booking_url(world))

    assert response.status_code == 500, response.text


def test_the_500_uses_the_shared_error_envelope(engine: Engine, session: Session) -> None:
    client, world = poisoned_client(engine)

    body = client.delete(booking_url(world)).json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["message"] == GENERIC_SERVER_MESSAGE
    assert body["error"]["details"] == []


def test_the_500_carries_a_usable_request_id(engine: Engine, session: Session) -> None:
    """The existing contract from Stage 4.5.4.1, not a second mechanism: the id the client is
    handed is the id the operator greps for."""
    client, world = poisoned_client(engine)

    response = client.delete(booking_url(world), headers={"X-Request-ID": "fault-correlation-1"})

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "fault-correlation-1"


def test_a_generated_request_id_is_returned_on_the_500(engine: Engine, session: Session) -> None:
    client, world = poisoned_client(engine)

    response = client.delete(booking_url(world))

    generated = response.headers["x-request-id"]
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", generated), generated


def test_the_500_still_carries_the_security_headers(engine: Engine, session: Session) -> None:
    """Stage 4.5.6's headers are additive and must not be lost on an error path."""
    client, world = poisoned_client(engine)

    response = client.delete(booking_url(world))

    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers


# ======================================================================================
# Transaction: nothing half-happened
# ======================================================================================


def test_the_booking_survives_the_internal_fault(engine: Engine, session: Session) -> None:
    client, world = poisoned_client(engine)

    assert client.delete(booking_url(world)).status_code == 500

    assert (
        rows(
            session,
            "SELECT count(*) FROM bookings WHERE public_id = CAST(:p AS uuid)",
            p=world["booking"],
        )
        == 1
    ), "the deletion committed despite the audit failure"


def test_no_deletion_event_survives_the_internal_fault(engine: Engine, session: Session) -> None:
    client, world = poisoned_client(engine)

    client.delete(booking_url(world))

    assert rows(session, "SELECT count(*) FROM audit_events WHERE action = 'booking.deleted'") == 0


def test_the_booking_is_still_readable_afterwards(engine: Engine, session: Session) -> None:
    """The session is usable after the handled failure -- the service rolled back, it did not
    leave a poisoned transaction behind."""
    client, world = poisoned_client(engine)
    client.delete(booking_url(world))

    response = client.get(booking_url(world))

    assert response.status_code == 200
    assert response.json()["public_id"] == world["booking"]


def test_the_creation_event_is_untouched(engine: Engine, session: Session) -> None:
    """Only the failed transaction rolled back. The booking's earlier history is intact."""
    client, world = poisoned_client(engine)

    client.delete(booking_url(world))

    assert rows(session, "SELECT count(*) FROM audit_events WHERE action = 'booking.created'") == 1


# ======================================================================================
# Leakage
# ======================================================================================


LEAKS = [
    "IntegrityError",
    "23503",
    "23502",
    "23505",
    "postgres",
    "psycopg",
    "sqlalchemy",
    "SQLSTATE",
    "fk_audit_events_actor_user_id_users",
    "audit_events",
    "bookings",
    "users",
    "actor_user_id",
    "hotel_id",
    "constraint",
    "relation",
    "Traceback",
    "INSERT",
    "DELETE FROM",
]


@pytest.mark.parametrize("leak", LEAKS)
def test_the_500_body_leaks_nothing(engine: Engine, session: Session, leak: str) -> None:
    client, world = poisoned_client(engine)

    text = client.delete(booking_url(world)).text

    assert leak.lower() not in text.lower(), f"{leak!r} reached the client: {text}"


def test_the_500_does_not_expose_the_injected_actor(engine: Engine, session: Session) -> None:
    """The internal id that caused the failure is the one thing a naive message would name."""
    client, world = poisoned_client(engine)

    text = client.delete(booking_url(world)).text

    assert str(PHANTOM_ACTOR_ID) not in text
    assert "ghost@example.test" not in text
    assert "Ghost" not in text


def test_the_500_names_no_identifier_from_the_request(engine: Engine, session: Session) -> None:
    client, world = poisoned_client(engine)

    text = client.delete(booking_url(world)).text

    assert world["booking"] not in text
    assert world["hotel"] not in text
    assert world["guest"] not in text


def test_the_500_body_is_exactly_the_generic_envelope(engine: Engine, session: Session) -> None:
    """Pinned whole, so nothing can be added to it later without this failing."""
    client, world = poisoned_client(engine)

    assert client.delete(booking_url(world)).json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal error occurred. The incident has been logged.",
            "details": [],
        }
    }


# ======================================================================================
# Diagnosable server-side
# ======================================================================================


def test_the_fault_is_logged_with_the_two_facts_that_identify_it(
    engine: Engine, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    client, world = poisoned_client(engine)

    with caplog.at_level("ERROR"):
        client.delete(booking_url(world))

    faults = [r for r in caplog.records if "Internal fault" in r.getMessage()]
    assert faults, "the internal fault was not logged at ERROR"
    message = faults[0].getMessage()
    assert "sqlstate=23503" in message
    assert "relation=audit_events" in message


def test_the_log_record_carries_the_request_id(
    engine: Engine, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Attached by the existing filter, so the client's header and the log line agree."""
    from app.core.logging import RequestIdFilter

    client, world = poisoned_client(engine)

    with caplog.at_level("ERROR"):
        response = client.delete(
            booking_url(world), headers={"X-Request-ID": "fault-log-correlation"}
        )

    faults = [r for r in caplog.records if "Internal fault" in r.getMessage()]
    assert faults
    RequestIdFilter().filter(faults[0])  # caplog bypasses the app's handler, which owns the filter
    assert response.headers["x-request-id"] == "fault-log-correlation"


def test_the_log_line_carries_no_row_data(
    engine: Engine, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """No ``exc_info``: the fault is raised ``from`` the IntegrityError, whose message renders
    the offending row. A traceback here would write the guest's name into the log."""
    client, world = poisoned_client(engine)

    with caplog.at_level("ERROR"):
        client.delete(booking_url(world))

    faults = [r for r in caplog.records if "Internal fault" in r.getMessage()]
    assert faults
    assert faults[0].exc_info is None, "the internal fault log carries a traceback"
    for banned in ("Lovelace", "Ada", "ghost@example.test", "Failing row", "DETAIL"):
        assert banned not in faults[0].getMessage()


# ======================================================================================
# The 409s that must not have moved
# ======================================================================================


def test_a_real_dependency_is_still_409(engine: Engine, session: Session) -> None:
    """The client CAN act on this one, so it stays a conflict."""
    client = authenticated_client(engine, email=OWNER_EMAIL)
    world = build_world(client)
    client.post(
        f"{booking_url(world)}/payments",
        json={"amount": "100.00", "currency": "EUR", "method": "card"},
    )

    response = client.delete(booking_url(world))

    assert response.status_code == 409
    assert "payments, revenue or reviews" in response.json()["error"]["message"]


def test_a_duplicate_reference_is_still_409(engine: Engine, session: Session) -> None:
    client = authenticated_client(engine, email=OWNER_EMAIL)
    world = build_world(client)
    reference = session.scalar(
        sa.text("SELECT reference FROM bookings WHERE public_id = CAST(:p AS uuid)"),
        {"p": world["booking"]},
    )

    response = client.post(
        f"/api/v1/hotels/{world['hotel']}/bookings",
        json={
            "guest_public_id": world["guest"],
            "reference": reference,
            "check_in_date": str(CHECK_IN + dt.timedelta(days=40)),
            "check_out_date": str(CHECK_IN + dt.timedelta(days=42)),
            "status": "pending",
            "total_amount": "240.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [
                        {
                            "stay_date": str(CHECK_IN + dt.timedelta(days=40 + n)),
                        }
                        for n in range(2)
                    ],
                }
            ],
        },
    )

    assert response.status_code == 409
    assert "already exists at this hotel" in response.json()["error"]["message"]


def test_an_overlapping_room_is_still_409(engine: Engine, session: Session) -> None:
    client = authenticated_client(engine, email=OWNER_EMAIL)
    world = build_world(client)

    response = client.post(
        f"/api/v1/hotels/{world['hotel']}/bookings",
        json={
            "guest_public_id": world["guest"],
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
    )

    assert response.status_code == 409
    assert "already booked for overlapping dates" in response.json()["error"]["message"]


def test_a_clean_deletion_is_still_204(engine: Engine, session: Session) -> None:
    client = authenticated_client(engine, email=OWNER_EMAIL)
    world = build_world(client)

    assert client.delete(booking_url(world)).status_code == 204


def test_not_found_is_still_404(engine: Engine, session: Session) -> None:
    client = authenticated_client(engine, email=OWNER_EMAIL)
    world = build_world(client)

    response = client.delete(f"/api/v1/hotels/{world['hotel']}/bookings/{uuid.uuid4()}")

    assert response.status_code == 404
