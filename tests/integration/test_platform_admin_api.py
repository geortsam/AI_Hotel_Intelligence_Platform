"""Platform administration against real PostgreSQL.

Stage 4.3. The three global catalogues -- ``amenities``, ``revenue_categories``,
``expense_categories`` -- have no ``hotel_id``: one row is shared by every property. Stage 4.2
closed their write routes to everyone because no per-hotel role can govern a row that belongs
to no hotel. This stage reopens them to exactly one identity.

**The central claim of this suite is a negative one.** A platform administrator may edit the
shared vocabularies and *nothing else*. Holding the grant must not move the hotel wall by one
byte: a platform admin with no membership of a hotel gets the same 404 a stranger gets, on
every route, and cannot reach a single row of another tenant's operational data. Roughly half
the tests below exist to prove that the privilege does **not** compose.

SQLite is not substituted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from tests.integration.conftest import (
    TEST_PASSWORD,
    authenticated_client,
    grant_membership,
    grant_platform_admin,
    requires_postgres,
    revoke_platform_admin,
    seed_amenity,
    seed_expense_category,
    seed_revenue_category,
)

pytestmark = requires_postgres

ADMIN_EMAIL = "platform-admin@example.test"
TENANT_EMAIL = "platform-tenant@example.test"

CHECK_IN = dt.date(2026, 9, 1)
CHECK_OUT = dt.date(2026, 9, 4)

AMENITIES = "/api/v1/amenities"
REVENUE_CATEGORIES = "/api/v1/revenue-categories"
EXPENSE_CATEGORIES = "/api/v1/expense-categories"

#: The three genuine global catalogues. `room_types` is NOT among them: it carries a
#: `hotel_id`, so one hotel's DLX is a different row from another's, and it stays under
#: `require_role(MANAGER)` exactly as Stage 4.2 left it.
CATALOGUES = [AMENITIES, REVENUE_CATEGORIES, EXPENSE_CATEGORIES]

#: The four hotel roles, none of which may write a catalogue.
HOTEL_ROLES = ["viewer", "staff", "manager", "owner"]

CREATE_PAYLOAD: dict[str, dict[str, object]] = {
    AMENITIES: {"code": "POOL", "name": "Swimming pool"},
    REVENUE_CATEGORIES: {"code": "SPA", "name": "Spa treatments"},
    EXPENSE_CATEGORIES: {"code": "RENT", "name": "Rent"},
}

#: One seeded row per catalogue, so PATCH and DELETE have a target.
SEEDED_CODE = {AMENITIES: "WIFI", REVENUE_CATEGORIES: "FB", EXPENSE_CATEGORIES: "UTILITIES"}


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
    yield
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(
            sa.text(
                "TRUNCATE amenities, revenue_categories, expense_categories, platform_admins, "
                "user_hotels, users, hotels RESTART IDENTITY CASCADE"
            )
        )
        cleanup.commit()


@pytest.fixture
def catalogues(engine: Engine) -> None:
    """One row in each catalogue, seeded out of band."""
    seed_amenity(engine, "WIFI", "Wi-Fi", "technology")
    seed_revenue_category(engine, "FB", "Food and beverage")
    seed_expense_category(engine, "UTILITIES", "Utilities")
    return None


@pytest.fixture
def admin(engine: Engine) -> TestClient:
    """A platform administrator who is a member of no hotel at all.

    Deliberately memberless: every isolation test below would be weakened by an admin who
    happened to have a membership lying around.
    """
    client = authenticated_client(engine, email=ADMIN_EMAIL)
    grant_platform_admin(engine, ADMIN_EMAIL)
    return client


@pytest.fixture
def tenant(engine: Engine) -> TestClient:
    """An ordinary account that owns its own hotel and holds no platform grant."""
    return authenticated_client(engine, email=TENANT_EMAIL)


@pytest.fixture
def tenant_hotel(tenant: TestClient) -> str:
    return str(tenant.post("/api/v1/hotels", json=hotel_payload("tenant-a")).json()["public_id"])


@pytest.fixture
def hotel_member(engine: Engine, tenant_hotel: str) -> Callable[[str], TestClient]:
    """An account holding *role* at the tenant's hotel, and no platform grant."""

    def build(role: str) -> TestClient:
        email = f"platform-{role}-{uuid.uuid4().hex[:8]}@example.test"
        client = authenticated_client(engine, email=email)
        grant_membership(engine, email, tenant_hotel, role)
        return client

    return build


# ======================================================================================
# The grant is server-side state, and is read per request
# ======================================================================================


def test_the_grant_is_what_changes_the_answer(engine: Engine, catalogues: None) -> None:
    """Same account, same token, before and after the grant.

    The token is issued once and never re-issued, so this also demonstrates that the
    privilege is not carried in the token: nothing about the credential changed.
    """
    client = authenticated_client(engine, email=ADMIN_EMAIL)

    before = client.post(AMENITIES, json={"code": "POOL", "name": "Pool"})
    grant_platform_admin(engine, ADMIN_EMAIL)
    after = client.post(AMENITIES, json={"code": "POOL", "name": "Pool"})

    assert before.status_code == 403
    assert after.status_code == 201


def test_revoking_the_grant_takes_effect_immediately(
    engine: Engine, admin: TestClient, catalogues: None
) -> None:
    """Not on the next login, and not when the token expires."""
    assert admin.post(AMENITIES, json={"code": "POOL", "name": "Pool"}).status_code == 201

    revoke_platform_admin(engine, ADMIN_EMAIL)

    assert admin.post(AMENITIES, json={"code": "SAUNA", "name": "Sauna"}).status_code == 403


def test_the_token_carries_no_platform_claim(admin: TestClient) -> None:
    """A JWT is signed but not encrypted. A privilege named in one is readable by its holder
    and stale the moment the grant changes, so the token must not mention it."""
    import base64
    import json

    token = str(admin.headers["Authorization"]).removeprefix("Bearer ")
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))

    assert set(claims) == {"sub", "iat", "exp", "jti", "typ"}
    for forbidden in ("role", "platform", "admin", "scope", "permissions"):
        assert not any(forbidden in str(key).lower() for key in claims), forbidden
        assert not any(forbidden in str(value).lower() for value in claims.values()), forbidden


def test_a_disabled_platform_admin_is_401_not_403(
    engine: Engine, admin: TestClient, catalogues: None
) -> None:
    """Authentication fails before authorization is consulted, so a disabled administrator
    looks like a stranger rather than like someone lacking a privilege."""
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": ADMIN_EMAIL}
        )
        session.commit()

    assert admin.post(AMENITIES, json={"code": "POOL", "name": "Pool"}).status_code == 401


def test_a_disabled_platform_admin_cannot_log_in(engine: Engine, admin: TestClient) -> None:
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": ADMIN_EMAIL}
        )
        session.commit()

    response = admin.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": TEST_PASSWORD}
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "header",
    ["Bearer not-a-jwt", "Bearer ", "Basic abc", "bearer eyJhbGciOiJIUzI1NiJ9.e30.x"],
    ids=["garbage", "empty", "wrong-scheme", "wrong-signature"],
)
def test_a_broken_token_is_401_even_for_an_administrator(
    admin: TestClient, catalogues: None, header: str
) -> None:
    admin.headers["Authorization"] = header

    assert admin.post(AMENITIES, json={"code": "POOL", "name": "Pool"}).status_code == 401


def test_an_expired_token_is_401(engine: Engine, catalogues: None) -> None:
    """Signed by the right key, for the right subject, holding the right grant -- and refused,
    because expiry is checked before anything else."""
    import datetime as _dt

    from app.core.config import Settings
    from app.core.security import create_access_token
    from tests.integration.conftest import TEST_SECRET

    client = authenticated_client(engine, email=ADMIN_EMAIL)
    grant_platform_admin(engine, ADMIN_EMAIL)
    subject = uuid.UUID(str(client.get("/api/v1/auth/me").json()["public_id"]))

    expired = create_access_token(
        Settings(environment="test", secret_key=TEST_SECRET),
        subject,
        now=_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=48),
    )
    client.headers["Authorization"] = f"Bearer {expired}"

    assert client.post(AMENITIES, json={"code": "POOL", "name": "Pool"}).status_code == 401


def test_the_platform_role_cannot_be_claimed_in_a_payload(engine: Engine, catalogues: None) -> None:
    """Registration accepts no authorization field, and would not be believed if it did."""
    client = authenticated_client(engine, email="platform-liar@example.test")

    rejected = client.post(
        "/api/v1/auth/register",
        json={
            "email": "claimed@example.test",
            "password": TEST_PASSWORD,
            "full_name": "Claimed Admin",
            "role": "platform_admin",
        },
    )

    assert rejected.status_code == 422
    assert client.post(AMENITIES, json={"code": "POOL", "name": "Pool"}).status_code == 403


# ======================================================================================
# The catalogue write matrix
# ======================================================================================


@pytest.mark.parametrize("collection", CATALOGUES)
def test_a_platform_admin_may_create(admin: TestClient, catalogues: None, collection: str) -> None:
    response = admin.post(collection, json=CREATE_PAYLOAD[collection])

    assert response.status_code == 201


@pytest.mark.parametrize("collection", CATALOGUES)
def test_a_platform_admin_may_update(admin: TestClient, catalogues: None, collection: str) -> None:
    response = admin.patch(
        f"{collection}/{SEEDED_CODE[collection]}", json={"name": "Renamed by the platform"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed by the platform"


@pytest.mark.parametrize("collection", CATALOGUES)
def test_a_platform_admin_may_delete_an_unused_entry(
    admin: TestClient, catalogues: None, collection: str
) -> None:
    code = SEEDED_CODE[collection]

    assert admin.delete(f"{collection}/{code}").status_code == 204
    assert admin.get(f"{collection}/{code}").status_code == 404


@pytest.mark.parametrize("role", HOTEL_ROLES)
@pytest.mark.parametrize("collection", CATALOGUES)
def test_no_hotel_role_may_write_a_catalogue(
    hotel_member: Callable[[str], TestClient], catalogues: None, role: str, collection: str
) -> None:
    """Owner included. The hotel hierarchy tops out below this privilege, not at it."""
    client = hotel_member(role)
    code = SEEDED_CODE[collection]

    assert client.post(collection, json=CREATE_PAYLOAD[collection]).status_code == 403
    assert client.patch(f"{collection}/{code}", json={"name": "x"}).status_code == 403
    assert client.delete(f"{collection}/{code}").status_code == 403


@pytest.mark.parametrize("collection", CATALOGUES)
def test_an_authenticated_non_admin_may_still_read(
    tenant: TestClient, catalogues: None, collection: str
) -> None:
    """Stage 4.2's read policy, unchanged: every hotel must reference the vocabulary it is
    required to use, so reading was never the thing in question."""
    assert tenant.get(collection).status_code == 200
    assert tenant.get(f"{collection}/{SEEDED_CODE[collection]}").status_code == 200


@pytest.mark.parametrize("collection", CATALOGUES)
def test_an_anonymous_caller_is_401_not_403(
    engine: Engine, catalogues: None, collection: str
) -> None:
    """The two failures must stay distinguishable in that direction: a stranger is told to
    authenticate, not told which privilege they lack."""
    client = authenticated_client(engine, email="platform-anon@example.test")
    client.headers.pop("Authorization")

    assert client.get(collection).status_code == 401
    assert client.post(collection, json=CREATE_PAYLOAD[collection]).status_code == 401


def test_a_refused_catalogue_write_changes_nothing(
    hotel_member: Callable[[str], TestClient], catalogues: None, engine: Engine
) -> None:
    """403 is raised as a route dependency, before the service or the session are involved."""
    owner = hotel_member("owner")

    owner.post(AMENITIES, json={"code": "POOL", "name": "Pool"})
    owner.patch(f"{AMENITIES}/WIFI", json={"name": "Renamed"})
    owner.delete(f"{AMENITIES}/WIFI")

    with sessionmaker(bind=engine, future=True)() as session:
        rows = session.execute(sa.text("SELECT code, name FROM amenities ORDER BY code")).all()

    assert [tuple(r) for r in rows] == [("WIFI", "Wi-Fi")]


# ======================================================================================
# Catalogue semantics are unchanged -- the privilege reopened the door, not the rules
# ======================================================================================


def test_a_duplicate_code_is_still_a_conflict(admin: TestClient, catalogues: None) -> None:
    assert admin.post(AMENITIES, json={"code": "WIFI", "name": "Anything"}).status_code == 409


def test_a_lowercase_code_is_still_upcased(admin: TestClient, catalogues: None) -> None:
    assert admin.post(AMENITIES, json={"code": "pool", "name": "Pool"}).json()["code"] == "POOL"


def test_the_natural_key_is_still_immutable(admin: TestClient, catalogues: None) -> None:
    assert admin.patch(f"{AMENITIES}/WIFI", json={"code": "NEWCODE"}).status_code == 422


def test_restrict_still_refuses_a_catalogue_entry_in_use(
    admin: TestClient, tenant: TestClient, tenant_hotel: str, catalogues: None
) -> None:
    """A platform administrator does not outrank a foreign key."""
    tenant.post(
        f"/api/v1/hotels/{tenant_hotel}/room-types",
        json={
            "code": "DLX",
            "name": "Deluxe",
            "max_occupancy": 2,
            "standard_occupancy": 2,
            "bed_count": 1,
            "base_price": "120.00",
            "currency": "EUR",
        },
    )
    tenant.post(f"/api/v1/hotels/{tenant_hotel}/room-types/DLX/amenities", json={"code": "WIFI"})

    response = admin.delete(f"{AMENITIES}/WIFI")

    assert response.status_code == 409
    assert admin.get(f"{AMENITIES}/WIFI").status_code == 200


def test_a_rejected_admin_write_rolls_back(
    admin: TestClient, catalogues: None, engine: Engine
) -> None:
    admin.post(AMENITIES, json={"code": "WIFI", "name": "Duplicate"})

    with sessionmaker(bind=engine, future=True)() as session:
        rows = session.execute(sa.text("SELECT code, name FROM amenities ORDER BY code")).all()

    assert [tuple(r) for r in rows] == [("WIFI", "Wi-Fi")]


def test_an_admin_conflict_leaks_no_sql_or_constraint_name(
    admin: TestClient, catalogues: None
) -> None:
    text = admin.post(AMENITIES, json={"code": "WIFI", "name": "x"}).text.lower()

    for leak in ["insert", "uq_amenities_code", "psycopg", "sqlalchemy", "traceback", "23505"]:
        assert leak not in text, f"leaked {leak!r}"


def test_the_catalogue_is_global_for_the_administrator_too(
    admin: TestClient, tenant: TestClient, tenant_hotel: str, catalogues: None
) -> None:
    """What the administrator creates is visible to every tenant, which is the whole reason
    the privilege has to exist above the hotel hierarchy rather than inside it."""
    admin.post(REVENUE_CATEGORIES, json={"code": "SPA", "name": "Spa"})

    assert tenant.get(f"{REVENUE_CATEGORIES}/SPA").status_code == 200


# ======================================================================================
# Tenant isolation: the privilege does not compose
# ======================================================================================


@pytest.mark.parametrize(
    "path",
    [
        "",
        "bookings",
        "guests",
        "revenue",
        "expenses",
        "reviews",
        "room-types",
    ],
)
def test_a_platform_admin_is_not_a_member_of_anything(
    admin: TestClient, tenant_hotel: str, catalogues: None, path: str
) -> None:
    """The 404 wall does not move for a platform administrator.

    This is the single most important property in Stage 4.3: the grant governs rows that
    belong to no hotel, and confers nothing over rows that belong to one.
    """
    url = f"/api/v1/hotels/{tenant_hotel}" + (f"/{path}" if path else "")

    assert admin.get(url).status_code == 404


def test_the_admins_404_is_identical_to_a_strangers(
    engine: Engine, admin: TestClient, tenant_hotel: str
) -> None:
    """Byte for byte. If holding the grant changed the response at all, the grant would be a
    way to enumerate which hotels exist."""
    stranger = authenticated_client(engine, email="platform-stranger@example.test")

    as_admin = admin.get(f"/api/v1/hotels/{tenant_hotel}")
    as_stranger = stranger.get(f"/api/v1/hotels/{tenant_hotel}")

    assert as_admin.status_code == as_stranger.status_code == 404
    assert as_admin.text == as_stranger.text


def test_the_admins_404_is_identical_for_a_real_and_an_imaginary_hotel(
    admin: TestClient, tenant_hotel: str
) -> None:
    real = admin.get(f"/api/v1/hotels/{tenant_hotel}")
    imaginary = admin.get(f"/api/v1/hotels/{uuid.uuid4()}")

    assert real.status_code == imaginary.status_code == 404
    assert real.text == imaginary.text


def test_the_hotel_listing_shows_an_admin_nothing(admin: TestClient, tenant_hotel: str) -> None:
    """`GET /hotels` is filtered by membership, and a platform grant is not a membership."""
    body = admin.get("/api/v1/hotels").json()

    assert body["items"] == []
    assert body["total"] == 0


@pytest.mark.parametrize(
    ("method", "payload"),
    [("PATCH", {"name": "Seized"}), ("DELETE", None)],
)
def test_a_platform_admin_cannot_administer_a_hotel(
    admin: TestClient, tenant_hotel: str, method: str, payload: dict[str, object] | None
) -> None:
    """Not even the operations an OWNER may perform. Platform authority is not a superset."""
    response = admin.request(method, f"/api/v1/hotels/{tenant_hotel}", json=payload)

    assert response.status_code == 404


def test_a_platform_admin_cannot_write_hotel_scoped_data(
    admin: TestClient, tenant_hotel: str, catalogues: None
) -> None:
    responses = [
        admin.post(
            f"/api/v1/hotels/{tenant_hotel}/guests",
            json={"first_name": "Mallory", "last_name": "Intruder"},
        ),
        admin.post(
            f"/api/v1/hotels/{tenant_hotel}/room-types",
            json={
                "code": "STD",
                "name": "Standard",
                "max_occupancy": 2,
                "standard_occupancy": 2,
                "bed_count": 1,
                "base_price": "90.00",
                "currency": "EUR",
            },
        ),
        admin.post(
            f"/api/v1/hotels/{tenant_hotel}/revenue",
            json={
                "category_code": "FB",
                "revenue_date": "2026-09-02",
                "amount": "10.00",
                "currency": "EUR",
            },
        ),
    ]

    assert [r.status_code for r in responses] == [404, 404, 404]


def test_a_platform_admin_cannot_reach_a_record_by_its_public_id(
    admin: TestClient, tenant: TestClient, tenant_hotel: str
) -> None:
    """Knowing an identifier is not authority to use it. The hotel is resolved first, so the
    booking's own id is never even tested."""
    guest = tenant.post(
        f"/api/v1/hotels/{tenant_hotel}/guests", json={"first_name": "Ada", "last_name": "L"}
    ).json()["public_id"]

    assert admin.get(f"/api/v1/hotels/{tenant_hotel}/guests/{guest}").status_code == 404


def test_room_types_stay_under_the_hotel_role(
    admin: TestClient, hotel_member: Callable[[str], TestClient], tenant_hotel: str
) -> None:
    """`room_types` carries a hotel_id, so it is NOT a global catalogue and Stage 4.3 left it
    alone: a manager may write it, and the platform administrator may not see it at all."""
    manager = hotel_member("manager")
    body = {
        "code": "STD",
        "name": "Standard",
        "max_occupancy": 2,
        "standard_occupancy": 2,
        "bed_count": 1,
        "base_price": "90.00",
        "currency": "EUR",
    }

    assert manager.post(f"/api/v1/hotels/{tenant_hotel}/room-types", json=body).status_code == 201
    assert admin.post(f"/api/v1/hotels/{tenant_hotel}/room-types", json=body).status_code == 404


def test_a_membership_grants_an_admin_exactly_what_it_grants_anyone(
    engine: Engine, admin: TestClient, tenant_hotel: str, catalogues: None
) -> None:
    """Given a real membership, the administrator gets that role and no more -- the platform
    grant does not top it up."""
    grant_membership(engine, ADMIN_EMAIL, tenant_hotel, "viewer")

    assert admin.get(f"/api/v1/hotels/{tenant_hotel}").status_code == 200
    assert (
        admin.post(
            f"/api/v1/hotels/{tenant_hotel}/guests",
            json={"first_name": "Ada", "last_name": "L"},
        ).status_code
        == 403
    )
    assert admin.patch(f"/api/v1/hotels/{tenant_hotel}", json={"name": "x"}).status_code == 403


# ======================================================================================
# What the refusal says
# ======================================================================================


def test_the_403_names_no_internal_state(
    hotel_member: Callable[[str], TestClient], catalogues: None
) -> None:
    body = hotel_member("owner").post(AMENITIES, json={"code": "POOL", "name": "Pool"}).json()

    assert body["error"]["code"] == "FORBIDDEN"
    text = body["error"]["message"].lower()
    for leak in [
        "platform_admins",
        "user_hotels",
        "user_id",
        "select",
        "sqlstate",
        "psycopg",
        "traceback",
    ]:
        assert leak not in text, f"the 403 leaked {leak!r}"


def test_the_403_does_not_reveal_who_holds_the_grant(
    hotel_member: Callable[[str], TestClient], admin: TestClient, catalogues: None
) -> None:
    """It may name the privilege required -- that is actionable and not secret -- but must not
    enumerate or hint at who has it."""
    text = hotel_member("owner").post(AMENITIES, json={"code": "POOL", "name": "P"}).text

    assert ADMIN_EMAIL not in text
    assert "platform administrator" in text.lower()


def test_no_refusal_exposes_an_internal_key(
    hotel_member: Callable[[str], TestClient], catalogues: None
) -> None:
    import re

    body = hotel_member("staff").delete(f"{AMENITIES}/WIFI").json()

    assert body["error"]["details"] in (None, {}, [])
    assert re.search(r"\b\d{1,6}\b", body["error"]["message"]) is None


def test_the_error_envelope_is_the_shared_one(
    hotel_member: Callable[[str], TestClient], catalogues: None
) -> None:
    body = hotel_member("owner").post(AMENITIES, json={"code": "POOL", "name": "Pool"}).json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_the_platform_grant_is_never_serialised(admin: TestClient) -> None:
    """No endpoint returns a platform grant, so no response may carry its shape.

    Asserted on the FIELD SET rather than by scanning the body for words: the administrator's
    own email address contains "platform", and a substring sweep would have called that a leak
    while missing a field genuinely named `granted` or `capability`.
    """
    body = admin.get("/api/v1/auth/me").json()

    assert set(body) == {
        "public_id",
        "email",
        "full_name",
        "is_active",
        "created_at",
        "last_login_at",
    }
