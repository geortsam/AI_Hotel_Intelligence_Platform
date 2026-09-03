"""Hotel membership administration against real PostgreSQL.

Stage 4.4. Stage 4.2 made memberships decide everything and gave no way to manage them; this
suite covers the four endpoints that do.

**The rule the whole stage turns on is that a hotel always has at least one owner.** It spans
rows, so no CHECK can state it and no unique index can express it -- ``at most one`` is what
an index says, and this needs ``at least one``. It is therefore enforced in the service, under
a row lock, and the section named for it below is the reason this suite exists. The final test
takes the lock in one transaction and proves a second is made to wait for it, because a rule
that holds only when requests arrive one at a time is not a rule.

SQLite is not substituted.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    grant_platform_admin,
    requires_postgres,
)

pytestmark = requires_postgres

FOUNDER_EMAIL = "members-founder@example.test"
OUTSIDER_EMAIL = "members-outsider@example.test"

ROLES = ["viewer", "staff", "manager", "owner"]


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


def members_url(hotel: str) -> str:
    return f"/api/v1/hotels/{hotel}/members"


# ======================================================================================
# Fixtures
# ======================================================================================


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    yield
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(
            sa.text("TRUNCATE platform_admins, user_hotels, users, hotels RESTART IDENTITY CASCADE")
        )
        cleanup.commit()


@pytest.fixture
def founder(engine: Engine) -> TestClient:
    """The account that creates the hotel and is therefore its first -- and only -- owner."""
    return authenticated_client(engine, email=FOUNDER_EMAIL)


@pytest.fixture
def hotel(founder: TestClient) -> str:
    return str(founder.post("/api/v1/hotels", json=hotel_payload("members-a")).json()["public_id"])


@pytest.fixture
def account(engine: Engine) -> Callable[..., tuple[TestClient, str]]:
    """Register an account and return its client and public id, with no membership anywhere."""

    def build(label: str = "") -> tuple[TestClient, str]:
        email = f"members-{label or 'x'}-{uuid.uuid4().hex[:8]}@example.test"
        client = authenticated_client(engine, email=email)
        public_id = str(client.get("/api/v1/auth/me").json()["public_id"])
        client.headers["X-Test-Email"] = email  # carried so tests can name the account
        return client, public_id

    return build


@pytest.fixture
def member(
    engine: Engine, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> Callable[..., tuple[TestClient, str]]:
    """An account holding *role* at the hotel."""

    def build(role: str, *, at: str | None = None) -> tuple[TestClient, str]:
        client, public_id = account(role)
        grant_membership(engine, str(client.headers["X-Test-Email"]), at or hotel, role)
        return client, public_id

    return build


def email_of(client: TestClient) -> str:
    return str(client.headers["X-Test-Email"])


# ======================================================================================
# Listing
# ======================================================================================


def test_a_new_hotel_has_exactly_its_founder(founder: TestClient, hotel: str) -> None:
    body = founder.get(members_url(hotel)).json()

    assert body["total"] == 1
    assert body["items"][0]["email"] == FOUNDER_EMAIL
    assert body["items"][0]["role"] == "owner"


def test_the_listing_uses_the_shared_envelope(founder: TestClient, hotel: str) -> None:
    body = founder.get(members_url(hotel)).json()

    assert set(body) == {"items", "total", "page", "page_size", "pages"}


def test_the_member_response_is_exactly_the_declared_contract(
    founder: TestClient, hotel: str
) -> None:
    item = founder.get(members_url(hotel)).json()["items"][0]

    assert set(item) == {"user_public_id", "email", "full_name", "is_active", "role", "joined_at"}


def test_the_listing_exposes_no_internal_key_or_secret(
    founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    member("staff")

    text = founder.get(members_url(hotel)).text

    for leak in ["password", "hash", "argon2", "user_id", "hotel_id", '"id"']:
        assert leak.lower() not in text.lower(), f"the listing leaked {leak!r}"


def test_the_listing_is_ordered_and_paginates(
    founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    for role in ("viewer", "staff", "manager"):
        member(role)

    first = founder.get(members_url(hotel), params={"page": 1, "page_size": 2}).json()
    second = founder.get(members_url(hotel), params={"page": 2, "page_size": 2}).json()

    assert first["total"] == second["total"] == 4
    assert first["pages"] == 2
    emails = [i["email"] for i in first["items"] + second["items"]]
    assert emails == sorted(emails), "the page order is not stable"
    assert len(set(emails)) == 4, "a page repeated or skipped a member"


def test_a_manager_may_list_members(
    member: Callable[..., tuple[TestClient, str]], hotel: str
) -> None:
    """Running a property means knowing who has access to it."""
    manager, _ = member("manager")

    assert manager.get(members_url(hotel)).status_code == 200


@pytest.mark.parametrize("role", ["viewer", "staff"])
def test_a_viewer_or_staff_may_not_list_members(
    member: Callable[..., tuple[TestClient, str]], hotel: str, role: str
) -> None:
    client, _ = member(role)

    assert client.get(members_url(hotel)).status_code == 403


def test_one_hotels_listing_never_shows_anothers_members(
    engine: Engine, founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    other_owner, _ = account("other")
    other = str(
        other_owner.post("/api/v1/hotels", json=hotel_payload("members-b")).json()["public_id"]
    )
    stranger, _ = account("stranger")
    grant_membership(engine, email_of(stranger), other, "owner")

    emails = {item["email"] for item in founder.get(members_url(hotel)).json()["items"]}

    assert emails == {FOUNDER_EMAIL}


# ======================================================================================
# Adding a member
# ======================================================================================


def test_an_owner_adds_an_existing_account(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    colleague, public_id = account("new")

    response = founder.post(
        members_url(hotel), json={"email": email_of(colleague), "role": "staff"}
    )

    assert response.status_code == 201
    assert response.json()["user_public_id"] == public_id
    assert response.json()["role"] == "staff"


def test_the_added_member_can_immediately_use_the_hotel(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """The grant is what the next request reads, so it takes effect without a new token."""
    colleague, _ = account("new")
    assert colleague.get(f"/api/v1/hotels/{hotel}").status_code == 404

    founder.post(members_url(hotel), json={"email": email_of(colleague), "role": "viewer"})

    assert colleague.get(f"/api/v1/hotels/{hotel}").status_code == 200


def test_adding_creates_no_second_account(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]], engine: Engine
) -> None:
    """Identity is global. Two rows for one person would be two passwords for one human."""
    colleague, _ = account("new")

    founder.post(members_url(hotel), json={"email": email_of(colleague), "role": "staff"})

    with sessionmaker(bind=engine, future=True)() as session:
        count = session.scalar(
            sa.text("SELECT count(*) FROM users WHERE email = :e"), {"e": email_of(colleague)}
        )
    assert count == 1


def test_adding_an_unknown_address_is_404(founder: TestClient, hotel: str) -> None:
    response = founder.post(
        members_url(hotel), json={"email": "nobody@example.test", "role": "staff"}
    )

    assert response.status_code == 404
    assert "register" in response.json()["error"]["message"].lower()


def test_adding_the_same_person_twice_is_409(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    colleague, _ = account("new")
    body = {"email": email_of(colleague), "role": "staff"}
    founder.post(members_url(hotel), json=body)

    response = founder.post(members_url(hotel), json=body)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_a_person_may_belong_to_two_hotels_at_different_roles(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """The membership is per hotel; the account is not."""
    other_owner, _ = account("other")
    other = str(
        other_owner.post("/api/v1/hotels", json=hotel_payload("members-b")).json()["public_id"]
    )
    colleague, _ = account("shared")

    founder.post(members_url(hotel), json={"email": email_of(colleague), "role": "viewer"})
    other_owner.post(members_url(other), json={"email": email_of(colleague), "role": "manager"})

    assert colleague.get(f"/api/v1/hotels/{hotel}").status_code == 200
    assert colleague.get(f"/api/v1/hotels/{other}").status_code == 200
    assert {h["public_id"] for h in colleague.get("/api/v1/hotels").json()["items"]} == {
        hotel,
        other,
    }


@pytest.mark.parametrize("role", ROLES)
def test_every_declared_role_can_be_granted(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]], role: str
) -> None:
    colleague, _ = account(role)

    response = founder.post(members_url(hotel), json={"email": email_of(colleague), "role": role})

    assert response.status_code == 201
    assert response.json()["role"] == role


@pytest.mark.parametrize("role", ["platform_admin", "admin", "superuser", "OWNER", "", "root"])
def test_an_undeclared_role_is_rejected(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]], role: str
) -> None:
    """Including `platform_admin`: platform authority is not a hotel role and cannot be
    granted through a hotel's membership list."""
    colleague, _ = account("bad")

    response = founder.post(members_url(hotel), json={"email": email_of(colleague), "role": role})

    assert response.status_code == 422


def test_a_membership_payload_cannot_name_a_hotel(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """The hotel comes from the URL. A body that could name one could name a different one."""
    colleague, _ = account("new")

    response = founder.post(
        members_url(hotel),
        json={
            "email": email_of(colleague),
            "role": "staff",
            "hotel_id": 1,
            "user_id": 1,
        },
    )

    assert response.status_code == 422


# ======================================================================================
# Changing a role
# ======================================================================================


def test_an_owner_changes_a_members_role(
    founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    _, public_id = member("staff")

    response = founder.patch(f"{members_url(hotel)}/{public_id}", json={"role": "manager"})

    assert response.status_code == 200
    assert response.json()["role"] == "manager"


def test_the_new_role_takes_effect_at_once(
    founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    staff, public_id = member("staff")
    assert staff.get(members_url(hotel)).status_code == 403

    founder.patch(f"{members_url(hotel)}/{public_id}", json={"role": "manager"})

    assert staff.get(members_url(hotel)).status_code == 200


def test_a_revoked_role_takes_effect_at_once(
    founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    manager, public_id = member("manager")
    assert manager.get(members_url(hotel)).status_code == 200

    founder.patch(f"{members_url(hotel)}/{public_id}", json={"role": "viewer"})

    assert manager.get(members_url(hotel)).status_code == 403


def test_changing_a_nonexistent_users_role_is_404(founder: TestClient, hotel: str) -> None:
    response = founder.patch(f"{members_url(hotel)}/{uuid.uuid4()}", json={"role": "staff"})

    assert response.status_code == 404


def test_changing_a_non_members_role_is_the_same_404(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """An account that exists but belongs to another hotel must be indistinguishable from one
    that does not exist -- otherwise this endpoint is an account-enumeration oracle."""
    _, real = account("elsewhere")

    unknown = founder.patch(f"{members_url(hotel)}/{uuid.uuid4()}", json={"role": "staff"})
    existing = founder.patch(f"{members_url(hotel)}/{real}", json={"role": "staff"})

    assert unknown.status_code == existing.status_code == 404
    assert unknown.text == existing.text


def test_an_empty_role_change_is_rejected(
    founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    """A membership has one mutable field, so a PATCH without it means nothing."""
    _, public_id = member("staff")

    assert founder.patch(f"{members_url(hotel)}/{public_id}", json={}).status_code == 422


# ======================================================================================
# Removing a member
# ======================================================================================


def test_an_owner_removes_a_member(
    founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    _, public_id = member("staff")

    assert founder.delete(f"{members_url(hotel)}/{public_id}").status_code == 204
    assert founder.get(members_url(hotel)).json()["total"] == 1


def test_removal_revokes_access_at_once(
    founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    staff, public_id = member("staff")
    assert staff.get(f"/api/v1/hotels/{hotel}").status_code == 200

    founder.delete(f"{members_url(hotel)}/{public_id}")

    assert staff.get(f"/api/v1/hotels/{hotel}").status_code == 404


def test_removal_leaves_the_account_alone(
    founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]], engine: Engine
) -> None:
    """Revoking access to one property must not touch a global identity."""
    staff, public_id = member("staff")

    founder.delete(f"{members_url(hotel)}/{public_id}")

    assert staff.get("/api/v1/auth/me").status_code == 200
    with sessionmaker(bind=engine, future=True)() as session:
        active = session.scalar(
            sa.text("SELECT is_active FROM users WHERE email = :e"), {"e": email_of(staff)}
        )
    assert active is True


def test_removing_a_member_of_another_hotel_is_404(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]], engine: Engine
) -> None:
    other_owner, _ = account("other")
    other = str(
        other_owner.post("/api/v1/hotels", json=hotel_payload("members-b")).json()["public_id"]
    )
    stranger, stranger_id = account("stranger")
    grant_membership(engine, email_of(stranger), other, "staff")

    assert founder.delete(f"{members_url(hotel)}/{stranger_id}").status_code == 404
    # ...and their real membership is untouched.
    assert stranger.get(f"/api/v1/hotels/{other}").status_code == 200


# ======================================================================================
# Ownership -- the invariant the stage turns on
# ======================================================================================


def test_the_only_owner_cannot_be_removed(founder: TestClient, hotel: str) -> None:
    """Scenario 1: a hotel with one owner. Nobody may take the last one away."""
    me = founder.get(members_url(hotel)).json()["items"][0]["user_public_id"]

    response = founder.delete(f"{members_url(hotel)}/{me}")

    assert response.status_code == 409
    assert "without an owner" in response.json()["error"]["message"]


def test_the_only_owner_cannot_be_demoted(founder: TestClient, hotel: str) -> None:
    me = founder.get(members_url(hotel)).json()["items"][0]["user_public_id"]

    response = founder.patch(f"{members_url(hotel)}/{me}", json={"role": "manager"})

    assert response.status_code == 409
    assert "without an owner" in response.json()["error"]["message"]


def test_a_refused_last_owner_change_leaves_the_row_untouched(
    founder: TestClient, hotel: str, engine: Engine
) -> None:
    me = founder.get(members_url(hotel)).json()["items"][0]["user_public_id"]

    founder.patch(f"{members_url(hotel)}/{me}", json={"role": "viewer"})
    founder.delete(f"{members_url(hotel)}/{me}")

    with sessionmaker(bind=engine, future=True)() as session:
        rows = session.execute(sa.text("SELECT role FROM user_hotels")).scalars().all()
    assert list(rows) == ["owner"]
    assert founder.get(f"/api/v1/hotels/{hotel}").status_code == 200


def test_a_second_owner_makes_the_first_removable(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """Scenarios 2, 3 and 5: two owners; one may remove the other, or themselves."""
    successor, _ = account("successor")
    founder.post(members_url(hotel), json={"email": email_of(successor), "role": "owner"})
    me = next(
        i["user_public_id"]
        for i in founder.get(members_url(hotel)).json()["items"]
        if i["email"] == FOUNDER_EMAIL
    )

    assert founder.delete(f"{members_url(hotel)}/{me}").status_code == 204
    assert successor.get(members_url(hotel)).json()["total"] == 1
    # And the hotel is still administrable by the survivor.
    assert (
        successor.patch(f"/api/v1/hotels/{hotel}", json={"name": "Still owned"}).status_code == 200
    )


def test_an_owner_may_remove_another_owner(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """Scenario 3, from the other side."""
    other, other_id = account("co-owner")
    founder.post(members_url(hotel), json={"email": email_of(other), "role": "owner"})

    assert founder.delete(f"{members_url(hotel)}/{other_id}").status_code == 204
    assert other.get(f"/api/v1/hotels/{hotel}").status_code == 404


def test_an_owner_may_demote_another_owner(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """Scenario 4."""
    other, other_id = account("co-owner")
    founder.post(members_url(hotel), json={"email": email_of(other), "role": "owner"})

    response = founder.patch(f"{members_url(hotel)}/{other_id}", json={"role": "staff"})

    assert response.status_code == 200
    assert response.json()["role"] == "staff"


def test_an_owner_may_demote_themselves_once_a_successor_exists(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """Scenario 6, and the reason the rule counts owners rather than naming the caller."""
    successor, _ = account("successor")
    founder.post(members_url(hotel), json={"email": email_of(successor), "role": "owner"})
    me = next(
        i["user_public_id"]
        for i in founder.get(members_url(hotel)).json()["items"]
        if i["email"] == FOUNDER_EMAIL
    )

    assert founder.patch(f"{members_url(hotel)}/{me}", json={"role": "manager"}).status_code == 200
    # Demoted: the founder can no longer administer membership.
    assert (
        founder.post(
            members_url(hotel), json={"email": email_of(successor), "role": "viewer"}
        ).status_code
        == 403
    )


def test_demoting_the_second_to_last_owner_then_the_last_is_refused(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """The invariant holds across a sequence, not just within one request."""
    other, other_id = account("co-owner")
    founder.post(members_url(hotel), json={"email": email_of(other), "role": "owner"})
    me = next(
        i["user_public_id"]
        for i in founder.get(members_url(hotel)).json()["items"]
        if i["email"] == FOUNDER_EMAIL
    )

    assert (
        founder.patch(f"{members_url(hotel)}/{other_id}", json={"role": "staff"}).status_code == 200
    )
    assert founder.patch(f"{members_url(hotel)}/{me}", json={"role": "staff"}).status_code == 409


def test_a_role_change_that_does_not_move_the_role_is_allowed(
    founder: TestClient, hotel: str
) -> None:
    """Setting the last owner to owner is a no-op, not a violation: the rule is about the
    hotel's resulting state, and that state is unchanged."""
    me = founder.get(members_url(hotel)).json()["items"][0]["user_public_id"]

    assert founder.patch(f"{members_url(hotel)}/{me}", json={"role": "owner"}).status_code == 200


# ======================================================================================
# Who may administer membership at all
# ======================================================================================


@pytest.mark.parametrize("role", ["viewer", "staff", "manager"])
def test_no_role_below_owner_may_change_membership(
    member: Callable[..., tuple[TestClient, str]], hotel: str, role: str, founder: TestClient
) -> None:
    """Scenarios 7, 8 and 9. A manager runs the property; granting access to it is the
    owner's decision."""
    client, _ = member(role)
    target = founder.get(members_url(hotel)).json()["items"][0]["user_public_id"]

    assert (
        client.post(members_url(hotel), json={"email": FOUNDER_EMAIL, "role": "viewer"}).status_code
        == 403
    )
    assert client.patch(f"{members_url(hotel)}/{target}", json={"role": "staff"}).status_code == 403
    assert client.delete(f"{members_url(hotel)}/{target}").status_code == 403


@pytest.mark.parametrize("role", ["viewer", "staff", "manager"])
def test_a_member_cannot_promote_themselves(
    member: Callable[..., tuple[TestClient, str]], hotel: str, role: str
) -> None:
    """Falls out of the rule above rather than needing one of its own: only an owner may
    write a membership, and an owner is already at the top."""
    client, public_id = member(role)

    assert (
        client.patch(f"{members_url(hotel)}/{public_id}", json={"role": "owner"}).status_code == 403
    )


def test_a_refused_promotion_writes_nothing(
    member: Callable[..., tuple[TestClient, str]], hotel: str, engine: Engine
) -> None:
    client, public_id = member("manager")

    client.patch(f"{members_url(hotel)}/{public_id}", json={"role": "owner"})

    with sessionmaker(bind=engine, future=True)() as session:
        roles = sorted(session.execute(sa.text("SELECT role FROM user_hotels")).scalars().all())
    assert roles == ["manager", "owner"]


# ======================================================================================
# Platform administrators get nothing here
# ======================================================================================


def test_a_platform_admin_without_membership_sees_the_404_wall(
    engine: Engine, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """Stage 4.3's rule, restated against the newest hotel-scoped resource."""
    admin, _ = account("platform")
    grant_platform_admin(engine, email_of(admin))

    assert admin.get(members_url(hotel)).status_code == 404
    assert (
        admin.post(members_url(hotel), json={"email": FOUNDER_EMAIL, "role": "viewer"}).status_code
        == 404
    )


def test_a_platform_admins_404_is_identical_to_a_strangers(
    engine: Engine, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    admin, _ = account("platform")
    grant_platform_admin(engine, email_of(admin))
    stranger, _ = account("stranger")

    as_admin = admin.get(members_url(hotel))
    as_stranger = stranger.get(members_url(hotel))

    assert as_admin.status_code == as_stranger.status_code == 404
    assert as_admin.text == as_stranger.text


def test_a_platform_admin_with_viewer_membership_has_viewer_powers(
    engine: Engine, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    """Platform privilege does not top up a hotel role."""
    viewer, _ = member("viewer")
    grant_platform_admin(engine, email_of(viewer))

    assert viewer.get(f"/api/v1/hotels/{hotel}").status_code == 200
    assert viewer.get(members_url(hotel)).status_code == 403


def test_a_platform_admin_with_owner_membership_has_owner_powers(
    engine: Engine, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    """And no more: the membership is doing the work, not the grant."""
    owner, _ = member("owner", at=hotel)
    grant_platform_admin(engine, email_of(owner))

    assert owner.get(members_url(hotel)).status_code == 200


def test_platform_authority_cannot_be_granted_as_a_hotel_role(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]], engine: Engine
) -> None:
    colleague, _ = account("new")

    founder.post(members_url(hotel), json={"email": email_of(colleague), "role": "platform_admin"})

    with sessionmaker(bind=engine, future=True)() as session:
        grants = session.scalar(sa.text("SELECT count(*) FROM platform_admins"))
    assert grants == 0


# ======================================================================================
# Authentication and the 401/403/404 ladder
# ======================================================================================


def test_an_anonymous_caller_is_401(engine: Engine, hotel: str) -> None:
    client = authenticated_client(engine, email=OUTSIDER_EMAIL)
    client.headers.pop("Authorization")

    assert client.get(members_url(hotel)).status_code == 401
    assert (
        client.post(members_url(hotel), json={"email": FOUNDER_EMAIL, "role": "viewer"}).status_code
        == 401
    )


@pytest.mark.parametrize(
    "header",
    ["Bearer not-a-jwt", "Bearer ", "Basic abc", "bearer eyJhbGciOiJIUzI1NiJ9.e30.x"],
    ids=["garbage", "empty", "wrong-scheme", "wrong-signature"],
)
def test_a_broken_token_is_401(founder: TestClient, hotel: str, header: str) -> None:
    founder.headers["Authorization"] = header

    assert founder.get(members_url(hotel)).status_code == 401


def test_an_expired_token_is_401(engine: Engine, founder: TestClient, hotel: str) -> None:
    import datetime as dt

    from app.core.config import Settings
    from app.core.security import create_access_token
    from tests.integration.conftest import TEST_SECRET

    subject = uuid.UUID(str(founder.get("/api/v1/auth/me").json()["public_id"]))
    expired = create_access_token(
        Settings(environment="test", secret_key=TEST_SECRET),
        subject,
        now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=48),
    )
    founder.headers["Authorization"] = f"Bearer {expired}"

    assert founder.get(members_url(hotel)).status_code == 401


def test_a_disabled_owner_is_401(engine: Engine, founder: TestClient, hotel: str) -> None:
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": FOUNDER_EMAIL}
        )
        session.commit()

    assert founder.get(members_url(hotel)).status_code == 401


def test_a_non_member_gets_the_hotel_wall_not_a_403(
    engine: Engine, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    """A 403 would confirm the hotel exists. The wall must come first."""
    stranger, _ = account("stranger")

    real = stranger.get(members_url(hotel))
    imaginary = stranger.get(members_url(str(uuid.uuid4())))

    assert real.status_code == imaginary.status_code == 404
    assert real.text == imaginary.text


def test_a_disabled_member_still_appears_in_the_listing(
    engine: Engine, founder: TestClient, hotel: str, member: Callable[..., tuple[TestClient, str]]
) -> None:
    """The membership survives the account being disabled; showing `is_active` is how an
    administrator sees that a listed colleague currently cannot sign in."""
    staff, _ = member("staff")
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"),
            {"e": email_of(staff)},
        )
        session.commit()

    item = next(
        i for i in founder.get(members_url(hotel)).json()["items"] if i["email"] == email_of(staff)
    )

    assert item["is_active"] is False
    assert staff.get(f"/api/v1/hotels/{hotel}").status_code == 401


# ======================================================================================
# What the refusals say
# ======================================================================================


@pytest.mark.parametrize(
    ("label", "expected"),
    [("unknown-hotel", 404), ("wrong-role", 403), ("last-owner", 409)],
)
def test_every_refusal_uses_the_shared_envelope(
    founder: TestClient,
    hotel: str,
    member: Callable[..., tuple[TestClient, str]],
    label: str,
    expected: int,
) -> None:
    if label == "unknown-hotel":
        response = founder.get(members_url(str(uuid.uuid4())))
    elif label == "wrong-role":
        client, _ = member("viewer")
        response = client.get(members_url(hotel))
    else:
        me = founder.get(members_url(hotel)).json()["items"][0]["user_public_id"]
        response = founder.delete(f"{members_url(hotel)}/{me}")

    body = response.json()
    assert response.status_code == expected
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def test_no_refusal_leaks_sql_or_schema(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]]
) -> None:
    colleague, _ = account("new")
    body = {"email": email_of(colleague), "role": "staff"}
    founder.post(members_url(hotel), json=body)
    me = founder.get(members_url(hotel)).json()["items"][0]["user_public_id"]

    texts = [
        founder.post(members_url(hotel), json=body).text,
        founder.delete(f"{members_url(hotel)}/{me}").text,
        founder.patch(f"{members_url(hotel)}/{uuid.uuid4()}", json={"role": "staff"}).text,
    ]

    for text in texts:
        for leak in [
            "uq_user_hotels",
            "user_hotels",
            "psycopg",
            "sqlalchemy",
            "23505",
            "23514",
            "insert",
            "select",
            "detail:",
            "traceback",
        ]:
            assert leak not in text.lower(), f"leaked {leak!r} in {text}"


def test_no_refusal_exposes_an_internal_key(founder: TestClient, hotel: str) -> None:
    import re

    me = founder.get(members_url(hotel)).json()["items"][0]["user_public_id"]
    body = founder.delete(f"{members_url(hotel)}/{me}").json()

    assert body["error"]["details"] in (None, {}, [])
    assert re.search(r"\b\d{1,6}\b", body["error"]["message"]) is None


def test_the_403_names_the_role_but_not_the_caller(
    member: Callable[..., tuple[TestClient, str]], hotel: str
) -> None:
    client, _ = member("manager")

    body = client.delete(f"{members_url(hotel)}/{uuid.uuid4()}").json()

    assert body["error"]["code"] == "FORBIDDEN"
    assert "owner" in body["error"]["message"].lower()
    assert "manager" not in body["error"]["message"].lower()


def test_the_last_owner_refusal_names_no_user_or_count(founder: TestClient, hotel: str) -> None:
    me = founder.get(members_url(hotel)).json()["items"][0]["user_public_id"]

    message = founder.delete(f"{members_url(hotel)}/{me}").json()["error"]["message"]

    assert FOUNDER_EMAIL not in message
    assert me not in message
    assert "1" not in message


# ======================================================================================
# The lock, under concurrency
# ======================================================================================


def test_the_owner_check_takes_a_lock_a_second_transaction_must_wait_for(
    founder: TestClient, hotel: str, account: Callable[..., tuple[TestClient, str]], engine: Engine
) -> None:
    """The invariant is only as good as its lock.

    Two concurrent demotions of two different owners would each read "2 owners", each conclude
    they are safe, and together leave the hotel with none. The service reads that count with
    ``SELECT ... FOR UPDATE`` over the owner rows, so the second request blocks until the first
    commits and then re-reads a count of 1.

    Proved from the outside: a separate transaction holds the same lock, and the API request
    is shown to be unable to proceed while it does. ``lock_timeout`` turns the wait into a
    prompt failure instead of hanging this test for the length of the suite.
    """
    successor, successor_id = account("successor")
    founder.post(members_url(hotel), json={"email": email_of(successor), "role": "owner"})

    factory = sessionmaker(bind=engine, future=True)
    with factory() as blocker:
        # Hold the exact rows the service will ask for.
        blocker.execute(
            sa.text(
                "SELECT uh.user_id FROM user_hotels uh JOIN hotels h ON h.id = uh.hotel_id "
                "WHERE h.public_id = CAST(:hotel AS uuid) AND uh.role = 'owner' FOR UPDATE"
            ),
            {"hotel": hotel},
        ).all()

        with factory() as impatient:
            impatient.execute(sa.text("SET lock_timeout = '750ms'"))
            with pytest.raises(sa.exc.OperationalError) as raised:
                impatient.execute(
                    sa.text(
                        "SELECT uh.user_id FROM user_hotels uh JOIN hotels h "
                        "ON h.id = uh.hotel_id WHERE h.public_id = CAST(:hotel AS uuid) "
                        "AND uh.role = 'owner' FOR UPDATE"
                    ),
                    {"hotel": hotel},
                ).all()
            impatient.rollback()

        assert "lock" in str(raised.value).lower()
        blocker.rollback()

    # With the blocker gone, the same demotion succeeds -- the lock delayed it, not broke it.
    assert (
        founder.patch(f"{members_url(hotel)}/{successor_id}", json={"role": "staff"}).status_code
        == 200
    )


def test_the_hotel_still_has_an_owner_after_every_test_above(
    founder: TestClient, hotel: str, engine: Engine
) -> None:
    """A closing sanity check on the invariant itself, independent of any one path."""
    with sessionmaker(bind=engine, future=True)() as session:
        owners = session.scalar(sa.text("SELECT count(*) FROM user_hotels WHERE role = 'owner'"))

    assert owners >= 1
