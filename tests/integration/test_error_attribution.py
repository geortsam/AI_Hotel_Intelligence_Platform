"""What a service says when the database refuses it. Real PostgreSQL.

Stage 4.5.16. The claim being tested is narrow and behavioural: **a service describes only the
failure it can actually account for.**

The defect closed here was found in Stage 4.5.15. Since Stage 4.5.12 six services write an
audit event inside the transaction of the mutation they describe -- deliberately, so the two
commit or roll back together. The consequence is that an integrity failure in the AUDIT layer
now lands in a DOMAIN service's ``except IntegrityError``, where SQLSTATE alone cannot tell it
apart from a real domain conflict. A booking deletion whose audit INSERT failed on its actor
foreign key was reported to the client as "payments still reference this booking" -- false, and
unactionable, because no amount of removing payments would have helped.

Both halves need a live database: the real diagnostics only exist when PostgreSQL produces
them, and the whole point is which of four genuinely different failures is being distinguished.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import (
    GENERIC_SERVER_MESSAGE,
    ConflictError,
    InternalFaultError,
    constraint_name_of,
    is_audit_integrity_failure,
    relation_of,
)
from tests.integration.conftest import authenticated_client, requires_postgres

pytestmark = requires_postgres

OWNER_EMAIL = "attribution-owner@example.test"

CHECK_IN = dt.date(2028, 5, 4)
CHECK_OUT = dt.date(2028, 5, 7)

DEPENDENCY_MESSAGE = "This booking cannot be deleted because payments, revenue or reviews"


# ======================================================================================
# Scaffolding
# ======================================================================================


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    client = authenticated_client(engine, email=OWNER_EMAIL)
    yield client

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE audit_events_archive"))
        cleanup.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE revenue_categories RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


def build(api: TestClient) -> dict[str, str]:
    hotel = str(
        api.post(
            "/api/v1/hotels",
            json={
                "slug": "attribution-hotel",
                "name": "Attribution Hotel",
                "address_line1": "1 Dionysiou Areopagitou",
                "city": "Athens",
                "country_code": "GR",
                "timezone": "Europe/Athens",
                "currency": "EUR",
            },
        ).json()["public_id"]
    )
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
    return {"hotel": hotel, "guest": str(guest), "booking": str(booking)}


@pytest.fixture
def world(api: TestClient) -> dict[str, str]:
    return build(api)


def booking_url(world: dict[str, str]) -> str:
    return f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}"


def booking_service_for(session: Session, *, actor_id: int | None = None) -> Any:
    """A `BookingService` wired exactly as the dependency graph wires it.

    *actor_id*, when given, binds an audit actor whose ``users`` row does not exist -- which is
    how a genuine audit-layer foreign-key failure is produced without mocking anything.
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

    actor = caller
    if actor_id is not None:
        actor = User(email="ghost@example.test", password_hash="x", full_name="Ghost")
        actor.id = actor_id

    policy = HotelAccessPolicy(caller, MembershipRepository(session))
    scope = HotelScopeResolver(HotelRepository(session), RoomTypeRepository(session), policy)
    return BookingService(
        session,
        BookingRepository(session),
        GuestRepository(session),
        scope,
        AuditTrail(AuditRepository(session), actor),
        # Stage 4.5.23: the booking service prices its own nights, so a hand-built
        # one needs the same collaborator the dependency wiring gives it.
        PricingService(PricingRepository(session)),
        # Stage 4.5.24: repricing reads the ledger to say what is refundable, so a
        # hand-built service needs the same collaborator the wiring gives it.
        PaymentRepository(session),
    )


# ======================================================================================
# The four failures, and the diagnostics that tell them apart
# ======================================================================================


def test_a_payments_restrict_is_attributed_to_the_dependents(api: TestClient, world: dict) -> None:
    api.post(
        f"{booking_url(world)}/payments",
        json={"amount": "100.00", "currency": "EUR", "method": "card"},
    )

    response = api.delete(booking_url(world))

    assert response.status_code == 409
    assert DEPENDENCY_MESSAGE in response.json()["error"]["message"]


def test_a_reviews_set_null_is_attributed_to_the_dependents(api: TestClient, world: dict) -> None:
    """``reviews`` blocks the delete through a composite SET NULL over a NOT NULL ``hotel_id``,
    which PostgreSQL reports as 23502 with NO constraint name. The relation is what recognises
    it -- an attribution built on the constraint name alone could not."""
    response = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings/{world['booking']}/review",
        json={"source": "direct", "rating": 8, "rating_scale": 10, "review_date": str(CHECK_OUT)},
    )
    assert response.status_code == 201, response.text

    refused = api.delete(booking_url(world))

    assert refused.status_code == 409
    assert DEPENDENCY_MESSAGE in refused.json()["error"]["message"]


def test_an_audit_failure_is_not_attributed_to_the_dependents(
    api: TestClient, world: dict, session: Session
) -> None:
    """The defect this stage closes.

    The booking has no payments, no revenue and no reviews -- nothing references it at all. The
    audit INSERT fails on its actor foreign key, and before Stage 4.5.16 the client was told
    that payments still referenced the booking.
    """
    service = booking_service_for(session, actor_id=9_999_999)

    # Stage 4.5.17 reclassified this from a client conflict to a server fault. The
    # attribution rule Stage 4.5.16 established is unchanged -- the failure is still
    # recognised as the audit layer's -- only what the server concludes from that changed.
    with pytest.raises(InternalFaultError) as caught:
        service.delete(uuid.UUID(world["hotel"]), uuid.UUID(world["booking"]))

    message = str(caught.value)
    assert "payments, revenue or reviews" not in message, "the audit failure was misattributed"
    assert "still referenced by other records" not in message
    assert message == GENERIC_SERVER_MESSAGE
    assert not isinstance(caught.value, ConflictError)


def test_the_booking_survives_an_audit_failure(
    api: TestClient, world: dict, session: Session
) -> None:
    """Stage 4.5.15's guarantee, unchanged: the deletion cannot commit without its event."""
    service = booking_service_for(session, actor_id=9_999_999)

    with pytest.raises(InternalFaultError):
        service.delete(uuid.UUID(world["hotel"]), uuid.UUID(world["booking"]))

    session.rollback()
    assert session.scalar(
        sa.text("SELECT count(*) FROM bookings WHERE public_id = CAST(:p AS uuid)"),
        {"p": world["booking"]},
    )


def test_the_diagnostics_really_do_differ_between_the_two_cases(
    api: TestClient, world: dict, session: Session
) -> None:
    """Not vacuous: the two failures this stage separates are shown to be genuinely different
    at the driver level, and identical by SQLSTATE class alone."""
    from app.models.booking import Booking

    session.rollback()
    booking = session.scalars(
        sa.select(Booking).where(Booking.public_id == uuid.UUID(world["booking"]))
    ).one()

    # 1. a real dependent
    session.execute(
        sa.text(
            "INSERT INTO payments (booking_id, hotel_id, kind, amount, currency, method, status)"
            " VALUES (:b, :h, 'charge', 10, 'EUR', 'card', 'pending')"
        ),
        {"b": booking.id, "h": booking.hotel_id},
    )
    session.flush()
    with pytest.raises(sa.exc.IntegrityError) as dependent:
        session.execute(sa.delete(Booking).where(Booking.id == booking.id))
        session.flush()
    session.rollback()

    # 2. the audit layer
    booking = session.scalars(
        sa.select(Booking).where(Booking.public_id == uuid.UUID(world["booking"]))
    ).one()
    with pytest.raises(sa.exc.IntegrityError) as audit:
        session.execute(sa.delete(Booking).where(Booking.id == booking.id))
        session.flush()
        session.execute(
            sa.text(
                "INSERT INTO audit_events (hotel_id, actor_user_id, action, resource_type,"
                " resource_reference) VALUES (:h, 9999999, 'booking.deleted', 'booking', 'x')"
            ),
            {"h": booking.hotel_id},
        )
        session.flush()
    session.rollback()

    assert relation_of(dependent.value) == "payments"
    assert relation_of(audit.value) == "audit_events"
    assert is_audit_integrity_failure(dependent.value) is False
    assert is_audit_integrity_failure(audit.value) is True
    # Both are foreign-key class failures. SQLSTATE alone could never have separated them.
    assert str(dependent.value.orig.sqlstate)[:2] == "23"  # type: ignore[union-attr]
    assert str(audit.value.orig.sqlstate)[:2] == "23"  # type: ignore[union-attr]


def test_a_not_null_violation_carries_no_constraint_name(
    api: TestClient, world: dict, session: Session
) -> None:
    """Measured against the live database, because it is the fact the design rests on."""
    from app.models.booking import Booking

    session.rollback()
    booking = session.scalars(
        sa.select(Booking).where(Booking.public_id == uuid.UUID(world["booking"]))
    ).one()
    session.execute(
        sa.text(
            "INSERT INTO reviews (hotel_id, booking_id, source, rating, rating_scale,"
            " review_date) VALUES (:h, :b, 'direct', 8, 10, :d)"
        ),
        {"h": booking.hotel_id, "b": booking.id, "d": CHECK_OUT},
    )
    session.flush()

    with pytest.raises(sa.exc.IntegrityError) as caught:
        session.execute(sa.delete(Booking).where(Booking.id == booking.id))
        session.flush()
    session.rollback()

    assert constraint_name_of(caught.value) is None
    assert relation_of(caught.value) == "reviews"


# ======================================================================================
# Nothing regressed, and nothing leaked
# ======================================================================================


def test_a_clean_deletion_is_unaffected(api: TestClient, world: dict) -> None:
    assert api.delete(booking_url(world)).status_code == 204


def test_a_duplicate_reference_is_still_reported_precisely(
    api: TestClient, world: dict, session: Session
) -> None:
    """The constraint-name branches that already worked keep working."""
    session.rollback()
    reference = session.scalar(
        sa.text("SELECT reference FROM bookings WHERE public_id = CAST(:p AS uuid)"),
        {"p": world["booking"]},
    )
    guest = world["guest"]

    response = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings",
        json={
            "guest_public_id": guest,
            "reference": reference,
            "check_in_date": str(CHECK_IN + dt.timedelta(days=30)),
            "check_out_date": str(CHECK_IN + dt.timedelta(days=32)),
            "status": "pending",
            "total_amount": "240.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [
                        {
                            "stay_date": str(CHECK_IN + dt.timedelta(days=30 + n)),
                        }
                        for n in range(2)
                    ],
                }
            ],
        },
    )

    assert response.status_code == 409
    assert "already exists at this hotel" in response.json()["error"]["message"]


def test_an_overlapping_room_is_still_reported_precisely(api: TestClient, world: dict) -> None:
    guest = world["guest"]

    response = api.post(
        f"/api/v1/hotels/{world['hotel']}/bookings",
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
    )

    assert response.status_code == 409
    assert "already booked for overlapping dates" in response.json()["error"]["message"]


def test_no_refusal_names_a_table_or_a_constraint(
    api: TestClient, world: dict, session: Session
) -> None:
    """The names are read to DECIDE, never to report."""
    api.post(
        f"{booking_url(world)}/payments",
        json={"amount": "10.00", "currency": "EUR", "method": "cash"},
    )
    bodies = [api.delete(booking_url(world)).text]

    service = booking_service_for(session, actor_id=9_999_999)
    try:
        service.delete(uuid.UUID(world["hotel"]), uuid.UUID(world["booking"]))
    except (ConflictError, InternalFaultError) as exc:
        bodies.append(str(exc))
    session.rollback()

    for body in bodies:
        for leak in (
            "audit_events",
            "bookings",
            "fk_",
            "ck_",
            "uq_",
            "23503",
            "23502",
            "23001",
            "SQLSTATE",
            "psycopg",
            "sqlalchemy",
            "Traceback",
            "table",
        ):
            assert leak not in body, f"{leak!r} reached the client: {body}"
