"""R1.1: the cookie session's pieces, without a database.

Its behaviour against PostgreSQL -- rotation, replay, revocation, CSRF on real requests -- is
``tests/integration/test_cookie_sessions.py``. These pin what can be read without one: the
attributes every cookie is set with, that the CSRF token is bound to its session and compared
in constant time, that a refresh secret is stored only as its digest, and that an access token
names its session only when it belongs to one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import uuid

from fastapi import Response

from app.api import session_cookies
from app.api.session_cookies import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    REFRESH_COOKIE,
    REFRESH_PATH,
    clear_session_cookies,
    set_session_cookies,
)
from app.core.config import Settings
from app.core.security import (
    REFRESH_SECRET_BYTES,
    create_access_token,
    csrf_matches,
    csrf_token,
    decode_access_token,
    hash_refresh_secret,
    new_refresh_secret,
)
from app.schemas.auth import UserResponse
from app.services.session import IssuedSession, SessionService

SETTINGS = Settings(environment="test", secret_key="a-test-only-secret-key-of-enough-length")
FAMILY = uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")
OTHER = uuid.UUID("16fd2706-8baf-433b-82eb-8c7fada847da")


def issued() -> IssuedSession:
    return IssuedSession(
        user=UserResponse(
            public_id=uuid.uuid4(),
            email="cookie@example.test",
            full_name="Cookie",
            is_active=True,
            created_at=dt.datetime(2026, 11, 20, tzinfo=dt.UTC),
            last_login_at=None,
        ),
        access_token="access.jwt.value",
        access_expires_in=1800,
        refresh_secret="refresh-secret-value",
        refresh_expires_in=43200,
        csrf_token="csrf-token-value",
    )


def set_cookies() -> dict[str, str]:
    response = Response()
    set_session_cookies(response, issued())
    headers = [value.decode() for name, value in response.raw_headers if name == b"set-cookie"]
    return {header.split("=", 1)[0]: header.lower() for header in headers}


# --- the cookies ----------------------------------------------------------------------------------


def test_exactly_three_cookies_are_set() -> None:
    assert set(set_cookies()) == {ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE}


def test_every_cookie_is_secure_strict_and_names_no_domain() -> None:
    for name, header in set_cookies().items():
        assert "; secure" in header, name
        assert "samesite=strict" in header, name
        assert "domain=" not in header, name


def test_the_tokens_are_httponly_and_the_csrf_token_is_not() -> None:
    cookies = set_cookies()

    assert "httponly" in cookies[ACCESS_COOKIE]
    assert "httponly" in cookies[REFRESH_COOKIE]
    # The page must read it to send it back; it is not a credential.
    assert "httponly" not in cookies[CSRF_COOKIE]


def test_the_refresh_cookie_is_sent_only_to_the_session_routes() -> None:
    cookies = set_cookies()

    assert f"path={REFRESH_PATH.lower()}" in cookies[REFRESH_COOKIE]
    assert "path=/;" in cookies[ACCESS_COOKIE] + ";"
    assert "path=/;" in cookies[CSRF_COOKIE] + ";"


def test_the_cookie_names_carry_the_browser_enforced_prefixes() -> None:
    """``__Host-`` makes a browser refuse the cookie unless Secure, Path=/ and domain-less."""
    assert ACCESS_COOKIE.startswith("__Host-")
    assert CSRF_COOKIE.startswith("__Host-")
    assert REFRESH_COOKIE.startswith("__Secure-")


def test_the_csrf_token_also_goes_in_a_response_header() -> None:
    response = Response()
    set_session_cookies(response, issued())

    assert response.headers[CSRF_HEADER] == "csrf-token-value"


def test_clearing_expires_all_three_with_their_own_paths() -> None:
    response = Response()
    clear_session_cookies(response)
    headers = [
        value.decode().lower() for name, value in response.raw_headers if name == b"set-cookie"
    ]

    assert len(headers) == 3
    assert all("max-age=0" in header for header in headers)
    assert any(f"path={REFRESH_PATH.lower()}" in header for header in headers)


def test_only_get_head_and_options_skip_the_csrf_check() -> None:
    assert frozenset({"GET", "HEAD", "OPTIONS"}) == session_cookies.SAFE_METHODS


# --- the CSRF token -------------------------------------------------------------------------------


def test_the_csrf_token_is_bound_to_its_session() -> None:
    assert csrf_token(SETTINGS, FAMILY) == csrf_token(SETTINGS, FAMILY)
    assert csrf_token(SETTINGS, FAMILY) != csrf_token(SETTINGS, OTHER)


def test_the_csrf_token_depends_on_the_secret() -> None:
    other = Settings(environment="test", secret_key="another-test-only-secret-of-enough-length")

    assert csrf_token(SETTINGS, FAMILY) != csrf_token(other, FAMILY)


def test_a_csrf_token_matches_only_its_own_session() -> None:
    token = csrf_token(SETTINGS, FAMILY)

    assert csrf_matches(SETTINGS, FAMILY, token)
    assert not csrf_matches(SETTINGS, OTHER, token)
    assert not csrf_matches(SETTINGS, FAMILY, None)
    assert not csrf_matches(SETTINGS, FAMILY, "")
    assert not csrf_matches(SETTINGS, FAMILY, token[:-1] + ("0" if token[-1] != "0" else "1"))


def test_the_comparison_is_constant_time() -> None:
    assert "hmac.compare_digest" in inspect.getsource(csrf_matches)


# --- the refresh secret and the access token ------------------------------------------------------


def test_a_refresh_secret_is_random_and_long() -> None:
    secrets = {new_refresh_secret() for _ in range(50)}

    assert len(secrets) == 50
    assert REFRESH_SECRET_BYTES >= 32
    assert all(len(secret) >= 43 for secret in secrets)


def test_only_the_digest_of_a_refresh_secret_is_stored() -> None:
    assert hash_refresh_secret("abc") == hashlib.sha256(b"abc").hexdigest()
    source = inspect.getsource(SessionService)
    assert "token_hash=hash_refresh_secret(secret)" in source
    assert "token_hash=hash_refresh_secret(secret_next)" in source


def test_a_bearer_token_names_no_session_and_a_session_token_names_its_own() -> None:
    subject = uuid.uuid4()
    bearer = decode_access_token(SETTINGS, create_access_token(SETTINGS, subject))
    session = decode_access_token(
        SETTINGS, create_access_token(SETTINGS, subject, session_id=FAMILY)
    )

    assert bearer.session_id is None
    assert session.session_id == FAMILY
