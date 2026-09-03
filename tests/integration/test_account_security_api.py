"""Account security against real PostgreSQL.

Stage 4.5.1 (changing your own password) and 4.5.2 (revoking what the change replaced).

Three properties carry the weight here. **The token says whose password changes** -- the body
names no account, and cannot be made to. **A valid token is not enough** -- the current
password is required, so a stolen token buys thirty minutes rather than the account. And from
4.5.2, **the change revokes every token minted before it**, including the one that made the
request, which is why a replacement comes back in the response.

The two tests that recorded 4.5.1's deliberate silence on revocation have inverted rather than
disappeared: they now assert the old token is refused and that a fresh one is issued.

Hashes are compared and counted, never printed: an assertion that renders a digest into a
failure message puts a credential-adjacent value into CI output.

SQLite is not substituted.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.rate_limit import FixedWindowRateLimiter
from tests.integration.conftest import (
    TEST_PASSWORD,
    authenticated_client,
    create_test_app,
    requires_postgres,
)

pytestmark = requires_postgres

CHANGE_URL = "/api/v1/auth/change-password"
LOGIN_URL = "/api/v1/auth/login"

OWNER_EMAIL = "account-security@example.test"
OTHER_EMAIL = "account-security-other@example.test"

NEW_PASSWORD = "a-replacement-password-long-enough"

#: Below the twelve-character floor. Chosen so it is not a SUBSTRING of any pydantic error
#: type -- an earlier draft used "short", which matched `string_too_short` and made a
#: leak-check fail against a value that was never actually echoed.
TOO_SHORT_PASSWORD = "nope-9"


def hash_of(engine: Engine, email: str) -> str:
    """The stored digest, for comparison only. Never asserted INTO a message."""
    with sessionmaker(bind=engine, future=True)() as session:
        digest = session.scalar(
            sa.text("SELECT password_hash FROM users WHERE email = :e"), {"e": email}
        )
    assert digest is not None, "the account under test does not exist"
    return str(digest)


def _public_id_of(engine: Engine, email: str) -> uuid.UUID:
    """The account's public id, straight from the database.

    Read here rather than from `/auth/me` because several tests below need it AFTER the
    token that could ask has already been revoked.
    """
    with sessionmaker(bind=engine, future=True)() as session:
        value = session.scalar(
            sa.text("SELECT public_id FROM users WHERE email = :e"), {"e": email}
        )
    assert value is not None
    return uuid.UUID(str(value))


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    yield
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(
            sa.text("TRUNCATE platform_admins, user_hotels, users, hotels RESTART IDENTITY CASCADE")
        )
        cleanup.commit()


@pytest.fixture
def user(engine: Engine) -> TestClient:
    return authenticated_client(engine, email=OWNER_EMAIL)


# ======================================================================================
# The happy path
# ======================================================================================


def test_an_authenticated_user_changes_their_own_password(user: TestClient) -> None:
    response = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 200
    assert response.json()["access_token"]


def test_the_stored_digest_actually_changes(user: TestClient, engine: Engine) -> None:
    before = hash_of(engine, OWNER_EMAIL)

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert hash_of(engine, OWNER_EMAIL) != before, "the digest was not replaced"


def test_the_new_password_authenticates(user: TestClient) -> None:
    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    response = user.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": NEW_PASSWORD})

    assert response.status_code == 200
    assert response.json()["access_token"]


def test_the_old_password_no_longer_authenticates(user: TestClient) -> None:
    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    response = user.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD})

    assert response.status_code == 401


def test_the_plaintext_is_stored_nowhere(user: TestClient, engine: Engine) -> None:
    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    with sessionmaker(bind=engine, future=True)() as session:
        row = (
            session.execute(sa.text("SELECT * FROM users WHERE email = :e"), {"e": OWNER_EMAIL})
            .mappings()
            .one()
        )

    for column, value in row.items():
        assert NEW_PASSWORD not in str(value), f"the new password reached {column}"
        assert TEST_PASSWORD not in str(value), f"the old password reached {column}"


def test_the_password_can_be_changed_twice(user: TestClient) -> None:
    """The second change uses the token the FIRST one returned.

    It has to: the first change revoked the token this client started with, which is as much
    the behaviour under test as the second change is.
    """
    second = "yet-another-sufficiently-long-password"
    first = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    ).json()
    user.headers["Authorization"] = f"Bearer {first['access_token']}"

    response = user.post(
        CHANGE_URL, json={"current_password": NEW_PASSWORD, "new_password": second}
    )

    assert response.status_code == 200
    assert user.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": second}).status_code == 200


def test_changing_a_password_needs_no_hotel(user: TestClient, engine: Engine) -> None:
    """A password belongs to the account, which is global.

    The user under test is a member of nothing at all -- if this required a membership, a
    person who belongs to no property could never change their own credential.
    """
    with sessionmaker(bind=engine, future=True)() as session:
        memberships = session.scalar(sa.text("SELECT count(*) FROM user_hotels"))
    assert memberships == 0

    response = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 200


# ======================================================================================
# The current password is required
# ======================================================================================


def test_a_wrong_current_password_is_the_same_401_login_gives(user: TestClient) -> None:
    response = user.post(
        CHANGE_URL,
        json={"current_password": "not-the-right-password", "new_password": NEW_PASSWORD},
    )
    login_failure = user.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": "wrong-one"})

    assert response.status_code == 401
    assert response.json() == login_failure.json(), "the two rejections are distinguishable"


def test_a_wrong_current_password_changes_nothing(user: TestClient, engine: Engine) -> None:
    before = hash_of(engine, OWNER_EMAIL)

    user.post(
        CHANGE_URL,
        json={"current_password": "not-the-right-password", "new_password": NEW_PASSWORD},
    )

    assert hash_of(engine, OWNER_EMAIL) == before, "a refused change still wrote a digest"
    assert (
        user.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD}).status_code
        == 200
    )


def test_the_new_password_does_not_work_after_a_refused_change(user: TestClient) -> None:
    user.post(
        CHANGE_URL,
        json={"current_password": "not-the-right-password", "new_password": NEW_PASSWORD},
    )

    assert (
        user.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": NEW_PASSWORD}).status_code
        == 401
    )


def test_a_short_wrong_current_password_is_401_not_422(user: TestClient) -> None:
    """Following LoginRequest: a value being VERIFIED carries no length floor, because
    rejecting a short guess tells an attacker it was too short to be the real one."""
    response = user.post(CHANGE_URL, json={"current_password": "x", "new_password": NEW_PASSWORD})

    assert response.status_code == 401


def test_another_users_password_is_not_accepted_as_the_current_one(
    engine: Engine, user: TestClient
) -> None:
    """Guards against verifying the supplied password against the wrong row."""
    other_password = "the-other-accounts-password"
    other = authenticated_client(engine, email=OTHER_EMAIL)
    other.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": other_password})

    response = user.post(
        CHANGE_URL, json={"current_password": other_password, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 401


# ======================================================================================
# The token decides whose password changes
# ======================================================================================


@pytest.mark.parametrize(
    "extra",
    [
        {"email": OTHER_EMAIL},
        {"user_id": 1},
        {"public_id": "00000000-0000-0000-0000-000000000001"},
        {"user_public_id": "00000000-0000-0000-0000-000000000001"},
        {"hotel_id": 1},
        {"role": "owner"},
        {"is_active": False},
    ],
    ids=lambda e: next(iter(e)),
)
def test_the_body_cannot_name_an_account_or_a_privilege(
    user: TestClient, extra: dict[str, object]
) -> None:
    """`extra="forbid"`: a field the service would ignore is still refused, because the next
    handler to read the body should not find one waiting there."""
    response = user.post(
        CHANGE_URL,
        json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD, **extra},
    )

    assert response.status_code == 422


def test_a_rejected_body_changes_no_password(user: TestClient, engine: Engine) -> None:
    before = hash_of(engine, OWNER_EMAIL)

    user.post(
        CHANGE_URL,
        json={
            "current_password": TEST_PASSWORD,
            "new_password": NEW_PASSWORD,
            "email": OTHER_EMAIL,
        },
    )

    assert hash_of(engine, OWNER_EMAIL) == before


def test_one_users_change_never_touches_another(engine: Engine, user: TestClient) -> None:
    other = authenticated_client(engine, email=OTHER_EMAIL)
    other_before = hash_of(engine, OTHER_EMAIL)

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert hash_of(engine, OTHER_EMAIL) == other_before
    assert (
        other.post(LOGIN_URL, json={"email": OTHER_EMAIL, "password": TEST_PASSWORD}).status_code
        == 200
    )


# ======================================================================================
# Authentication is required first
# ======================================================================================


def test_an_anonymous_request_is_401(engine: Engine) -> None:
    client = authenticated_client(engine, email=OWNER_EMAIL)
    client.headers.pop("Authorization")

    response = client.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 401


def test_an_anonymous_request_changes_nothing(engine: Engine) -> None:
    client = authenticated_client(engine, email=OWNER_EMAIL)
    before = hash_of(engine, OWNER_EMAIL)
    client.headers.pop("Authorization")

    client.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert hash_of(engine, OWNER_EMAIL) == before


@pytest.mark.parametrize(
    "header",
    ["Bearer not-a-jwt", "Bearer ", "Basic abc", "bearer eyJhbGciOiJIUzI1NiJ9.e30.x"],
    ids=["garbage", "empty", "wrong-scheme", "wrong-signature"],
)
def test_a_broken_token_is_401(user: TestClient, header: str) -> None:
    user.headers["Authorization"] = header

    response = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 401


def test_an_expired_token_is_401(engine: Engine, user: TestClient) -> None:
    import datetime as dt

    from app.core.config import Settings
    from app.core.security import create_access_token
    from tests.integration.conftest import TEST_SECRET

    subject = uuid.UUID(str(user.get("/api/v1/auth/me").json()["public_id"]))
    expired = create_access_token(
        Settings(environment="test", secret_key=TEST_SECRET),
        subject,
        now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=48),
    )
    user.headers["Authorization"] = f"Bearer {expired}"

    response = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 401


def test_a_disabled_account_is_401_and_indistinguishable(engine: Engine, user: TestClient) -> None:
    """Authentication fails before the current password is ever examined, so a disabled
    caller cannot be told apart from one who mistyped."""
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": OWNER_EMAIL}
        )
        session.commit()

    response = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 401


def test_a_disabled_account_cannot_change_its_password(engine: Engine, user: TestClient) -> None:
    before = hash_of(engine, OWNER_EMAIL)
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": OWNER_EMAIL}
        )
        session.commit()

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert hash_of(engine, OWNER_EMAIL) == before


# ======================================================================================
# The new password is validated by the existing policy
# ======================================================================================


@pytest.mark.parametrize(
    "new_password",
    ["", TOO_SHORT_PASSWORD, "elevenchars"],
    ids=["empty", "short", "one-below-the-floor"],
)
def test_a_new_password_below_the_floor_is_rejected(user: TestClient, new_password: str) -> None:
    """The floor is registration's, reused rather than reinvented: twelve characters."""
    response = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": new_password}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_rejected_new_password_changes_nothing(user: TestClient, engine: Engine) -> None:
    before = hash_of(engine, OWNER_EMAIL)

    user.post(
        CHANGE_URL,
        json={"current_password": TEST_PASSWORD, "new_password": TOO_SHORT_PASSWORD},
    )

    assert hash_of(engine, OWNER_EMAIL) == before


@pytest.mark.parametrize(
    "body",
    [
        {"new_password": NEW_PASSWORD},
        {"current_password": TEST_PASSWORD},
        {},
    ],
    ids=["no-current", "no-new", "empty"],
)
def test_both_fields_are_required(user: TestClient, body: dict[str, object]) -> None:
    assert user.post(CHANGE_URL, json=body).status_code == 422


# ======================================================================================
# Nothing sensitive comes back
# ======================================================================================


def test_the_success_response_is_exactly_the_login_token_shape(user: TestClient) -> None:
    """The same TokenResponse login returns -- not a second token format invented here."""
    body = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    ).json()
    login = user.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": NEW_PASSWORD}).json()

    assert set(body) == set(login) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"


@pytest.mark.parametrize(
    ("label", "body", "rejected"),
    [
        (
            "wrong-current",
            {"current_password": "wrong-password-x", "new_password": NEW_PASSWORD},
            "wrong-password-x",
        ),
        (
            "short-new",
            {"current_password": TEST_PASSWORD, "new_password": TOO_SHORT_PASSWORD},
            TOO_SHORT_PASSWORD,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_no_refusal_echoes_a_password_or_a_digest(
    user: TestClient, engine: Engine, label: str, body: dict[str, object], rejected: str
) -> None:
    """Neither the value that was refused nor the one that is stored comes back."""
    digest = hash_of(engine, OWNER_EMAIL)

    text = user.post(CHANGE_URL, json=body).text

    assert rejected not in text, f"{label} echoed the value it refused"
    assert TEST_PASSWORD not in text, f"{label} echoed the current password"
    assert NEW_PASSWORD not in text, f"{label} echoed the new password"
    assert digest not in text, f"{label} echoed the stored digest"
    assert "$argon2" not in text, f"{label} echoed a hash"


def test_no_refusal_leaks_sql_schema_or_internals(user: TestClient) -> None:
    texts = [
        user.post(
            CHANGE_URL, json={"current_password": "wrong-password-x", "new_password": NEW_PASSWORD}
        ).text,
        user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": "s"}).text,
    ]

    for text in texts:
        for leak in [
            "password_hash",
            "uq_users",
            "users",
            "psycopg",
            "sqlalchemy",
            "23505",
            "sqlstate",
            "insert",
            "update",
            "select",
            "detail:",
            "traceback",
            "argon2",
        ]:
            assert leak not in text.lower(), f"leaked {leak!r}"


def test_no_response_exposes_an_internal_key(user: TestClient) -> None:
    import re

    body = user.post(
        CHANGE_URL, json={"current_password": "wrong-password-x", "new_password": NEW_PASSWORD}
    ).json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert re.search(r"\b\d{1,6}\b", body["error"]["message"]) is None


def test_the_validation_error_names_the_field_not_the_value(user: TestClient) -> None:
    body = user.post(
        CHANGE_URL,
        json={"current_password": TEST_PASSWORD, "new_password": TOO_SHORT_PASSWORD},
    ).json()

    details = body["error"]["details"]
    assert any("new_password" in str(d.get("location", "")) for d in details)
    assert TOO_SHORT_PASSWORD not in str(details)


def test_changing_a_password_leaks_nothing_into_the_logs(
    user: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        fresh = user.post(
            CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
        ).json()
        # The first change revoked the token this client held; carry the replacement so the
        # second call reaches the password check rather than stopping at 401.
        user.headers["Authorization"] = f"Bearer {fresh['access_token']}"
        user.post(
            CHANGE_URL, json={"current_password": "wrong-password-x", "new_password": NEW_PASSWORD}
        )

    captured = "\n".join(
        record.getMessage() + str(record.exc_info or "") for record in caplog.records
    )
    for secret in (TEST_PASSWORD, NEW_PASSWORD, "wrong-password-x", "$argon2"):
        assert secret not in captured, f"leaked {secret!r} into the logs"


# ======================================================================================
# What this stage deliberately does NOT do
# ======================================================================================


def test_a_token_minted_before_the_change_is_refused(user: TestClient) -> None:
    """Stage 4.5.1 recorded that this token survived. Stage 4.5.2 is where that inverts.

    The token this request was made with predates the change, so it is refused from the very
    next request -- the point of the whole stage.
    """
    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert user.get("/api/v1/auth/me").status_code == 401


def test_the_change_issues_a_fresh_token(user: TestClient) -> None:
    """It has to. The request that changes the password revokes its own credential, so
    without a replacement the caller would be signed out by succeeding."""
    response = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    )

    assert response.status_code == 200
    assert response.json()["access_token"]


# ======================================================================================
# Stage 4.5.2 -- what the change revokes
# ======================================================================================


def changed_at(engine: Engine, email: str) -> dt.datetime:
    """The revocation boundary, read straight from the column."""
    with sessionmaker(bind=engine, future=True)() as session:
        value = session.scalar(
            sa.text("SELECT password_changed_at FROM users WHERE email = :e"), {"e": email}
        )
    assert value is not None
    return value


def claims_of(token: str) -> dict[str, object]:
    """The token's payload, decoded WITHOUT verification -- this is inspecting a value, not
    trusting one."""
    import base64
    import json

    payload = token.split(".")[1]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    return dict(json.loads(decoded))


def test_the_fresh_token_works_immediately(user: TestClient) -> None:
    """The whole endpoint is useless if it hands back a token born revoked."""
    fresh = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    ).json()["access_token"]
    user.headers["Authorization"] = f"Bearer {fresh}"

    assert user.get("/api/v1/auth/me").status_code == 200


def test_the_old_token_gets_the_ordinary_authentication_failure(
    engine: Engine, user: TestClient
) -> None:
    """A revoked token must be indistinguishable from a malformed one. Anything else tells a
    holder that this account's password just changed."""
    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    revoked = user.get("/api/v1/auth/me")
    user.headers["Authorization"] = "Bearer not-a-jwt"
    garbage = user.get("/api/v1/auth/me")

    assert revoked.status_code == garbage.status_code == 401
    assert revoked.json() == garbage.json(), "a revoked token is distinguishable from a bad one"


def test_the_old_token_cannot_reach_a_hotel_resource(engine: Engine, user: TestClient) -> None:
    """Revocation is global account state, not a hotel concern -- so it applies to hotel-scoped
    routes exactly as it applies to /auth/me."""
    hotel = str(
        user.post(
            "/api/v1/hotels",
            json={
                "slug": "revocation-a",
                "name": "Hotel revocation-a",
                "address_line1": "1 Dionysiou Areopagitou",
                "city": "Athens",
                "country_code": "GR",
                "timezone": "Europe/Athens",
                "currency": "EUR",
            },
        ).json()["public_id"]
    )
    assert user.get(f"/api/v1/hotels/{hotel}").status_code == 200

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert user.get(f"/api/v1/hotels/{hotel}").status_code == 401
    assert user.get("/api/v1/hotels").status_code == 401


def test_a_token_minted_after_the_change_works(user: TestClient) -> None:
    """Logging in afterwards produces a token past the boundary, not one caught by it."""
    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    token = user.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": NEW_PASSWORD}).json()[
        "access_token"
    ]
    user.headers["Authorization"] = f"Bearer {token}"

    assert user.get("/api/v1/auth/me").status_code == 200


def test_another_users_token_is_untouched(engine: Engine, user: TestClient) -> None:
    """The boundary is per row. One account's change must not sign anybody else out."""
    other = authenticated_client(engine, email=OTHER_EMAIL)

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert other.get("/api/v1/auth/me").status_code == 200


def test_a_second_session_of_the_same_user_is_revoked_too(engine: Engine, user: TestClient) -> None:
    """Revoking only the calling session would leave the attacker's session alive, which is
    the entire scenario this stage exists for."""
    elsewhere = authenticated_client(engine, email=OWNER_EMAIL)
    assert elsewhere.get("/api/v1/auth/me").status_code == 200

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert elsewhere.get("/api/v1/auth/me").status_code == 401


# --- the boundary itself --------------------------------------------------------------------


def test_a_token_issued_in_the_same_second_as_the_change_is_rejected(
    engine: Engine, user: TestClient
) -> None:
    """The conservative boundary, tested at exactly the second it turns on.

    ``iat`` has one-second resolution, so a token minted at 12:00:00.9 and a password changed
    at 12:00:00.1 cannot be told apart by it. The rule rejects the whole second -- ``iat <=
    floor(password_changed_at)`` -- and this constructs a token sitting precisely on that
    boundary to prove it.
    """
    from app.core.config import Settings
    from app.core.security import create_access_token
    from tests.integration.conftest import TEST_SECRET

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})
    boundary = changed_at(engine, OWNER_EMAIL)
    subject = _public_id_of(engine, OWNER_EMAIL)

    same_second = create_access_token(
        Settings(environment="test", secret_key=TEST_SECRET),
        subject,
        now=dt.datetime.fromtimestamp(int(boundary.timestamp()), tz=dt.UTC),
    )
    assert claims_of(same_second)["iat"] == int(boundary.timestamp()), "the fixture missed"
    user.headers["Authorization"] = f"Bearer {same_second}"

    assert user.get("/api/v1/auth/me").status_code == 401


def test_a_token_one_second_past_the_boundary_is_accepted(engine: Engine, user: TestClient) -> None:
    """The other side of the same line, so the rule is pinned from both directions rather
    than merely being 'strict enough'."""
    from app.core.config import Settings
    from app.core.security import create_access_token
    from tests.integration.conftest import TEST_SECRET

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})
    boundary = changed_at(engine, OWNER_EMAIL)

    just_after = create_access_token(
        Settings(environment="test", secret_key=TEST_SECRET),
        _public_id_of(engine, OWNER_EMAIL),
        now=dt.datetime.fromtimestamp(int(boundary.timestamp()) + 1, tz=dt.UTC),
    )
    user.headers["Authorization"] = f"Bearer {just_after}"

    assert user.get("/api/v1/auth/me").status_code == 200


def test_the_issued_token_sits_past_the_boundary(engine: Engine, user: TestClient) -> None:
    """Why the replacement survives its own revocation: it is minted only once the clock has
    left the second the rule kills, rather than inside it."""
    fresh = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    ).json()["access_token"]

    boundary = int(changed_at(engine, OWNER_EMAIL).timestamp())

    assert int(str(claims_of(fresh)["iat"])) > boundary


# --- the boundary moves only on success -----------------------------------------------------


def test_a_failed_change_moves_neither_the_digest_nor_the_boundary(
    user: TestClient, engine: Engine
) -> None:
    digest_before = hash_of(engine, OWNER_EMAIL)
    boundary_before = changed_at(engine, OWNER_EMAIL)

    user.post(
        CHANGE_URL,
        json={"current_password": "not-the-right-password", "new_password": NEW_PASSWORD},
    )

    assert hash_of(engine, OWNER_EMAIL) == digest_before
    assert changed_at(engine, OWNER_EMAIL) == boundary_before


def test_a_failed_change_leaves_the_token_working(user: TestClient) -> None:
    """The corollary that matters operationally: mistyping your current password must not
    sign you out."""
    user.post(
        CHANGE_URL,
        json={"current_password": "not-the-right-password", "new_password": NEW_PASSWORD},
    )

    assert user.get("/api/v1/auth/me").status_code == 200


def test_a_rejected_new_password_moves_no_boundary(user: TestClient, engine: Engine) -> None:
    boundary_before = changed_at(engine, OWNER_EMAIL)

    user.post(
        CHANGE_URL,
        json={"current_password": TEST_PASSWORD, "new_password": TOO_SHORT_PASSWORD},
    )

    assert changed_at(engine, OWNER_EMAIL) == boundary_before
    assert user.get("/api/v1/auth/me").status_code == 200


def test_a_successful_change_moves_the_boundary_forward(user: TestClient, engine: Engine) -> None:
    before = changed_at(engine, OWNER_EMAIL)

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    assert changed_at(engine, OWNER_EMAIL) > before


def test_a_disabled_account_is_still_401_after_the_column_exists(
    engine: Engine, user: TestClient
) -> None:
    """Disabled-user rejection happens before the boundary is consulted and is unchanged."""
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": OWNER_EMAIL}
        )
        session.commit()

    assert user.get("/api/v1/auth/me").status_code == 401
    assert (
        user.post(
            CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
        ).status_code
        == 401
    )


# --- the token and the API say nothing about any of this ------------------------------------


def test_the_fresh_token_carries_exactly_the_five_claims(user: TestClient) -> None:
    """Revocation added a COLUMN, not a claim. The token contract is untouched."""
    fresh = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    ).json()["access_token"]

    assert set(claims_of(fresh)) == {"sub", "iat", "exp", "jti", "typ"}


def test_no_token_mentions_the_boundary(user: TestClient) -> None:
    fresh = user.post(
        CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
    ).json()["access_token"]
    claims = claims_of(fresh)

    for forbidden in ("password", "changed", "version", "revok"):
        assert not any(forbidden in str(key).lower() for key in claims), forbidden
        assert not any(forbidden in str(value).lower() for value in claims.values()), forbidden


def test_no_response_exposes_password_changed_at(user: TestClient) -> None:
    """Not on /auth/me, not on registration, not on the change itself."""
    texts = [
        user.get("/api/v1/auth/me").text,
        user.post(
            CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
        ).text,
    ]

    for text in texts:
        assert "password_changed_at" not in text
        assert "changed_at" not in text


def test_the_revocation_401_leaks_no_schema_or_sql(user: TestClient) -> None:
    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    text = user.get("/api/v1/auth/me").text

    # `token` is deliberately absent from this list: INVALID_TOKEN is the established error
    # code for every bearer-token failure, and the whole point of this test is that a REVOKED
    # token produces that same code rather than one of its own.
    for leak in [
        "password_changed_at",
        "password_hash",
        "revoked",
        "expired",
        "users",
        "uq_users",
        "psycopg",
        "sqlalchemy",
        "sqlstate",
        "select",
        "argon2",
    ]:
        assert leak not in text.lower(), f"the revocation 401 leaked {leak!r}"


def test_the_revocation_401_exposes_no_internal_key(user: TestClient) -> None:
    import re

    user.post(CHANGE_URL, json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})

    body = user.get("/api/v1/auth/me").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert re.search(r"\b\d{1,6}\b", body["error"]["message"]) is None


# ======================================================================================
# Stage 4.5.3 -- authentication rate limiting
# ======================================================================================
#
# The limiter runs on an injected clock in these tests. Waiting out a real sixty-second
# window would add a minute per test and make the suite's runtime depend on wall time; the
# limiter takes its clock as a constructor argument precisely so this does not have to.
#
# Source addresses come from `TestClient(app, client=(ip, port))`, which sets `request.client`
# exactly as a real connection would. That is the only thing the limiter keys on -- no header
# is consulted, and a test that set one would prove nothing.


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Limited:
    """An app whose limiter this test controls, plus a way to reach it from an address."""

    app: Any
    clock: FakeClock

    def client(self, ip: str = "203.0.113.10") -> TestClient:
        return TestClient(self.app, client=(ip, 44_444))


@pytest.fixture
def limited(engine: Engine) -> Limited:
    """An application whose rate limiter runs on a clock the test owns."""
    app = create_test_app(engine)
    clock = FakeClock()
    app.state.rate_limiter = FixedWindowRateLimiter(clock=clock)
    return Limited(app=app, clock=clock)


def register(client: TestClient, email: str, password: str = TEST_PASSWORD) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Rate Limit Subject"},
    )
    assert response.status_code == 201, response.text


def attempt(client: TestClient, email: str, password: str = "wrong-password-entirely") -> int:
    return client.post(LOGIN_URL, json={"email": email, "password": password}).status_code


# --- the limit itself -------------------------------------------------------------------


def test_logins_below_the_limit_are_unaffected(limited: Limited) -> None:
    """Five is the budget; the first five must behave exactly as they did before."""
    client = limited.client()
    register(client, OWNER_EMAIL)

    codes = [attempt(client, OWNER_EMAIL) for _ in range(5)]

    assert codes == [401] * 5, "a request within the budget was refused by the limiter"


def test_a_correct_password_still_works_within_the_budget(limited: Limited) -> None:
    client = limited.client()
    register(client, OWNER_EMAIL)
    attempt(client, OWNER_EMAIL)

    response = client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD})

    assert response.status_code == 200
    assert response.json()["access_token"]


def test_the_sixth_login_in_a_window_is_429(limited: Limited) -> None:
    client = limited.client()
    register(client, OWNER_EMAIL)

    codes = [attempt(client, OWNER_EMAIL) for _ in range(6)]

    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_the_limiter_releases_after_the_window(limited: Limited) -> None:
    """Not a lockout: the window ends and the budget returns. Verified by moving the clock,
    not by sleeping."""
    client = limited.client()
    register(client, OWNER_EMAIL)
    for _ in range(6):
        attempt(client, OWNER_EMAIL)
    assert attempt(client, OWNER_EMAIL) == 429

    limited.clock.advance(61)

    assert attempt(client, OWNER_EMAIL) == 401


def test_the_correct_password_works_again_after_the_window(limited: Limited) -> None:
    """The definitive no-lockout test: the account is never marked, only the source is
    counted, so the window ending restores normal service."""
    client = limited.client()
    register(client, OWNER_EMAIL)
    for _ in range(6):
        attempt(client, OWNER_EMAIL)

    limited.clock.advance(61)

    assert (
        client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD}).status_code
        == 200
    )


def test_the_429_carries_a_usable_retry_after(limited: Limited) -> None:
    client = limited.client()
    register(client, OWNER_EMAIL)
    for _ in range(5):
        attempt(client, OWNER_EMAIL)

    response = client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": "x"})

    assert response.status_code == 429
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= 60, "Retry-After must describe this window, not a guess"


# --- the key is the address, not the account ---------------------------------------------


def test_the_bucket_is_shared_across_accounts_from_one_address(limited: Limited) -> None:
    """Keyed by IP. Five attempts against five different addresses still exhaust the budget,
    which is what stops an attacker spreading a guess across a user list."""
    client = limited.client()
    register(client, OWNER_EMAIL)
    register(client, OTHER_EMAIL)

    for index in range(5):
        attempt(client, f"person-{index}@example.test")

    assert attempt(client, OWNER_EMAIL) == 429


def test_two_addresses_do_not_share_a_bucket(limited: Limited) -> None:
    """One noisy client must not deny service to everybody else."""
    first = limited.client("198.51.100.1")
    second = limited.client("198.51.100.2")
    register(first, OWNER_EMAIL)
    for _ in range(6):
        attempt(first, OWNER_EMAIL)
    assert attempt(first, OWNER_EMAIL) == 429

    assert attempt(second, OWNER_EMAIL) == 401


def test_a_forwarded_header_cannot_mint_a_fresh_bucket(limited: Limited) -> None:
    """The direct peer address is the key, and no client-controlled header changes it.

    Honouring X-Forwarded-For without a trusted proxy would let an attacker reset their own
    budget on every request -- protection that looks real and is not.
    """
    client = limited.client("198.51.100.9")
    register(client, OWNER_EMAIL)
    for _ in range(6):
        attempt(client, OWNER_EMAIL)

    spoofed = client.post(
        LOGIN_URL,
        json={"email": OWNER_EMAIL, "password": "x"},
        headers={"X-Forwarded-For": "9.9.9.9", "Forwarded": "for=9.9.9.9"},
    )

    assert spoofed.status_code == 429


def test_login_and_change_password_count_separately(limited: Limited) -> None:
    """Different scopes, different buckets: exhausting one must not disable the other."""
    client = limited.client()
    register(client, OWNER_EMAIL)
    token = client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD}).json()[
        "access_token"
    ]
    for _ in range(6):
        attempt(client, OWNER_EMAIL)
    assert attempt(client, OWNER_EMAIL) == 429

    changed = client.post(
        CHANGE_URL,
        json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert changed.status_code == 200


# --- authentication semantics are unchanged below the limit -------------------------------


def test_an_unknown_account_is_still_401_below_the_limit(limited: Limited) -> None:
    client = limited.client()

    assert attempt(client, "no-such-person@example.test") == 401


def test_a_wrong_password_is_still_401_below_the_limit(limited: Limited) -> None:
    client = limited.client()
    register(client, OWNER_EMAIL)

    assert attempt(client, OWNER_EMAIL) == 401


def test_a_disabled_account_is_still_401_below_the_limit(limited: Limited, engine: Engine) -> None:
    client = limited.client()
    register(client, OWNER_EMAIL)
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": OWNER_EMAIL}
        )
        session.commit()

    assert (
        client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD}).status_code
        == 401
    )


def test_the_three_failures_remain_indistinguishable_below_the_limit(
    limited: Limited, engine: Engine
) -> None:
    """Stage 4.1's uniform failure, re-asserted with the limiter in the path."""
    client = limited.client()
    register(client, OWNER_EMAIL)
    register(client, OTHER_EMAIL)
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": OTHER_EMAIL}
        )
        session.commit()

    bodies = [
        client.post(LOGIN_URL, json={"email": "nobody@example.test", "password": "x"}),
        client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": "x"}),
        client.post(LOGIN_URL, json={"email": OTHER_EMAIL, "password": TEST_PASSWORD}),
    ]

    assert {r.status_code for r in bodies} == {401}
    assert len({r.text for r in bodies}) == 1, "the three rejections are distinguishable"


def test_the_three_failures_stay_indistinguishable_once_the_budget_is_gone(
    limited: Limited, engine: Engine
) -> None:
    """And the limiter must not become the oracle the uniform 401 was protecting against.

    Once the budget is gone, an unknown address, a known one with a wrong password, and a
    disabled account all produce the identical 429 -- so the limiter says nothing about which
    of them provoked it.
    """
    client = limited.client()
    register(client, OWNER_EMAIL)
    register(client, OTHER_EMAIL)
    with sessionmaker(bind=engine, future=True)() as session:
        session.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": OTHER_EMAIL}
        )
        session.commit()
    for _ in range(5):
        attempt(client, OWNER_EMAIL)

    bodies = [
        client.post(LOGIN_URL, json={"email": "nobody@example.test", "password": "x"}),
        client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": "x"}),
        client.post(LOGIN_URL, json={"email": OTHER_EMAIL, "password": TEST_PASSWORD}),
    ]

    assert {r.status_code for r in bodies} == {429}
    assert len({r.text for r in bodies}) == 1, "the 429 varies by account"


# --- what the 429 says --------------------------------------------------------------------


def test_the_429_uses_the_shared_error_envelope(limited: Limited) -> None:
    client = limited.client()
    register(client, OWNER_EMAIL)
    for _ in range(5):
        attempt(client, OWNER_EMAIL)

    body = client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": "x"}).json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["details"] == []


def test_the_429_names_no_account_password_or_schema(limited: Limited) -> None:
    client = limited.client()
    register(client, OWNER_EMAIL)
    for _ in range(5):
        attempt(client, OWNER_EMAIL)

    text = client.post(
        LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD}
    ).text.lower()

    for leak in [
        OWNER_EMAIL.lower(),
        TEST_PASSWORD.lower(),
        "argon2",
        "password_hash",
        "users",
        "uq_users",
        "psycopg",
        "sqlalchemy",
        "sqlstate",
        "select",
        "insert",
        "detail:",
        "traceback",
        "203.0.113",
        "bucket",
        "limiter",
    ]:
        assert leak not in text, f"the 429 leaked {leak!r}"


def test_the_429_exposes_no_internal_key(limited: Limited) -> None:
    client = limited.client()
    register(client, OWNER_EMAIL)
    for _ in range(5):
        attempt(client, OWNER_EMAIL)

    body = client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": "x"}).json()

    assert re.search(r"\b\d{1,6}\b", body["error"]["message"]) is None


# --- the password-change endpoint ---------------------------------------------------------


def test_password_change_requests_are_limited(limited: Limited) -> None:
    client = limited.client()
    register(client, OWNER_EMAIL)
    token = client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    body = {"current_password": "wrong-one-entirely", "new_password": NEW_PASSWORD}

    codes = [client.post(CHANGE_URL, json=body, headers=headers).status_code for _ in range(6)]

    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_the_limiter_does_not_bypass_authentication_on_change(limited: Limited) -> None:
    """An unauthenticated caller is refused whether or not they have budget left -- 401 while
    they do, 429 once they do not, and never 200."""
    client = limited.client()
    body = {"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}

    codes = [client.post(CHANGE_URL, json=body).status_code for _ in range(7)]

    assert set(codes) <= {401, 429}
    assert 200 not in codes


def test_a_successful_change_still_revokes_and_reissues(limited: Limited) -> None:
    """Stage 4.5.2 intact with the limiter in front of it."""
    client = limited.client()
    register(client, OWNER_EMAIL)
    token = client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD}).json()[
        "access_token"
    ]

    changed = client.post(
        CHANGE_URL,
        json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {token}"},
    )
    fresh = changed.json()["access_token"]

    assert changed.status_code == 200
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    ), "the old token survived"
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {fresh}"}).status_code
        == 200
    ), "the fresh token does not work"


# --- the limiter touches nothing --------------------------------------------------------


def test_the_limiter_writes_no_database_state(limited: Limited, engine: Engine) -> None:
    """It counts in memory. Nothing about being rate limited is recorded against the account
    -- which is exactly why it cannot become a lockout."""
    client = limited.client()
    register(client, OWNER_EMAIL)

    with sessionmaker(bind=engine, future=True)() as session:
        before = session.execute(
            sa.text(
                "SELECT is_active, last_login_at, password_hash, password_changed_at "
                "FROM users WHERE email = :e"
            ),
            {"e": OWNER_EMAIL},
        ).one()

    for _ in range(8):
        attempt(client, OWNER_EMAIL)

    with sessionmaker(bind=engine, future=True)() as session:
        after = session.execute(
            sa.text(
                "SELECT is_active, last_login_at, password_hash, password_changed_at "
                "FROM users WHERE email = :e"
            ),
            {"e": OWNER_EMAIL},
        ).one()
        rows = session.scalar(sa.text("SELECT count(*) FROM users"))

    assert tuple(before) == tuple(after), "the limiter changed the account row"
    assert rows == 1, "the limiter created a row"


def test_no_table_records_rate_limiting(engine: Engine) -> None:
    """Stage 4.5.3 added no schema. A counter table would be a migration, and there is none."""
    with sessionmaker(bind=engine, future=True)() as session:
        matches = session.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                "AND (table_name LIKE '%rate%' OR table_name LIKE '%limit%' "
                "OR table_name LIKE '%attempt%' OR table_name LIKE '%lockout%')"
            )
        ).all()

    assert matches == []


def test_an_unrelated_account_is_unaffected(limited: Limited) -> None:
    """A different source, a different account: neither shares the exhausted budget."""
    noisy = limited.client("198.51.100.50")
    quiet = limited.client("198.51.100.51")
    register(noisy, OWNER_EMAIL)
    register(quiet, OTHER_EMAIL)
    for _ in range(6):
        attempt(noisy, OWNER_EMAIL)

    response = quiet.post(LOGIN_URL, json={"email": OTHER_EMAIL, "password": TEST_PASSWORD})

    assert response.status_code == 200


def test_unprotected_routes_are_not_limited(limited: Limited) -> None:
    """Only the two authentication routes carry a limit; registration and /auth/me do not,
    and a stage that quietly limited everything would be a different change."""
    client = limited.client()
    register(client, OWNER_EMAIL)
    token = client.post(LOGIN_URL, json={"email": OWNER_EMAIL, "password": TEST_PASSWORD}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}

    codes = [client.get("/api/v1/auth/me", headers=headers).status_code for _ in range(12)]

    assert codes == [200] * 12
