"""R1.1: the browser's cookie session, against real PostgreSQL.

``POST /auth/session`` signs in and keeps every credential in a cookie: the access and refresh
tokens ``HttpOnly``, a CSRF token readable by the page. A cookie-authenticated request that
changes something must echo the CSRF token, refreshing rotates the refresh token and a replayed
one ends the session, and signing out, a password change, a deactivated account and expiry each
end it -- access tokens included. The bearer login is unchanged.

Every client here talks to ``https://testserver``: the cookies are ``Secure``, and a client that
honours that, as a browser does, sends them only over HTTPS. The last section runs migration
0023's own upgrade and downgrade inside a rolled-back transaction.
"""

from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import Iterator
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.session_cookies import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    REFRESH_COOKIE,
    REFRESH_PATH,
)
from tests.integration.conftest import TEST_PASSWORD, create_test_app, requires_postgres

pytestmark = requires_postgres

EMAIL = "cookie-session@example.test"
OTHER = "cookie-other@example.test"
BASE = "https://testserver"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "database"
    / "migrations"
    / "versions"
    / "20261120_0023_auth_sessions.py"
)


@pytest.fixture(autouse=True)
def _clean(session: Session) -> Iterator[None]:
    """The suite's own teardown after every test: users -- and by CASCADE their sessions --
    truncated, so every session a test sees is its own."""
    yield


@pytest.fixture
def app(engine: Engine) -> Any:
    """One application, with the login limit out of the way of a suite that signs in a lot."""
    application = create_test_app(engine, auth_login_rate_limit=1000)
    registrar = TestClient(application, base_url=BASE)
    for email in (EMAIL, OTHER):
        registrar.post(
            "/api/v1/auth/register",
            json={"email": email, "password": TEST_PASSWORD, "full_name": "Cookie Suite"},
        )
    return application


def browser(app: Any) -> TestClient:
    """A fresh browser: an empty cookie jar, HTTPS."""
    return TestClient(app, base_url=BASE)


def sign_in(client: TestClient, email: str = EMAIL) -> dict[str, Any]:
    response = client.post("/api/v1/auth/session", json={"email": email, "password": TEST_PASSWORD})
    assert response.status_code == 201, response.text
    return dict(response.json())


def set_cookies(response: Response) -> dict[str, str]:
    """name -> the raw ``Set-Cookie`` header that set it."""
    return {header.split("=", 1)[0]: header for header in response.headers.get_list("set-cookie")}


def bare(app: Any, **cookies: str) -> TestClient:
    """A client that sends exactly these cookies and nothing it remembered."""
    header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    return TestClient(app, base_url=BASE, headers={"Cookie": header} if header else {})


def csrf(client: TestClient) -> dict[str, str]:
    return {CSRF_HEADER: str(client.cookies.get(CSRF_COOKIE))}


def hotel_payload(slug: str) -> dict[str, object]:
    return {
        "name": f"Hotel {slug}",
        "slug": slug,
        "address_line1": "1 Harbour Road",
        "city": "Piraeus",
        "country_code": "GR",
        "currency": "EUR",
        "timezone": "Europe/Athens",
    }


def stored_hashes(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return list(connection.scalars(sa.text("SELECT token_hash FROM auth_sessions")))


# --- signing in -----------------------------------------------------------------------------------


def test_sign_in_sets_three_cookies_and_puts_no_credential_in_the_body(app: Any) -> None:
    client = browser(app)
    response = client.post("/api/v1/auth/session", json={"email": EMAIL, "password": TEST_PASSWORD})

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"user", "csrf_token", "expires_in", "refresh_expires_in"}
    assert body["user"]["email"] == EMAIL
    assert "access_token" not in response.text
    cookies = set_cookies(response)
    assert set(cookies) == {ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE}
    for header in cookies.values():
        attributes = header.lower()
        assert "; secure" in attributes
        assert "samesite=strict" in attributes
        assert "domain=" not in attributes
    assert "httponly" in cookies[ACCESS_COOKIE].lower()
    assert "httponly" in cookies[REFRESH_COOKIE].lower()
    assert "httponly" not in cookies[CSRF_COOKIE].lower()
    assert "path=/;" in cookies[ACCESS_COOKIE].lower() + ";"
    assert f"path={REFRESH_PATH.lower()}" in cookies[REFRESH_COOKIE].lower()
    assert client.cookies.get(CSRF_COOKIE) == body["csrf_token"]


def test_a_wrong_password_is_logins_401_and_sets_no_cookie(app: Any) -> None:
    response = browser(app).post(
        "/api/v1/auth/session", json={"email": EMAIL, "password": "not-the-password-1"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_FAILED"
    assert response.headers.get_list("set-cookie") == []


def test_only_a_digest_of_the_refresh_token_is_stored(app: Any, engine: Engine) -> None:
    client = browser(app)
    sign_in(client)
    secret = str(client.cookies.get(REFRESH_COOKIE))

    assert stored_hashes(engine) == [hashlib.sha256(secret.encode()).hexdigest()]
    assert secret not in str(stored_hashes(engine))


# --- the access cookie, and CSRF ------------------------------------------------------------------


def test_the_cookie_alone_authenticates_a_read(app: Any) -> None:
    client = browser(app)
    sign_in(client)

    response = client.get("/api/v1/auth/me")

    assert response.status_code == 200, response.text
    assert response.json()["email"] == EMAIL


def test_a_write_without_the_csrf_token_is_refused_and_writes_nothing(app: Any) -> None:
    client = browser(app)
    sign_in(client)

    missing = client.post("/api/v1/hotels", json=hotel_payload("no-csrf"))
    wrong = client.post(
        "/api/v1/hotels", json=hotel_payload("no-csrf"), headers={CSRF_HEADER: "0" * 64}
    )

    for refused in (missing, wrong):
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "CSRF_FAILED"
    assert client.get("/api/v1/hotels").json()["total"] == 0


def test_a_write_with_the_csrf_token_goes_through(app: Any) -> None:
    client = browser(app)
    sign_in(client)

    response = client.post("/api/v1/hotels", json=hotel_payload("with-csrf"), headers=csrf(client))

    assert response.status_code == 201, response.text


def test_another_sessions_csrf_token_does_not_fit(app: Any) -> None:
    """The token is bound to its session, not merely well-formed."""
    mine, theirs = browser(app), browser(app)
    sign_in(mine)
    sign_in(theirs, OTHER)

    response = mine.post("/api/v1/hotels", json=hotel_payload("cross"), headers=csrf(theirs))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"


def test_a_bearer_token_needs_no_csrf_token(app: Any) -> None:
    client = browser(app)
    token = client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": TEST_PASSWORD}
    ).json()["access_token"]

    response = client.post(
        "/api/v1/hotels",
        json=hotel_payload("bearer"),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201, response.text


def test_a_bearer_token_in_the_access_cookie_is_refused(app: Any) -> None:
    """A cookie must carry a session's token: a bearer token has no session to check CSRF by."""
    token = (
        browser(app)
        .post("/api/v1/auth/login", json={"email": EMAIL, "password": TEST_PASSWORD})
        .json()["access_token"]
    )

    response = bare(app, **{ACCESS_COOKIE: token}).get("/api/v1/auth/me")

    assert response.status_code == 401


def test_no_credential_at_all_is_401(app: Any) -> None:
    assert browser(app).get("/api/v1/auth/me").status_code == 401


# --- refreshing -----------------------------------------------------------------------------------


def test_a_refresh_rotates_the_refresh_token_and_keeps_the_csrf_token(app: Any) -> None:
    client = browser(app)
    started = sign_in(client)
    first = client.cookies.get(REFRESH_COOKIE)

    response = client.post("/api/v1/auth/session/refresh", headers=csrf(client))

    assert response.status_code == 200, response.text
    assert client.cookies.get(REFRESH_COOKIE) != first
    assert response.json()["csrf_token"] == started["csrf_token"]
    assert client.get("/api/v1/auth/me").status_code == 200


def test_a_refresh_without_the_csrf_token_is_refused_and_consumes_nothing(app: Any) -> None:
    client = browser(app)
    sign_in(client)

    refused = client.post("/api/v1/auth/session/refresh")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "CSRF_FAILED"

    assert client.post("/api/v1/auth/session/refresh", headers=csrf(client)).status_code == 200


def test_a_replayed_refresh_token_ends_the_whole_session(app: Any) -> None:
    """The thief and the owner both hold it; the session ends for both."""
    client = browser(app)
    sign_in(client)
    stolen = str(client.cookies.get(REFRESH_COOKIE))
    assert client.post("/api/v1/auth/session/refresh", headers=csrf(client)).status_code == 200

    replay = bare(app, **{REFRESH_COOKIE: stolen}).post(
        "/api/v1/auth/session/refresh", headers=csrf(client)
    )

    assert replay.status_code == 401
    # The owner's newer tokens are gone too: the refresh, and the access token with it.
    assert client.post("/api/v1/auth/session/refresh", headers=csrf(client)).status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401


def test_an_unknown_refresh_token_is_401(app: Any) -> None:
    client = bare(app, **{REFRESH_COOKIE: "made-up"})
    assert (
        client.post("/api/v1/auth/session/refresh", headers={CSRF_HEADER: "x"}).status_code == 401
    )


def test_an_expired_session_cannot_be_refreshed_and_its_access_token_stops(
    app: Any, engine: Engine
) -> None:
    client = browser(app)
    sign_in(client)
    with engine.begin() as connection:
        # Expiry is the ONLY reason left to refuse: the password predates the session, so the
        # password-change rule cannot be what refuses it.
        connection.execute(
            sa.text("UPDATE users SET password_changed_at = now() - interval '3 days'")
        )
        connection.execute(
            sa.text(
                "UPDATE auth_sessions SET created_at = now() - interval '2 days', "
                "expires_at = now() - interval '1 second'"
            )
        )

    assert client.post("/api/v1/auth/session/refresh", headers=csrf(client)).status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401


def test_a_deactivated_account_cannot_refresh(app: Any, engine: Engine) -> None:
    client = browser(app)
    sign_in(client)
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": EMAIL}
        )

    assert client.post("/api/v1/auth/session/refresh", headers=csrf(client)).status_code == 401


# --- signing out ----------------------------------------------------------------------------------


def test_signing_out_ends_the_session_and_clears_the_cookies(app: Any) -> None:
    client = browser(app)
    sign_in(client)
    access = str(client.cookies.get(ACCESS_COOKIE))
    refresh = str(client.cookies.get(REFRESH_COOKIE))
    headers = csrf(client)

    response = client.post("/api/v1/auth/session/end", headers=headers)

    assert response.status_code == 204
    cleared = set_cookies(response)
    assert set(cleared) == {ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE}
    for header in cleared.values():
        assert "max-age=0" in header.lower() or _expired(header)
    # The tokens it held stop at once, wherever a copy of them went.
    assert bare(app, **{ACCESS_COOKIE: access}).get("/api/v1/auth/me").status_code == 401
    replay = bare(app, **{REFRESH_COOKIE: refresh}).post(
        "/api/v1/auth/session/refresh", headers=headers
    )
    assert replay.status_code == 401


def _expired(header: str) -> bool:
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name.lower() == "expires":
            return parsedate_to_datetime(value).timestamp() <= 0 or "1970" in value
    return False


def test_signing_out_without_the_csrf_token_is_refused_and_the_session_lives(app: Any) -> None:
    client = browser(app)
    sign_in(client)

    response = client.post("/api/v1/auth/session/end")

    assert response.status_code == 403
    assert client.get("/api/v1/auth/me").status_code == 200


def test_signing_out_with_no_session_is_still_204(app: Any) -> None:
    assert browser(app).post("/api/v1/auth/session/end").status_code == 204


def test_signing_out_ends_only_this_session(app: Any) -> None:
    first, second = browser(app), browser(app)
    sign_in(first)
    sign_in(second)

    assert first.post("/api/v1/auth/session/end", headers=csrf(first)).status_code == 204

    assert second.get("/api/v1/auth/me").status_code == 200


# --- a password change ----------------------------------------------------------------------------


def test_a_password_change_ends_every_session_and_continues_the_callers(
    app: Any, engine: Engine
) -> None:
    caller, elsewhere = browser(app), browser(app)
    sign_in(caller)
    sign_in(elsewhere)
    before = set(stored_hashes(engine))

    response = caller.post(
        "/api/v1/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": "a-brand-new-password-1"},
        headers=csrf(caller),
    )

    assert response.status_code == 200, response.text
    assert set(set_cookies(response)) == {ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE}
    assert caller.get("/api/v1/auth/me").status_code == 200
    assert elsewhere.get("/api/v1/auth/me").status_code == 401
    assert (
        elsewhere.post("/api/v1/auth/session/refresh", headers=csrf(elsewhere)).status_code == 401
    )
    # Ended in the table itself, in the password's own transaction -- not merely refused later
    # because they predate the password.
    with engine.connect() as connection:
        live = set(
            connection.scalars(
                sa.text("SELECT token_hash FROM auth_sessions WHERE revoked_at IS NULL")
            )
        )
    assert before and not before & live


def test_a_password_change_by_bearer_sets_no_cookie(app: Any) -> None:
    client = browser(app)
    token = client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": TEST_PASSWORD}
    ).json()["access_token"]

    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": "a-brand-new-password-1"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.headers.get_list("set-cookie") == []


# --- the table ------------------------------------------------------------------------------------


def test_a_users_sessions_go_with_the_account(app: Any, engine: Engine) -> None:
    sign_in(browser(app))
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM users WHERE email = :e"), {"e": EMAIL})

    assert stored_hashes(engine) == []


@pytest.mark.parametrize(
    ("token_hash", "expires"),
    [("not-hex", "now() + interval '1 hour'"), ("a" * 64, "now() - interval '1 hour'")],
)
def test_the_table_refuses_a_malformed_digest_or_an_expiry_before_creation(
    app: Any, engine: Engine, token_hash: str, expires: str
) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO auth_sessions (user_id, family_id, token_hash, expires_at) "
                f"SELECT id, gen_random_uuid(), :h, {expires} FROM users WHERE email = :e"
            ),
            {"h": token_hash, "e": EMAIL},
        )


# --- migration 0023 -------------------------------------------------------------------------------


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0023", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def migrating(engine: Engine) -> Iterator[sa.Connection]:
    """A connection inside a transaction that is always rolled back, with Alembic operations
    bound to it -- so 0023's own functions run against the real catalog and leave nothing."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
            with Operations.context(MigrationContext.configure(connection)):
                yield connection
        finally:
            transaction.rollback()


def table_exists(connection: sa.Connection) -> bool:
    return bool(
        connection.scalar(sa.text("SELECT to_regclass('public.auth_sessions') IS NOT NULL"))
    )


def test_the_migration_is_the_one_on_disk() -> None:
    migration = load_migration()
    assert migration.revision == "0023_auth_sessions"
    assert migration.down_revision == "0022_catalogue_versions"


def test_the_downgrade_drops_only_the_table_and_the_upgrade_restores_it(
    migrating: sa.Connection,
) -> None:
    migration = load_migration()
    tables_before = migrating.scalar(
        sa.text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
    )
    assert table_exists(migrating)

    migration.downgrade()
    assert not table_exists(migrating)
    tables_down = migrating.scalar(
        sa.text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
    )
    migration.upgrade()

    assert tables_down == tables_before - 1
    assert table_exists(migrating)
