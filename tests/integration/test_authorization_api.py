"""Authorization and hotel membership against real PostgreSQL.

Stage 4.2. Three questions decide every hotel-scoped request, in this order:

* **Who are you?** No token, a broken token or a disabled account -> 401.
* **Is this hotel yours?** A hotel that does not exist, a hotel you have no membership of,
  and a hotel someone else is a member of are all the SAME 404 -- byte for byte. A caller who
  can tell those apart can enumerate the installation's properties.
* **May you do this to it?** A member whose role is too low -> 403. That distinction is safe
  precisely because it is only ever reached by a member.

The role hierarchy is ``viewer < staff < manager < owner``, and the matrix below is driven
from the authorised decision rather than from the code: each operation names the minimum role
it needs, and every role is tried against every operation. A route that quietly loosened its
requirement fails here even though its own domain suite still passes.

SQLite is not substituted.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Callable, Iterator
from typing import NamedTuple

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from tests.integration.conftest import (
    TEST_PASSWORD,
    authenticated_client,
    grant_membership,
    requires_postgres,
    revoke_membership,
    seed_amenity,
    seed_expense_category,
    seed_revenue_category,
)

pytestmark = requires_postgres

OWNER_EMAIL = "authz-owner@example.test"
OUTSIDER_EMAIL = "authz-outsider@example.test"

CHECK_IN = dt.date(2026, 9, 1)
CHECK_OUT = dt.date(2026, 9, 4)
LEDGER_DATE = dt.date(2026, 9, 2)

#: The hierarchy, as authorised. Ranks are compared, not permissions enumerated.
ROLES = ["viewer", "staff", "manager", "owner"]
RANK = {role: index for index, role in enumerate(ROLES)}


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


# ======================================================================================
# Fixtures
# ======================================================================================


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    """Every account, membership and hotel is torn down between tests.

    ``users`` has no foreign key to ``hotels``, so the CASCADE from hotels does not reach it;
    both are named. The global catalogues are named too -- they belong to no hotel, so nothing
    cascades to them either.
    """
    yield
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(
            sa.text(
                "TRUNCATE amenities, revenue_categories, expense_categories, "
                "user_hotels, users, hotels RESTART IDENTITY CASCADE"
            )
        )
        cleanup.commit()


@pytest.fixture
def owner(engine: Engine) -> TestClient:
    """The account that creates the hotel, and is therefore its owner."""
    return authenticated_client(engine, email=OWNER_EMAIL)


@pytest.fixture
def hotel(owner: TestClient) -> str:
    return str(owner.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"])


@pytest.fixture
def member(engine: Engine, hotel: str) -> Callable[..., TestClient]:
    """Build a fresh account holding *role* at a hotel.

    A new email per call, so no test can be affected by another's grants, and the grant is a
    direct database write because Stage 4.2 exposes no membership endpoint -- inventing one to
    make tests convenient would be building product from a test's needs.
    """

    def build(role: str, *, at: str | None = None) -> TestClient:
        email = f"authz-{role}-{uuid.uuid4().hex[:8]}@example.test"
        client = authenticated_client(engine, email=email)
        grant_membership(engine, email, at or hotel, role)
        return client

    return build


class World(NamedTuple):
    """Identifiers for one fully populated hotel, so a matrix row can address anything."""

    hotel: str
    guest: str
    booking: str
    payment: str
    room_type: str
    room: str
    amenity: str


@pytest.fixture
def world(owner: TestClient, hotel: str, engine: Engine) -> World:
    """One hotel with a room type, a room, a guest, a booking, a payment and a review.

    Built by the owner through the API, so nothing here depends on the authorization being
    tested being wrong.
    """
    seed_amenity(engine, "WIFI", "Wi-Fi")
    seed_revenue_category(engine, "FB", "Food and beverage")
    seed_expense_category(engine, "UTILITIES", "Utilities")

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
                        {"stay_date": str(CHECK_IN + dt.timedelta(days=n)), "rate": "120.00"}
                        for n in range(3)
                    ],
                }
            ],
        },
    ).json()["public_id"]
    payment = owner.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        json={"amount": "100.00", "currency": "EUR", "method": "card"},
    ).json()["public_id"]
    owner.post(
        f"/api/v1/hotels/{hotel}/bookings/{booking}/review",
        json={"rating": "4.50", "review_date": str(LEDGER_DATE)},
    )
    # Assigned up front so the matrix's "unassign amenity" row has something to remove: a 404
    # from an absent assignment would be indistinguishable from a 404 from the membership wall,
    # and the matrix reads the two the same way on purpose.
    owner.post(f"/api/v1/hotels/{hotel}/room-types/DLX/amenities", json={"code": "WIFI"})
    return World(hotel, guest, booking, payment, "DLX", "101", "WIFI")


# ======================================================================================
# `POST /hotels` grants ownership, `GET /hotels` shows only what you belong to
# ======================================================================================


def test_creating_a_hotel_makes_the_creator_its_owner(owner: TestClient, hotel: str) -> None:
    """The grant is in the same transaction as the insert: nobody creates a hotel they cannot
    then administer."""
    assert owner.patch(f"/api/v1/hotels/{hotel}", json={"name": "Renamed"}).status_code == 200


def test_the_listing_shows_only_the_callers_hotels(owner: TestClient, engine: Engine) -> None:
    owner.post("/api/v1/hotels", json=hotel_payload("hotel-a"))
    owner.post("/api/v1/hotels", json=hotel_payload("hotel-b"))
    outsider = authenticated_client(engine, email=OUTSIDER_EMAIL)

    assert owner.get("/api/v1/hotels").json()["total"] == 2
    assert outsider.get("/api/v1/hotels").json() == {
        "items": [],
        "total": 0,
        "pages": 0,
        "page": 1,
        "page_size": 20,
    }


def test_the_total_counts_memberships_not_hotels(owner: TestClient, engine: Engine) -> None:
    """A total taken from the unfiltered table would tell an outsider how many properties the
    installation has, which is the leak the filter exists to close."""
    owner.post("/api/v1/hotels", json=hotel_payload("hotel-a"))
    owner.post("/api/v1/hotels", json=hotel_payload("hotel-b"))
    owner.post("/api/v1/hotels", json=hotel_payload("hotel-c"))
    viewer = authenticated_client(engine, email="authz-one-hotel@example.test")
    target = owner.get("/api/v1/hotels").json()["items"][0]["public_id"]
    grant_membership(engine, "authz-one-hotel@example.test", target, "viewer")

    body = viewer.get("/api/v1/hotels").json()

    assert body["total"] == 1
    assert [item["public_id"] for item in body["items"]] == [target]


def test_the_filtered_listing_paginates_over_memberships(owner: TestClient, engine: Engine) -> None:
    for slug in ("hotel-a", "hotel-b", "hotel-c"):
        owner.post("/api/v1/hotels", json=hotel_payload(slug))

    first = owner.get("/api/v1/hotels", params={"page": 1, "page_size": 2}).json()
    second = owner.get("/api/v1/hotels", params={"page": 2, "page_size": 2}).json()

    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    ids = [i["public_id"] for i in first["items"] + second["items"]]
    assert len(set(ids)) == 3, "a page repeated or skipped a hotel"


def test_a_revoked_membership_removes_the_hotel_from_the_listing(
    owner: TestClient, hotel: str, engine: Engine
) -> None:
    assert owner.get("/api/v1/hotels").json()["total"] == 1

    revoke_membership(engine, OWNER_EMAIL, hotel)

    assert owner.get("/api/v1/hotels").json()["total"] == 0
    assert owner.get(f"/api/v1/hotels/{hotel}").status_code == 404


# ======================================================================================
# One user, several hotels, a different role at each
# ======================================================================================


def test_a_user_can_belong_to_several_hotels(engine: Engine, owner: TestClient) -> None:
    a = str(owner.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"])
    b = str(owner.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    email = "authz-multi@example.test"
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, a, "viewer")
    grant_membership(engine, email, b, "viewer")

    assert {i["public_id"] for i in client.get("/api/v1/hotels").json()["items"]} == {a, b}


def test_a_role_at_one_hotel_does_not_travel_to_another(engine: Engine, owner: TestClient) -> None:
    """The membership row is per (user, hotel); rank is read for the hotel in the URL."""
    a = str(owner.post("/api/v1/hotels", json=hotel_payload("hotel-a")).json()["public_id"])
    b = str(owner.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    email = "authz-split@example.test"
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, a, "manager")
    grant_membership(engine, email, b, "viewer")

    room_type = {
        "code": "DLX",
        "name": "Deluxe",
        "max_occupancy": 2,
        "standard_occupancy": 2,
        "bed_count": 1,
        "base_price": "120.00",
        "currency": "EUR",
    }

    assert client.post(f"/api/v1/hotels/{a}/room-types", json=room_type).status_code == 201
    assert client.post(f"/api/v1/hotels/{b}/room-types", json=room_type).status_code == 403


def test_the_same_hotel_cannot_hold_two_roles_for_one_user(
    engine: Engine, hotel: str, owner: TestClient
) -> None:
    """uq_user_hotels_user_id_hotel_id. A second grant replaces the first rather than stacking,
    so there is never an ambiguity about which rank applies."""
    email = "authz-regrant@example.test"
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, hotel, "viewer")
    grant_membership(engine, email, hotel, "manager")

    with sessionmaker(bind=engine, future=True)() as session:
        rows = (
            session.execute(
                sa.text(
                    "SELECT uh.role FROM user_hotels uh JOIN users u ON u.id = uh.user_id "
                    "WHERE u.email = :email"
                ),
                {"email": email},
            )
            .scalars()
            .all()
        )

    assert list(rows) == ["manager"]
    assert client.get(f"/api/v1/hotels/{hotel}").status_code == 200


# ======================================================================================
# 401: who are you?
# ======================================================================================


def test_no_token_is_401(engine: Engine, hotel: str) -> None:
    from app.api.deps import get_db
    from app.core.config import Settings
    from app.main import create_app
    from tests.integration.conftest import TEST_SECRET

    app = create_app(Settings(environment="test", secret_key=TEST_SECRET))
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def override() -> Iterator[object]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    anonymous = TestClient(app)

    assert anonymous.get(f"/api/v1/hotels/{hotel}").status_code == 401


@pytest.mark.parametrize(
    "header",
    ["Bearer not-a-jwt", "Bearer ", "Basic abc", "bearer eyJhbGciOiJIUzI1NiJ9.e30.x"],
    ids=["garbage", "empty", "wrong-scheme", "wrong-signature"],
)
def test_a_broken_token_is_401(member: Callable[..., TestClient], hotel: str, header: str) -> None:
    client = member("owner")
    client.headers["Authorization"] = header

    assert client.get(f"/api/v1/hotels/{hotel}").status_code == 401


def test_a_disabled_account_is_401_even_with_a_valid_token(
    engine: Engine, hotel: str, member: Callable[..., TestClient]
) -> None:
    """The token is still cryptographically fine; the account behind it is not. Deactivation
    has to take effect on the next request, not on the next token expiry."""
    email = "authz-disabled@example.test"
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, hotel, "owner")
    assert client.get(f"/api/v1/hotels/{hotel}").status_code == 200

    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :email"), {"email": email}
        )
        session.commit()

    assert client.get(f"/api/v1/hotels/{hotel}").status_code == 401


def test_a_disabled_account_cannot_log_in_again(engine: Engine) -> None:
    email = "authz-disabled-login@example.test"
    client = authenticated_client(engine, email=email)
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :email"), {"email": email}
        )
        session.commit()

    response = client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})

    assert response.status_code == 401


def test_registration_stays_public(engine: Engine) -> None:
    """Requiring a token to obtain a token would close the door on the first user."""
    client = authenticated_client(engine, email="authz-register@example.test")
    client.headers.pop("Authorization")

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "brand-new@example.test",
            "password": TEST_PASSWORD,
            "full_name": "Brand New",
        },
    )

    assert response.status_code == 201


# ======================================================================================
# 404: the wall. Three different situations, one indistinguishable answer.
# ======================================================================================


def not_found_bodies(client: TestClient, hotel: str) -> list[tuple[int, str]]:
    """(status, raw body) for a hotel that does not exist, and one that is not yours."""
    return [
        (r.status_code, r.text)
        for r in (
            client.get(f"/api/v1/hotels/{uuid.uuid4()}"),
            client.get(f"/api/v1/hotels/{hotel}"),
        )
    ]


def test_a_non_member_cannot_tell_an_unknown_hotel_from_someone_elses(
    engine: Engine, hotel: str
) -> None:
    """Byte-identical. Any difference -- a word, a code, a length -- is an enumeration oracle
    that would let an outsider map every property in the installation."""
    outsider = authenticated_client(engine, email=OUTSIDER_EMAIL)

    results = not_found_bodies(outsider, hotel)

    assert [status for status, _ in results] == [404, 404]
    assert results[0][1] == results[1][1]


def test_the_wall_holds_for_a_member_of_a_different_hotel(
    engine: Engine, owner: TestClient, hotel: str
) -> None:
    """Holding a membership somewhere must not change the answer about a hotel elsewhere."""
    other = str(owner.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    email = "authz-elsewhere@example.test"
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, other, "owner")

    results = not_found_bodies(client, hotel)

    assert [status for status, _ in results] == [404, 404]
    assert results[0][1] == results[1][1]


#: The reporting routes take a mandatory date window; without it FastAPI answers 422 before
#: the policy runs, which would test validation rather than the wall.
WINDOW = {"date_from": "2026-09-01", "date_to": "2026-09-30"}


@pytest.mark.parametrize(
    "path",
    [
        "bookings",
        "guests",
        "revenue",
        "expenses",
        "reviews",
        "room-types",
        "analytics/overview",
        "analytics/revenue-by-category",
        "intelligence/insights",
        "intelligence/anomalies",
    ],
)
def test_every_domain_answers_the_non_member_the_same_way(
    engine: Engine, world: World, path: str
) -> None:
    """The wall is not just on the hotel resource: no collection may confirm the hotel exists."""
    outsider = authenticated_client(engine, email=OUTSIDER_EMAIL)
    params = WINDOW if path.startswith(("analytics/", "intelligence/")) else None

    real = outsider.get(f"/api/v1/hotels/{world.hotel}/{path}", params=params)
    imaginary = outsider.get(f"/api/v1/hotels/{uuid.uuid4()}/{path}", params=params)

    assert real.status_code == 404, real.text
    assert real.text == imaginary.text


def test_a_malformed_hotel_id_is_422_before_anything_else(engine: Engine) -> None:
    """FastAPI rejects a non-UUID path segment before the policy runs, and that is fine: it
    reveals nothing about which hotels exist."""
    outsider = authenticated_client(engine, email=OUTSIDER_EMAIL)

    assert outsider.get("/api/v1/hotels/not-a-uuid").status_code == 422


# ======================================================================================
# The role matrix
# ======================================================================================


class Op(NamedTuple):
    """One operation, and the minimum role the authorised matrix gives it."""

    name: str
    method: str
    url: str
    payload: dict[str, object] | None
    required: str
    #: Query string, where the route requires one to get past validation at all.
    params: dict[str, str] | None = None


ROOM_TYPE_BODY: dict[str, object] = {
    "code": "STD",
    "name": "Standard",
    "max_occupancy": 2,
    "standard_occupancy": 2,
    "bed_count": 1,
    "base_price": "90.00",
    "currency": "EUR",
}

OPERATIONS: list[Op] = [
    # --- reads: the floor is membership itself ---------------------------------------------
    Op("read hotel", "GET", "/api/v1/hotels/{hotel}", None, "viewer"),
    Op("list bookings", "GET", "/api/v1/hotels/{hotel}/bookings", None, "viewer"),
    Op("list guests", "GET", "/api/v1/hotels/{hotel}/guests", None, "viewer"),
    Op("list revenue", "GET", "/api/v1/hotels/{hotel}/revenue", None, "viewer"),
    Op("list expenses", "GET", "/api/v1/hotels/{hotel}/expenses", None, "viewer"),
    Op("list reviews", "GET", "/api/v1/hotels/{hotel}/reviews", None, "viewer"),
    Op("list room types", "GET", "/api/v1/hotels/{hotel}/room-types", None, "viewer"),
    Op(
        "read analytics",
        "GET",
        "/api/v1/hotels/{hotel}/analytics/overview",
        None,
        "viewer",
        WINDOW,
    ),
    Op(
        "read intelligence",
        "GET",
        "/api/v1/hotels/{hotel}/intelligence/insights",
        None,
        "viewer",
        WINDOW,
    ),
    Op(
        "read payments",
        "GET",
        "/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        None,
        "viewer",
    ),
    # --- staff: the daily front-desk work ---------------------------------------------------
    Op(
        "create guest",
        "POST",
        "/api/v1/hotels/{hotel}/guests",
        {"first_name": "Grace", "last_name": "Hopper"},
        "staff",
    ),
    Op(
        "update guest",
        "PATCH",
        "/api/v1/hotels/{hotel}/guests/{guest}",
        {"first_name": "Adele"},
        "staff",
    ),
    Op(
        "create booking",
        "POST",
        "/api/v1/hotels/{hotel}/bookings",
        {
            "guest_public_id": "{guest}",
            "reference": "BK-MATRIX01",
            "check_in_date": "2026-10-01",
            "check_out_date": "2026-10-03",
            "status": "confirmed",
            "total_amount": "240.00",
            "currency": "EUR",
            "rooms": [
                {
                    "room_number": "101",
                    "nights": [
                        {"stay_date": "2026-10-01", "rate": "120.00"},
                        {"stay_date": "2026-10-02", "rate": "120.00"},
                    ],
                }
            ],
        },
        "staff",
    ),
    Op(
        "update booking",
        "PATCH",
        "/api/v1/hotels/{hotel}/bookings/{booking}",
        {"status": "checked_in"},
        "staff",
    ),
    Op(
        "take payment",
        "POST",
        "/api/v1/hotels/{hotel}/bookings/{booking}/payments",
        {"amount": "10.00", "currency": "EUR", "method": "cash"},
        "staff",
    ),
    Op(
        "refund payment",
        "POST",
        "/api/v1/hotels/{hotel}/bookings/{booking}/payments/refunds",
        # A positive amount: the direction is carried by `refunds_public_id`, not by the sign.
        {
            "amount": "5.00",
            "currency": "EUR",
            "method": "card",
            "refunds_public_id": "{payment}",
        },
        "staff",
    ),
    Op(
        "moderate review",
        "PATCH",
        "/api/v1/hotels/{hotel}/bookings/{booking}/review",
        {"is_published": False},
        "staff",
    ),
    Op(
        "post revenue",
        "POST",
        "/api/v1/hotels/{hotel}/revenue",
        {
            "category_code": "FB",
            "revenue_date": "2026-09-02",
            "amount": "50.00",
            "currency": "EUR",
        },
        "staff",
    ),
    Op(
        "post expense",
        "POST",
        "/api/v1/hotels/{hotel}/expenses",
        {
            "category_code": "UTILITIES",
            "expense_date": "2026-09-02",
            "amount": "20.00",
            "currency": "EUR",
        },
        "staff",
    ),
    # --- manager: the property's shape, and destroying records ------------------------------
    Op("create room type", "POST", "/api/v1/hotels/{hotel}/room-types", ROOM_TYPE_BODY, "manager"),
    Op(
        "update room type",
        "PATCH",
        "/api/v1/hotels/{hotel}/room-types/{room_type}",
        {"name": "Deluxe plus"},
        "manager",
    ),
    Op(
        "create room",
        "POST",
        "/api/v1/hotels/{hotel}/room-types/{room_type}/rooms",
        {"room_number": "102"},
        "manager",
    ),
    Op(
        "update room",
        "PATCH",
        "/api/v1/hotels/{hotel}/room-types/{room_type}/rooms/{room}",
        {"floor": 2},
        "manager",
    ),
    Op(
        "delete room",
        "DELETE",
        "/api/v1/hotels/{hotel}/room-types/{room_type}/rooms/{room}",
        None,
        "manager",
    ),
    Op(
        "assign amenity",
        "POST",
        "/api/v1/hotels/{hotel}/room-types/{room_type}/amenities",
        {"code": "{amenity}"},
        "manager",
    ),
    Op(
        "unassign amenity",
        "DELETE",
        "/api/v1/hotels/{hotel}/room-types/{room_type}/amenities/{amenity}",
        None,
        "manager",
    ),
    Op(
        "delete room type",
        "DELETE",
        "/api/v1/hotels/{hotel}/room-types/{room_type}",
        None,
        "manager",
    ),
    Op(
        "delete booking",
        "DELETE",
        "/api/v1/hotels/{hotel}/bookings/{booking}",
        None,
        "manager",
    ),
    Op("delete guest", "DELETE", "/api/v1/hotels/{hotel}/guests/{guest}", None, "manager"),
    # --- owner: the property itself ---------------------------------------------------------
    Op("rename hotel", "PATCH", "/api/v1/hotels/{hotel}", {"name": "Renamed"}, "owner"),
    Op("delete hotel", "DELETE", "/api/v1/hotels/{hotel}", None, "owner"),
]


def fill(value: object, world: World) -> object:
    """Substitute ``{placeholder}`` identifiers, at any depth of the payload."""
    if isinstance(value, str):
        return value.format(**world._asdict())
    if isinstance(value, dict):
        return {k: fill(v, world) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, world) for v in value]
    return value


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("op", OPERATIONS, ids=lambda op: op.name.replace(" ", "-"))
def test_the_role_matrix(
    member: Callable[..., TestClient], world: World, role: str, op: Op
) -> None:
    """Every role against every operation.

    The assertion is deliberately about AUTHORIZATION only. A permitted call is asserted not
    to be refused (401/403/404) rather than to return a particular success code, because the
    domain outcome of an operation -- 200 against 201, or a 409 from a rule that has nothing to
    do with roles -- is the subject of that domain's own suite. Pinning it here would make this
    matrix fail for reasons that are not about permission.
    """
    client = member(role)
    url = str(fill(op.url, world))
    payload = fill(op.payload, world) if op.payload is not None else None

    response = client.request(op.method, url, json=payload, params=op.params)

    if RANK[role] >= RANK[op.required]:
        # 422 is included deliberately. Every payload in the matrix is a valid one, so a
        # validation error here means the ROW is wrong, not the permission -- and a row that
        # silently 422s would assert nothing about authorization while still passing.
        assert response.status_code not in (401, 403, 404, 422), (
            f"{role} should be allowed to {op.name}, got {response.status_code}: {response.text}"
        )
    else:
        assert response.status_code == 403, (
            f"{role} must not be allowed to {op.name}, got {response.status_code}"
        )


def test_the_matrix_covers_every_role_boundary() -> None:
    """Guards the guard: a matrix that had drifted to one role would still pass every row."""
    assert {op.required for op in OPERATIONS} == set(ROLES)


def test_a_refused_write_changes_nothing(
    member: Callable[..., TestClient], world: World, engine: Engine
) -> None:
    """403 is raised as a route dependency, before any service or session work."""
    viewer = member("viewer")

    viewer.post(
        f"/api/v1/hotels/{world.hotel}/guests",
        json={"first_name": "Should", "last_name": "NotExist"},
    )
    viewer.delete(f"/api/v1/hotels/{world.hotel}/bookings/{world.booking}")

    with sessionmaker(bind=engine, future=True)() as session:
        guests = session.scalar(sa.text("SELECT count(*) FROM guests"))
        bookings = session.scalar(sa.text("SELECT count(*) FROM bookings"))

    assert guests == 1, "a refused create still wrote a row"
    assert bookings == 1, "a refused delete still removed a row"


# ======================================================================================
# Cross-hotel reachability: the IDOR sweep
# ======================================================================================


@pytest.fixture
def neighbour(owner: TestClient, engine: Engine) -> tuple[TestClient, str]:
    """A second hotel with its own owner, who has no membership of the first."""
    email = "authz-neighbour@example.test"
    client = authenticated_client(engine, email=email)
    other = str(client.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    return client, other


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "bookings/{booking}", None),
        ("PATCH", "bookings/{booking}", {"status": "checked_in"}),
        ("DELETE", "bookings/{booking}", None),
        ("GET", "guests/{guest}", None),
        ("PATCH", "guests/{guest}", {"first_name": "Mallory"}),
        ("DELETE", "guests/{guest}", None),
        ("GET", "bookings/{booking}/payments/{payment}", None),
        ("GET", "bookings/{booking}/review", None),
        ("PATCH", "bookings/{booking}/review", {"is_published": False}),
        ("GET", "room-types/{room_type}", None),
        ("PATCH", "room-types/{room_type}", {"name": "Taken over"}),
        ("DELETE", "room-types/{room_type}", None),
        ("GET", "room-types/{room_type}/rooms/{room}", None),
        ("DELETE", "room-types/{room_type}/rooms/{room}", None),
    ],
    ids=lambda value: str(value).replace("/", "-")[:40] if isinstance(value, str) else "",
)
def test_a_neighbour_cannot_reach_another_hotels_records(
    neighbour: tuple[TestClient, str],
    world: World,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    """Owner of hotel B, addressing hotel A's URLs. Every one is 404: the resolver refuses at
    the hotel, so no per-record identifier is ever tested against B's data."""
    client, _ = neighbour
    url = f"/api/v1/hotels/{world.hotel}/{path.format(**world._asdict())}"

    response = client.request(method, url, json=payload)

    assert response.status_code == 404, f"{method} {url} returned {response.status_code}"


def test_a_neighbours_own_hotel_still_works(
    neighbour: tuple[TestClient, str], world: World
) -> None:
    """Guards the guard: a client that could reach nothing would pass the sweep above."""
    client, other = neighbour

    assert client.get(f"/api/v1/hotels/{other}").status_code == 200


def test_another_hotels_record_is_not_reachable_by_swapping_the_hotel(
    member: Callable[..., TestClient], owner: TestClient, world: World, engine: Engine
) -> None:
    """The classic IDOR: a legitimate member of B pastes A's booking id under B's hotel."""
    email = "authz-swapper@example.test"
    other = str(owner.post("/api/v1/hotels", json=hotel_payload("hotel-b")).json()["public_id"])
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, other, "owner")

    response = client.get(f"/api/v1/hotels/{other}/bookings/{world.booking}")

    assert response.status_code == 404


# ======================================================================================
# The global catalogues
# ======================================================================================


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("collection", ["amenities", "revenue-categories", "expense-categories"])
def test_every_role_may_read_a_global_catalogue(
    member: Callable[..., TestClient], world: World, role: str, collection: str
) -> None:
    """A hotel must be able to reference the vocabulary it is required to use."""
    client = member(role)

    assert client.get(f"/api/v1/{collection}").status_code == 200


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    ("collection", "payload"),
    [
        ("amenities", {"code": "POOL", "name": "Pool"}),
        ("revenue-categories", {"code": "SPA", "name": "Spa"}),
        ("expense-categories", {"code": "RENT", "name": "Rent"}),
    ],
)
def test_no_role_may_write_a_global_catalogue(
    member: Callable[..., TestClient], role: str, collection: str, payload: dict[str, object]
) -> None:
    """Not even an owner: the row belongs to no hotel, so no per-hotel grant reaches it."""
    client = member(role)

    assert client.post(f"/api/v1/{collection}", json=payload).status_code == 403


def test_a_catalogue_write_is_401_without_a_token_not_403(engine: Engine) -> None:
    """The two failures must stay distinguishable in the right direction: an anonymous caller
    is told to authenticate, not that the resource is off limits to everyone."""
    client = authenticated_client(engine, email="authz-anon-catalogue@example.test")
    client.headers.pop("Authorization")

    assert (
        client.post("/api/v1/amenities", json={"code": "POOL", "name": "Pool"}).status_code == 401
    )


# ======================================================================================
# What the refusals say, and do not say
# ======================================================================================


BIGINT = re.compile(r"\b\d{1,6}\b")


def test_the_404_names_no_role_and_no_membership(engine: Engine, hotel: str) -> None:
    """It must not hint that membership is the thing standing in the way -- that is itself the
    confirmation the hotel exists."""
    outsider = authenticated_client(engine, email=OUTSIDER_EMAIL)

    text = outsider.get(f"/api/v1/hotels/{hotel}").text.lower()

    for leak in ["member", "user_hotels", "viewer", "staff", "manager", "owner", "role", "grant"]:
        assert leak not in text, f"the 404 leaked {leak!r}"


def test_the_403_explains_the_requirement_without_leaking_internals(
    member: Callable[..., TestClient], world: World
) -> None:
    """Naming the required role is deliberate and safe: only a member ever sees a 403. Naming
    the table, the caller's own role or an internal key is not."""
    viewer = member("viewer")

    body = viewer.post(
        f"/api/v1/hotels/{world.hotel}/guests", json={"first_name": "A", "last_name": "B"}
    ).json()

    assert body["error"]["code"] == "FORBIDDEN"
    text = body["error"]["message"].lower()
    assert "manager" in text or "staff" in text
    for leak in ["user_hotels", "select", "sqlstate", "psycopg", "traceback", "hotel_id"]:
        assert leak not in text, f"the 403 leaked {leak!r}"


@pytest.mark.parametrize(
    ("label", "call"),
    [
        ("unknown hotel", lambda c, w: c.get(f"/api/v1/hotels/{uuid.uuid4()}")),
        (
            "forbidden write",
            lambda c, w: c.post(
                f"/api/v1/hotels/{w.hotel}/guests", json={"first_name": "A", "last_name": "B"}
            ),
        ),
    ],
)
def test_no_refusal_exposes_an_internal_key(
    member: Callable[..., TestClient],
    world: World,
    label: str,
    call: Callable[[TestClient, World], object],
) -> None:
    """No BIGINT primary key -- not the user's, not the hotel's, not the membership's."""
    viewer = member("viewer")

    body = call(viewer, world).json()  # type: ignore[attr-defined]

    assert body["error"]["details"] in (None, {}, [])
    assert BIGINT.search(body["error"]["message"]) is None, f"{label} leaked a small integer"


def test_the_membership_row_is_never_serialised(
    member: Callable[..., TestClient], world: World
) -> None:
    """No endpoint returns a membership, so no response may carry one's shape."""
    viewer = member("viewer")

    text = viewer.get(f"/api/v1/hotels/{world.hotel}").text

    for leak in ["user_hotels", "user_id", "role", "membership"]:
        assert leak not in text, f"the hotel response leaked {leak!r}"


def test_the_error_envelope_is_the_same_for_401_403_and_404(
    engine: Engine, member: Callable[..., TestClient], world: World
) -> None:
    """A caller must not be able to fingerprint the failure by its shape rather than its code."""
    viewer = member("viewer")
    anonymous = authenticated_client(engine, email="authz-envelope@example.test")
    anonymous.headers.pop("Authorization")

    bodies = [
        anonymous.get(f"/api/v1/hotels/{world.hotel}").json(),
        viewer.post(
            f"/api/v1/hotels/{world.hotel}/guests", json={"first_name": "A", "last_name": "B"}
        ).json(),
        viewer.get(f"/api/v1/hotels/{uuid.uuid4()}").json(),
    ]

    for body in bodies:
        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message", "details"}
