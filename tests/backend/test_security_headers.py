"""Security response headers, and the boundaries where they are easy to lose.

Stage 4.5.6. Three things make this worth testing rather than eyeballing.

**The headers have to survive every exit path.** A response can leave this application through
the router, through one of the five centralised exception handlers, through CORSMiddleware
answering a preflight without ever calling inward, or through Starlette's ServerErrorMiddleware
turning an unhandled exception into a 500 -- and that last one sits OUTSIDE every user
middleware. Each is exercised below, because "we added middleware" only covers the first.

**HSTS is the one header that can do harm.** Sent over plain HTTP in development it teaches a
browser to refuse ``http://localhost`` for a year. The tests therefore assert its ABSENCE at
least as carefully as its presence.

**The docs are the one HTML this application serves.** ``default-src 'none'`` would render
Swagger UI blank, so those paths are exempted from the CSP -- and only from the CSP.

No database is used here: every path exercised is refused or answered before a session is
touched, which is itself worth pinning.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.config import Settings
from app.core.security_headers import (
    BASE_SECURITY_HEADERS,
    CONTENT_SECURITY_POLICY,
    CONTENT_SECURITY_POLICY_HEADER,
    PERMISSIONS_POLICY,
    STRICT_TRANSPORT_SECURITY,
    hsts_applies,
    hsts_value,
    security_headers,
)
from app.main import create_app
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

SECRET = "security-header-suite-secret"


class Probe(BaseModel):
    """The body shape /probe/validated requires."""

    count: int


#: Every header that must be on an ordinary response, with the exact value expected.
EXPECTED: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": PERMISSIONS_POLICY,
    "content-security-policy": CONTENT_SECURITY_POLICY,
}


def build(**overrides: object) -> FastAPI:
    """The real application, plus probes that fail in each way that bypasses the router."""
    settings = Settings(environment="test", secret_key=SECRET, **overrides)  # type: ignore[arg-type]
    application = create_app(settings)

    @application.get("/probe/ok")
    def _ok() -> dict[str, bool]:
        return {"ok": True}

    @application.get("/probe/boom", response_model=None)
    def _boom() -> dict[str, bool]:
        """Raises past every handler, so the 500 comes from ServerErrorMiddleware."""
        raise RuntimeError("deliberate failure")

    @application.post("/probe/validated")
    def _validated(payload: Probe) -> dict[str, bool]:
        """Rejects a bad body through FastAPI's own validation handler.

        A probe rather than a real endpoint because every real one that validates a body also
        depends on a database session, and this suite deliberately has none.
        """
        return {"ok": payload.count > 0}

    return application


@pytest.fixture
def app() -> FastAPI:
    return build()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ======================================================================================
# Presence on every exit path
# ======================================================================================


def test_a_successful_response_carries_every_header(client: TestClient) -> None:
    response = client.get("/probe/ok")

    assert response.status_code == 200
    for name, value in EXPECTED.items():
        assert response.headers[name] == value


def test_a_404_carries_every_header(client: TestClient) -> None:
    """A 404 leaves through an exception handler, not the router."""
    response = client.get("/api/v1/no-such-endpoint")

    assert response.status_code == 404
    for name, value in EXPECTED.items():
        assert response.headers[name] == value


def test_a_422_carries_every_header(client: TestClient) -> None:
    """Validation failures leave through FastAPI's own handler."""
    response = client.post("/probe/validated", json={"count": "not-a-number"})

    assert response.status_code == 422
    for name, value in EXPECTED.items():
        assert response.headers[name] == value


def test_a_500_carries_every_header(client: TestClient) -> None:
    """The hard case.

    Starlette routes the bare ``Exception`` handler to ServerErrorMiddleware, which wraps
    every user middleware -- so this response never passes back through
    SecurityHeadersMiddleware's ``send``. It is covered only because
    ``handle_unexpected_error`` applies the same policy itself. Remove that and this fails.
    """
    response = client.get("/probe/boom")

    assert response.status_code == 500
    for name, value in EXPECTED.items():
        assert response.headers[name] == value


def test_a_cors_preflight_carries_every_header(client: TestClient) -> None:
    """CORSMiddleware answers a preflight itself without calling inward.

    It is covered only because the security middleware is OUTSIDE it. Adding the security
    middleware before CORS instead would leave this response bare.
    """
    response = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    for name, value in EXPECTED.items():
        assert response.headers[name] == value


# ======================================================================================
# Exact values, and no duplicates
# ======================================================================================


def test_the_values_are_exactly_as_configured(client: TestClient) -> None:
    response = client.get("/probe/ok")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in response.headers["permissions-policy"]
    assert "geolocation=()" in response.headers["permissions-policy"]
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_header_is_sent_twice(client: TestClient, name: str) -> None:
    """Written by assignment rather than by appending, so a second value cannot appear.

    A duplicated ``X-Frame-Options`` is not merely untidy: browsers have historically treated
    conflicting values as no policy at all.
    """
    response = client.get("/probe/ok")

    assert len(response.headers.get_list(name)) == 1


def test_the_base_headers_carry_no_csp_of_their_own() -> None:
    """CSP is conditional, so it must not be baked into the unconditional set."""
    assert CONTENT_SECURITY_POLICY_HEADER not in BASE_SECURITY_HEADERS


# ======================================================================================
# HSTS: environment- and scheme-aware
# ======================================================================================


def test_hsts_is_sent_for_production_over_https() -> None:
    app = create_app(Settings(environment="production", secret_key=SECRET))
    client = TestClient(app, base_url="https://api.example.test")

    response = client.get("/health")

    assert response.headers[STRICT_TRANSPORT_SECURITY] == "max-age=31536000; includeSubDomains"


def test_hsts_is_not_sent_for_production_over_plain_http() -> None:
    """Announcing an HTTPS-only policy over a connection that is not HTTPS is not something
    to do on the client's behalf."""
    app = create_app(Settings(environment="production", secret_key=SECRET))
    client = TestClient(app, base_url="http://api.example.test")

    response = client.get("/health")

    assert STRICT_TRANSPORT_SECURITY not in response.headers


def test_hsts_is_not_sent_in_the_ordinary_test_configuration(client: TestClient) -> None:
    """The regression that matters most to a developer: this must never appear locally."""
    response = client.get("/probe/ok")

    assert STRICT_TRANSPORT_SECURITY not in response.headers


@pytest.mark.parametrize("environment", ["development", "test", "staging"])
def test_hsts_is_not_sent_outside_production_even_over_https(environment: str) -> None:
    app = create_app(Settings(environment=environment, secret_key=SECRET))
    client = TestClient(app, base_url="https://api.example.test")

    response = client.get("/health")

    assert STRICT_TRANSPORT_SECURITY not in response.headers


def test_the_hsts_policy_string_is_built_from_settings() -> None:
    base = Settings(environment="production", secret_key=SECRET)

    assert hsts_value(base) == "max-age=31536000; includeSubDomains"
    assert hsts_value(base.model_copy(update={"hsts_include_subdomains": False})) == (
        "max-age=31536000"
    )
    assert hsts_value(base.model_copy(update={"hsts_preload": True})) == (
        "max-age=31536000; includeSubDomains; preload"
    )


def test_preload_is_off_by_default() -> None:
    """Submitting a domain to the preload list is close to irreversible, so it is opt-in."""
    assert Settings(environment="production", secret_key=SECRET).hsts_preload is False
    assert "preload" not in hsts_value(Settings(environment="production", secret_key=SECRET))


def test_a_zero_max_age_switches_hsts_off_rather_than_emitting_it() -> None:
    """``max-age=0`` tells a browser to FORGET an existing policy.

    A deployment that zeroes the setting means "do not send this header", not "actively undo
    it on every client that ever visited".
    """
    settings = Settings(environment="production", secret_key=SECRET, hsts_max_age=0)

    assert hsts_applies(settings, "https") is False


# ======================================================================================
# The CSP exemption for the interactive documentation
# ======================================================================================


def test_the_docs_page_is_exempt_from_the_csp(client: TestClient) -> None:
    """Swagger UI loads its assets from a CDN; ``default-src 'none'`` renders it blank."""
    response = client.get("/docs")

    assert response.status_code == 200
    assert CONTENT_SECURITY_POLICY_HEADER not in response.headers


def test_the_docs_page_still_carries_every_other_header(client: TestClient) -> None:
    """Exempt from the CSP only. Clickjacking protection is not negotiable for an HTML page."""
    response = client.get("/docs")

    for name, value in BASE_SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_the_openapi_document_is_not_exempt(client: TestClient) -> None:
    """It is JSON, not a document that loads anything."""
    response = client.get("/openapi.json")

    assert response.headers[CONTENT_SECURITY_POLICY_HEADER] == CONTENT_SECURITY_POLICY


def test_production_exempts_nothing_because_it_serves_no_html() -> None:
    app = create_app(Settings(environment="production", secret_key=SECRET))
    exempt = app.user_middleware[0].kwargs["csp_exempt_paths"]

    assert exempt == frozenset()


# ======================================================================================
# The policy function, independent of transport
# ======================================================================================


def test_security_headers_omits_the_csp_when_asked() -> None:
    settings = Settings(environment="test", secret_key=SECRET)

    assert CONTENT_SECURITY_POLICY_HEADER in security_headers(settings, "http")
    assert CONTENT_SECURITY_POLICY_HEADER not in security_headers(
        settings, "http", include_csp=False
    )


def test_every_header_name_is_lowercase() -> None:
    """HTTP header names are case-insensitive, but a mixed-case key would silently defeat the
    duplicate-suppressing assignment in the middleware."""
    settings = Settings(environment="production", secret_key=SECRET)

    assert all(name == name.lower() for name in security_headers(settings, "https"))


# ======================================================================================
# Nothing else changed
# ======================================================================================


def test_the_request_id_header_still_appears(client: TestClient) -> None:
    """Stage 4.5.4.1 must survive a new middleware being wrapped around it."""
    response = client.get("/probe/ok")

    assert response.headers["x-request-id"]


def test_the_request_id_still_appears_on_a_500(client: TestClient) -> None:
    response = client.get("/probe/boom")

    assert response.status_code == 500
    assert response.headers["x-request-id"]


def test_a_supplied_request_id_is_still_echoed(client: TestClient) -> None:
    response = client.get("/probe/ok", headers={"X-Request-ID": "caller-supplied-id"})

    assert response.headers["x-request-id"] == "caller-supplied-id"


def test_response_bodies_are_unchanged(client: TestClient) -> None:
    """Headers are additive. The body a caller parses must be byte-for-byte what it was."""
    health = client.get("/health")
    not_found = client.get("/api/v1/no-such-endpoint")

    assert health.json()["status"] == "ok"
    assert set(not_found.json()) == {"error"}
    assert not_found.json()["error"]["code"] == "NOT_FOUND"


def test_the_openapi_document_is_unchanged() -> None:
    """Middleware is invisible to the schema: no route added, no response model altered."""
    spec = create_app(Settings(environment="test", secret_key=SECRET)).openapi()
    operations = [(path, method) for path, item in spec["paths"].items() for method in item]

    # Stage 7.7 added POST .../copilot/ask: 54 / 86 -> 55 / 87. No middleware route.
    # Stage 7.9 added the knowledge documents and their search: 55 / 87 -> 60 / 93.
    # Stage 7.11 added the copilot conversations: 60 / 93 -> 63 / 98.
    assert len(spec["paths"]) == 63
    assert len(operations) == 98
    # Built from the real factory, so the middleware is present -- and contributes nothing.
    assert not any("security" in path.lower() for path in spec["paths"])


def test_the_middleware_order_is_the_one_that_makes_the_tests_above_pass(app: FastAPI) -> None:
    """Asserted explicitly, because the ordering is load-bearing rather than incidental.

    ``add_middleware`` inserts at the FRONT, so index 0 is outermost. Security headers must be
    outside CORS to reach a preflight; the request-id middleware stays innermost, exactly
    where Stage 4.5.4.1 left it.
    """
    assert [m.cls for m in app.user_middleware] == [
        SecurityHeadersMiddleware,
        CORSMiddleware,
        RequestIdMiddleware,
    ]
