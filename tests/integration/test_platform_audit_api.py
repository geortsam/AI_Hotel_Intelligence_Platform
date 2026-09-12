"""The platform-scoped audit read surface against real PostgreSQL.

Stage 4.5.13. Stage 4.5.12 recorded eighteen kinds of change and could report eight. The other
ten belong to no property, so ``audit_events.hotel_id`` is NULL for them and the hotel-scoped
endpoint has no hotel to ask under. This suite is about the endpoint that reads them, and it
makes two claims that only a live database can settle.

**The scope is a database predicate.** ``hotel_id IS NULL`` is in the WHERE clause, so a
hotel-scoped row is never selected -- not selected and then filtered out. The difference is
invisible in a passing functional test and decisive in a failing one, so the query itself is
inspected here as well as its results.

**Platform authority is not hotel authority.** An owner at the property is refused; an
administrator who belongs to no property at all is admitted. That asymmetry is the whole point
of the endpoint and it is exercised for every role.
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

from tests.integration.conftest import (
    TEST_PASSWORD,
    authenticated_client,
    create_test_app,
    grant_membership,
    grant_platform_admin,
    requires_postgres,
)

pytestmark = requires_postgres

URL = "/api/v1/platform/audit-events"

OWNER_EMAIL = "platform-audit-owner@example.test"
ADMIN_EMAIL = "platform-audit-admin@example.test"
NEW_PASSWORD = "platform-audit-replacement-password"

CHECK_IN = dt.date(2027, 8, 3)
CHECK_OUT = dt.date(2027, 8, 6)


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


@pytest.fixture(autouse=True)
def _clean(engine: Engine) -> Iterator[None]:
    """Empty the tables this module writes to, after EVERY test in it.

    Autouse, and deliberately not hung off one of the client fixtures. The three global
    catalogues have natural keys -- an amenity is addressed by ``code`` -- so a test that
    creates ``WIFI`` and does not clean up makes the next one fail with a 409 that has nothing
    to do with what it was testing. Tying the teardown to a fixture meant every test that
    happened not to request that fixture inherited the previous test's catalogue, which is
    exactly the failure this replaced: three tests passed or failed depending on collection
    order.

    ``audit_events`` is truncated too, so a count assertion measures what this test wrote and
    not what the suite has accumulated. TRUNCATE is not a DELETE, so migration 0007's
    append-only trigger does not fire -- the same distinction the Stage 4.5.12 suite relies on.
    """
    yield

    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE amenities RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE revenue_categories RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE expense_categories RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE platform_admins RESTART IDENTITY CASCADE"))
        cleanup.execute(sa.text("TRUNCATE users RESTART IDENTITY CASCADE"))
        cleanup.commit()


@pytest.fixture
def owner(engine: Engine) -> TestClient:
    """A hotel owner. Owns a property; holds no platform grant."""
    return authenticated_client(engine, email=OWNER_EMAIL)


@pytest.fixture
def admin(engine: Engine) -> TestClient:
    """A platform administrator who is a member of NO hotel.

    Deliberately not given a membership anywhere: the endpoint must admit them on the strength
    of the grant alone, and a fixture that quietly made them an owner would hide exactly the
    bug this stage exists to avoid.
    """
    client = authenticated_client(engine, email=ADMIN_EMAIL)
    grant_platform_admin(engine, ADMIN_EMAIL)
    return client


def build_hotel(owner: TestClient, slug: str = "platform-audit-hotel") -> str:
    hotel = str(owner.post("/api/v1/hotels", json=hotel_payload(slug)).json()["public_id"])
    owner.post(
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
    owner.post(f"/api/v1/hotels/{hotel}/room-types/DLX/rooms", json={"room_number": "101"})
    return hotel


def make_hotel_events(owner: TestClient, hotel: str) -> str:
    """Produce four HOTEL-scoped events. Returns the booking's public id."""
    guest = owner.post(
        f"/api/v1/hotels/{hotel}/guests", json={"first_name": "Ada", "last_name": "Lovelace"}
    ).json()["public_id"]
    booking = owner.post(
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
    charge = owner.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={"amount": "100.00", "currency": "EUR", "method": "card"},
    ).json()
    owner.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments/refunds",
        json={
            "amount": "10.00",
            "currency": "EUR",
            "method": "card",
            "refunds_public_id": charge["public_id"],
        },
    )
    owner.patch(f"/api/v1/hotels/{hotel}/bookings/{booking}", json={"status": "checked_in"})
    return str(booking)


def make_platform_events(admin: TestClient) -> None:
    """Produce PLATFORM-scoped events across all four kinds that can carry no hotel."""
    assert (
        admin.post(
            "/api/v1/amenities", json={"code": "WIFI", "name": "Wi-Fi", "category": "tech"}
        ).status_code
        == 201
    )
    assert admin.patch("/api/v1/amenities/WIFI", json={"name": "Wireless"}).status_code == 200
    assert (
        admin.post(
            "/api/v1/revenue-categories", json={"code": "ROOMS", "name": "Rooms"}
        ).status_code
        == 201
    )
    assert (
        admin.post(
            "/api/v1/expense-categories", json={"code": "LAUNDRY", "name": "Laundry"}
        ).status_code
        == 201
    )
    changed = admin.post(
        "/api/v1/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    # Stage 4.5.2: a password change revokes every token minted before it, INCLUDING the one
    # this request was made with. The replacement is returned for exactly that reason, and a
    # suite that ignored it would spend the rest of the test collecting 401s that look like an
    # authorization bug in the endpoint under test.
    admin.headers["Authorization"] = f"Bearer {changed.json()['access_token']}"


@pytest.fixture
def world(owner: TestClient, admin: TestClient) -> dict[str, str]:
    """Both kinds of event, in one database, so the scope split is a real separation."""
    hotel = build_hotel(owner)
    booking = make_hotel_events(owner, hotel)
    make_platform_events(admin)
    return {"hotel": hotel, "booking": booking}


def events(client: TestClient, **params: object) -> list[dict[str, Any]]:
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def member(engine: Engine, hotel: str, role: str) -> TestClient:
    email = f"platform-audit-{role}@example.test"
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, hotel, role)
    return client


# ======================================================================================
# Authorization -- the matrix this stage exists for
# ======================================================================================


def test_anonymous_is_refused_with_401(engine: Engine, world: dict) -> None:
    anonymous = TestClient(create_test_app(engine))

    assert anonymous.get(URL).status_code == 401


@pytest.mark.parametrize("role", ["viewer", "staff", "manager", "owner"])
def test_every_hotel_role_is_refused_with_403(engine: Engine, world: dict, role: str) -> None:
    """A role is a per-hotel grant. It cannot confer authority over rows that belong to no
    hotel, and OWNER is refused as flatly as VIEWER -- seniority within a property is not a
    step towards platform authority."""
    client = member(engine, world["hotel"], role)

    assert client.get(URL).status_code == 403


def test_the_hotels_own_owner_is_refused(owner: TestClient, world: dict) -> None:
    """The account that CREATED the property, and owns it, still cannot read this."""
    assert owner.get(URL).status_code == 403


def test_an_ordinary_authenticated_user_is_refused(engine: Engine, world: dict) -> None:
    """No membership anywhere, no grant: authenticated is not authorized."""
    stranger = authenticated_client(engine, email="platform-audit-nobody@example.test")

    assert stranger.get(URL).status_code == 403


def test_a_platform_administrator_is_admitted(admin: TestClient, world: dict) -> None:
    response = admin.get(URL)

    assert response.status_code == 200
    assert response.json()["total"] >= 1


def test_a_platform_administrator_who_is_no_hotels_member_is_admitted(
    admin: TestClient, world: dict, session: Session
) -> None:
    """The load-bearing case: platform authorization must not depend on hotel membership.

    Asserted against the database rather than trusted from the fixture -- the account has no
    ``user_hotels`` row at all, and is admitted anyway.
    """
    session.rollback()
    memberships = session.scalar(
        sa.text(
            "SELECT count(*) FROM user_hotels uh JOIN users u ON u.id = uh.user_id "
            "WHERE u.email = :e"
        ),
        {"e": ADMIN_EMAIL},
    )

    assert memberships == 0
    assert admin.get(URL).status_code == 200


def test_revoking_the_grant_revokes_access(admin: TestClient, engine: Engine, world: dict) -> None:
    """The grant is read from the database on every request, not carried in the token, so a
    revocation takes effect now rather than when a token happens to expire."""
    assert admin.get(URL).status_code == 200

    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text(
                "DELETE FROM platform_admins pa USING users u "
                "WHERE pa.user_id = u.id AND u.email = :e"
            ),
            {"e": ADMIN_EMAIL},
        )
        session.commit()

    assert admin.get(URL).status_code == 403


def test_granting_hotel_ownership_does_not_grant_platform_access(
    engine: Engine, world: dict
) -> None:
    """The converse of the rule, checked directly: promoting somebody to owner of every
    property in the database changes nothing here."""
    email = "platform-audit-super-owner@example.test"
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, world["hotel"], "owner")

    assert client.get(URL).status_code == 403


# ======================================================================================
# Scope -- platform events in, hotel events out
# ======================================================================================


PLATFORM_ACTIONS = {
    "auth.password_changed",
    "amenity.created",
    "amenity.updated",
    "revenue_category.created",
    "expense_category.created",
}

HOTEL_ACTIONS = {
    "booking.created",
    "booking.status_changed",
    "payment.created",
    "payment.refund_created",
}


def test_platform_scoped_events_are_returned(admin: TestClient, world: dict) -> None:
    actions = {event["action"] for event in events(admin, page_size=100)}

    assert actions >= PLATFORM_ACTIONS, sorted(PLATFORM_ACTIONS - actions)


def test_hotel_scoped_events_are_excluded(admin: TestClient, world: dict) -> None:
    """The assertion this stage turns on. Both kinds exist in the database; only one kind is
    returned."""
    actions = {event["action"] for event in events(admin, page_size=100)}

    assert actions & HOTEL_ACTIONS == set(), sorted(actions & HOTEL_ACTIONS)


def test_both_kinds_really_exist_in_the_database(world: dict, session: Session) -> None:
    """Without this the exclusion above could pass because nothing hotel-scoped was ever
    written, which would make the whole scope section vacuous."""
    session.rollback()
    scoped = session.scalar(sa.text("SELECT count(*) FROM audit_events WHERE hotel_id IS NOT NULL"))
    unscoped = session.scalar(sa.text("SELECT count(*) FROM audit_events WHERE hotel_id IS NULL"))

    assert scoped >= 4, scoped
    assert unscoped >= 5, unscoped


def test_every_returned_event_has_a_null_hotel_in_the_database(
    admin: TestClient, world: dict, session: Session
) -> None:
    """Checked row by row against the table, not inferred from the action name."""
    returned = [event["public_id"] for event in events(admin, page_size=100)]
    assert returned

    session.rollback()
    rows = session.execute(
        sa.text(
            "SELECT public_id, hotel_id FROM audit_events "
            "WHERE public_id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": returned},
    ).all()

    assert len(rows) == len(returned)
    assert all(hotel_id is None for _, hotel_id in rows)


def test_the_total_counts_only_platform_scoped_events(
    admin: TestClient, world: dict, session: Session
) -> None:
    """A total that counted the whole table would disclose how much activity every tenant has,
    without returning a single one of their rows."""
    session.rollback()
    expected = session.scalar(sa.text("SELECT count(*) FROM audit_events WHERE hotel_id IS NULL"))

    assert admin.get(URL).json()["total"] == expected


def test_the_scope_is_in_the_sql_not_in_python(
    admin: TestClient, engine: Engine, world: dict
) -> None:
    """`hotel_id IS NULL` must reach the database.

    A platform read that fetched every audit row and discarded the tenant ones would pass every
    functional test above while doing unbounded work -- and would be one refactor away from
    returning them. The statements are captured and inspected.
    """
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        if "audit_events" in statement:
            statements.append(" ".join(statement.split()))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        assert admin.get(URL).status_code == 200
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert statements, "no audit query was issued -- this test would be vacuous"
    for statement in statements:
        assert "hotel_id IS NULL" in statement, statement


def test_a_hotel_only_action_filter_returns_an_empty_page(admin: TestClient, world: dict) -> None:
    """Accepted rather than refused, so the two surfaces agree about what a valid filter is --
    and it returns nothing, because the scope decides, not the filter."""
    response = admin.get(URL, params={"action": "booking.created"})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


def test_an_administrator_with_no_platform_events_gets_an_empty_page(
    admin: TestClient, owner: TestClient
) -> None:
    """A valid caller and a genuinely empty result is a 200 with an envelope, never an error.

    No ``world`` fixture here: hotel activity exists, platform activity does not.
    """
    hotel = build_hotel(owner)
    make_hotel_events(owner, hotel)

    response = admin.get(URL)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 20, "pages": 0}


# ======================================================================================
# Privacy and leakage
# ======================================================================================


BANNED = [
    "password",
    "argon2",
    "$2b$",
    "bearer ",
    "eyj",
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


def test_the_response_carries_no_credential_or_sql(admin: TestClient, world: dict) -> None:
    """The ``action`` column legitimately reads ``auth.password_changed``; everything else is
    swept. Same split as the hotel surface, for the same reason."""
    items = events(admin, page_size=100)
    assert items

    rendered = str([{k: v for k, v in item.items() if k != "action"} for item in items]).lower()

    for banned in BANNED:
        assert banned not in rendered, f"{banned!r} reached the platform audit response"


def test_no_literal_secret_reaches_the_response(admin: TestClient, world: dict) -> None:
    rendered = str(events(admin, page_size=100))

    for secret in (TEST_PASSWORD, NEW_PASSWORD):
        assert secret not in rendered


def test_a_password_change_event_carries_no_details(admin: TestClient, world: dict) -> None:
    """Visible to a platform administrator, and still says nothing about the credential."""
    changes = [e for e in events(admin, page_size=100) if e["action"] == "auth.password_changed"]

    assert len(changes) == 1
    assert changes[0]["details"] == {}


def test_the_response_exposes_no_internal_identifier(admin: TestClient, world: dict) -> None:
    for item in events(admin, page_size=100):
        assert set(item) == {
            "public_id",
            "occurred_at",
            "action",
            "resource_type",
            "resource_reference",
            "actor_public_id",
            "actor_email",
            "request_id",
            "details",
        }
        for value in item.values():
            assert not isinstance(value, int), f"{item} carries an integer identifier"


def test_the_response_carries_no_hotel_field_at_all(admin: TestClient, world: dict) -> None:
    """Not a null hotel -- no field. Every row here has none, and the type says so."""
    for item in events(admin, page_size=100):
        assert not any("hotel" in key for key in item)


def test_the_actor_is_a_public_identifier(admin: TestClient, world: dict) -> None:
    change = next(e for e in events(admin, page_size=100) if e["action"] == "auth.password_changed")

    assert uuid.UUID(change["actor_public_id"])
    assert change["actor_email"] == ADMIN_EMAIL


def test_a_refusal_names_no_table_and_no_internals(engine: Engine, world: dict) -> None:
    stranger = authenticated_client(engine, email="platform-audit-error-shape@example.test")

    body = stranger.get(URL).text

    assert set(stranger.get(URL).json()) == {"error"}
    for leak in ("audit_events", "hotel_id", "SELECT", "psycopg", "sqlalchemy", "platform_admins"):
        assert leak not in body


def test_a_refusal_does_not_reveal_whether_any_event_exists(
    engine: Engine, owner: TestClient, world: dict
) -> None:
    """The 403 for a caller without the grant is identical whether the table is full or empty,
    so it cannot be used to probe platform activity."""
    with_events = owner.get(URL)

    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        session.commit()

    assert owner.get(URL).json() == with_events.json()


# ======================================================================================
# Pagination
# ======================================================================================


def bulk_platform_events(admin: TestClient, count: int) -> None:
    """*count* amenity creations, all in rapid succession, so timestamps collide."""
    for index in range(count):
        response = admin.post(
            "/api/v1/amenities", json={"code": f"AMEN{index:03d}", "name": f"Amenity {index}"}
        )
        assert response.status_code == 201, response.text


def test_events_are_returned_newest_first(admin: TestClient, world: dict) -> None:
    times = [event["occurred_at"] for event in events(admin, page_size=100)]

    assert times == sorted(times, reverse=True)


def test_pagination_partitions_the_history_without_repeating_or_dropping_a_row(
    admin: TestClient, session: Session
) -> None:
    """The ``(occurred_at DESC, id DESC)`` tie-break is what makes this hold: these events are
    written within the same microsecond, so ordering by the timestamp alone is not a total
    order and a row would fall between two pages."""
    bulk_platform_events(admin, 10)

    collected: list[str] = []
    for page in range(1, 6):
        collected += [event["public_id"] for event in events(admin, page=page, page_size=2)]

    session.rollback()
    total = session.scalar(sa.text("SELECT count(*) FROM audit_events WHERE hotel_id IS NULL"))

    assert len(collected) == 10
    assert len(set(collected)) == 10
    assert admin.get(URL).json()["total"] == total


def test_equal_timestamps_do_not_break_the_ordering(admin: TestClient, session: Session) -> None:
    """The tie-break on ``id``, against a genuine collision.

    The rows are INSERTed directly with one identical ``occurred_at``, and that is the only way
    to build this case honestly: ``occurred_at`` defaults to ``now()``, which in PostgreSQL is
    transaction start time, and every request is its own transaction -- so six API calls
    produce six distinct timestamps and would prove nothing. An earlier draft of this test did
    exactly that, and asserted a collision it had not actually created.

    A plain INSERT is available here for the same reason TRUNCATE is: migration 0007's trigger
    forbids UPDATE and DELETE on this table, not writes to it.
    """
    session.rollback()
    session.execute(
        sa.text(
            "INSERT INTO audit_events (action, resource_type, resource_reference, occurred_at) "
            "SELECT 'amenity.created', 'amenity', 'TIE' || g, "
            "TIMESTAMPTZ '2027-01-01 12:00:00+00' FROM generate_series(1, 6) g"
        )
    )
    session.commit()

    distinct = session.scalar(
        sa.text("SELECT count(DISTINCT occurred_at) FROM audit_events WHERE hotel_id IS NULL")
    )
    total = session.scalar(sa.text("SELECT count(*) FROM audit_events WHERE hotel_id IS NULL"))
    assert total == 6
    assert distinct == 1, "the timestamps did not collide -- this test would prove nothing"

    pages = [[e["public_id"] for e in events(admin, page=n, page_size=2)] for n in (1, 2, 3)]
    collected = [public_id for page in pages for public_id in page]

    assert len(collected) == 6, pages
    assert len(set(collected)) == 6, pages
    references = [e["resource_reference"] for e in events(admin, page_size=100)]
    assert references == [f"TIE{n}" for n in range(6, 0, -1)], references


def test_a_page_beyond_the_end_is_empty_not_an_error(admin: TestClient, world: dict) -> None:
    response = admin.get(URL, params={"page": 999, "page_size": 100})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] >= 1


def test_the_envelope_reports_the_page_count(admin: TestClient) -> None:
    bulk_platform_events(admin, 5)

    body = admin.get(URL, params={"page_size": 2}).json()

    assert body["total"] == 5
    assert body["pages"] == 3
    assert body["page_size"] == 2
    assert len(body["items"]) == 2


# ======================================================================================
# Filters
# ======================================================================================


def test_the_action_filter_narrows_the_page(admin: TestClient, world: dict) -> None:
    found = events(admin, action="amenity.created")

    assert found
    assert {event["action"] for event in found} == {"amenity.created"}


def test_the_resource_type_filter_narrows_the_page(admin: TestClient, world: dict) -> None:
    found = events(admin, resource_type="amenity")

    assert found
    assert {event["resource_type"] for event in found} == {"amenity"}


def test_the_actor_filter_selects_that_persons_events(
    admin: TestClient, engine: Engine, world: dict
) -> None:
    admin_id = admin.get("/api/v1/auth/me").json()["public_id"]

    mine = events(admin, actor_public_id=admin_id, page_size=100)

    assert mine
    assert {event["actor_public_id"] for event in mine} == {admin_id}


def test_an_unknown_actor_filter_is_an_empty_page_not_an_error(
    admin: TestClient, world: dict
) -> None:
    """A different answer for a real account than for an invented one would be an
    account-enumeration oracle wearing a filter's clothes."""
    response = admin.get(URL, params={"actor_public_id": str(uuid.uuid4())})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 20, "pages": 0}


def test_the_request_id_filter_correlates_with_the_response_header(
    admin: TestClient, owner: TestClient
) -> None:
    response = admin.post(
        "/api/v1/amenities",
        json={"code": "POOL", "name": "Pool"},
        headers={"X-Request-ID": "platform-audit-correlation-1"},
    )
    assert response.status_code == 201, response.text
    assert response.headers["x-request-id"] == "platform-audit-correlation-1"

    found = events(admin, request_id="platform-audit-correlation-1")

    assert [event["resource_reference"] for event in found] == ["POOL"]


def test_the_date_window_selects_by_occurrence(admin: TestClient, world: dict) -> None:
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    past = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)

    assert events(admin, occurred_from=future.isoformat()) == []
    assert events(admin, occurred_from=past.isoformat())
    assert events(admin, occurred_to=past.isoformat()) == []


def test_filters_combine(admin: TestClient, world: dict) -> None:
    admin_id = admin.get("/api/v1/auth/me").json()["public_id"]

    found = events(admin, action="amenity.updated", actor_public_id=admin_id)

    assert [event["resource_reference"] for event in found] == ["WIFI"]


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page": -1},
        {"page_size": 0},
        {"page_size": 101},
        {"action": "amenity.exploded"},
        {"action": "booking"},
        {"resource_type": "spaceship"},
        {"actor_public_id": "not-a-uuid"},
        {"request_id": "not a valid id"},
        {"request_id": "x" * 65},
        {"request_id": ""},
        {"occurred_from": "yesterday"},
        {"occurred_to": "soon"},
    ],
)
def test_invalid_filters_are_rejected(admin: TestClient, world: dict, params: dict) -> None:
    response = admin.get(URL, params=params)

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}


def test_a_validation_failure_echoes_no_input_value(admin: TestClient, world: dict) -> None:
    body = admin.get(URL, params={"action": "sekrit-value-9876"}).json()

    assert "sekrit-value-9876" not in str(body)


def test_an_invalid_filter_is_refused_after_authorization_not_before(
    owner: TestClient, world: dict
) -> None:
    """A caller without the grant must not be able to tell a bad filter from a good one --
    otherwise 422-versus-403 becomes a way to probe the endpoint's shape."""
    assert owner.get(URL, params={"page_size": 9999}).status_code == 403
    assert owner.get(URL, params={"action": "nonsense"}).status_code == 403


# ======================================================================================
# Cost
# ======================================================================================


def test_the_listing_issues_a_bounded_number_of_statements(
    admin: TestClient, engine: Engine
) -> None:
    """One COUNT and one page SELECT, whatever the page holds.

    The budget is the measured implementation, not a guess: the actor is joined in the page
    query rather than resolved per row, so twenty events cost the same two statements as one.
    """
    bulk_platform_events(admin, 20)

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        if "audit_events" in statement:
            statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        found = events(admin, page_size=100)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert len(found) == 20
    assert len(statements) == 2, statements


def test_the_page_query_is_bounded_by_limit_and_offset(admin: TestClient, engine: Engine) -> None:
    """Pagination is the database's. Nothing outside the requested page is fetched."""
    bulk_platform_events(admin, 12)

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        if "audit_events" in statement and "count" not in statement.lower():
            statements.append(" ".join(statement.split()))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        assert len(events(admin, page=2, page_size=3)) == 3
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert len(statements) == 1
    assert "LIMIT" in statements[0].upper()
    assert "OFFSET" in statements[0].upper()


def test_the_platform_scope_is_answered_by_the_existing_index(
    admin: TestClient, session: Session
) -> None:
    """What the existing index does and does not do for this endpoint, measured.

    ``ix_audit_events_hotel_id_occurred_at (hotel_id, occurred_at DESC, id DESC)`` answers the
    SCOPE: ``hotel_id IS NULL`` is an ordinary lookup on its leading column, because a btree
    stores NULLs. It does NOT supply the ORDERING, and the first draft of this stage claimed it
    did until this test disagreed.

    The reason is worth recording, because it is not obvious and it is the whole argument for
    the finding in the report: ``IS NULL`` is a ``NullTest`` rather than an equality, so the
    planner never concludes ``hotel_id`` is constant within the scan, the leading column stays
    in the path's sort order, and ``occurred_at DESC, id DESC`` is therefore not a usable
    prefix. The hotel listing next door, whose predicate IS an equality, gets an ordered Index
    Only Scan with no sort from the same index -- asserted below so the contrast is on record.

    ``enable_seqscan`` is off so the plan describes the query rather than the size of the
    fixture. The endpoint is correct either way; this is about cost, and a partial index would
    remove the sort at the price of a migration this stage was told not to create.
    """
    bulk_platform_events(admin, 30)
    session.rollback()
    session.execute(sa.text("ANALYZE audit_events"))
    session.execute(sa.text("SET LOCAL enable_seqscan = off"))

    def explain(predicate: str) -> str:
        return "\n".join(
            str(row[0])
            for row in session.execute(
                sa.text(
                    f"EXPLAIN SELECT id FROM audit_events WHERE {predicate} "
                    "ORDER BY occurred_at DESC, id DESC LIMIT 20"
                )
            )
        )

    platform_plan = explain("hotel_id IS NULL")
    hotel_plan = explain("hotel_id = 1")
    session.rollback()

    # The scope IS answered by the index -- the rows are found through it, not by a filter.
    assert "ix_audit_events_hotel_id_occurred_at" in platform_plan, platform_plan
    assert "Index Cond: (hotel_id IS NULL)" in platform_plan, platform_plan

    # The ordering is NOT. Pinned, so the day a partial index is added this test fails and
    # says so, rather than quietly continuing to describe the old behaviour.
    assert "Sort" in platform_plan, (
        "the platform scope no longer sorts -- if an index was added for it, update this test "
        f"and the finding it records:\n{platform_plan}"
    )

    # The same index, an equality predicate, and no sort at all.
    assert "ix_audit_events_hotel_id_occurred_at" in hotel_plan, hotel_plan
    assert "Sort" not in hotel_plan, hotel_plan
