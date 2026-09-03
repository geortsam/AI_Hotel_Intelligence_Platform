"""Request/correlation identifiers.

Stage 4.5.4.1. A request id groups the log lines one request produced. It is metadata, and the
tests that matter most here are the ones proving it is *only* metadata: it cannot name a user,
choose a hotel, reach a query, or move a rate-limit bucket.

The second theme is that a client-supplied value is never trusted. The header is attacker
-controlled and this codebase formats log messages with ``%s``, so a value carrying a newline
would let somebody write their own log lines. Every rejection path below checks both that the
bad value was replaced and that it was not echoed anywhere.

No database: these exercise middleware and a ContextVar, so they belong in the fast suite.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import NoReturn, cast

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db, login_rate_limit
from app.core.config import Settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.request_id import (
    NO_REQUEST_ID,
    REQUEST_ID_HEADER,
    current_request_id,
    new_request_id,
    sanitise_request_id,
)
from app.main import create_app
from app.middleware.request_id import RequestIdMiddleware, _supplied_header

SECRET = "request-id-suite-signing-secret-not-real"

#: A UUID4 hex is 32 lowercase hex characters and carries no dashes.
UUID4_HEX = re.compile(r"[0-9a-f]{32}")


def _stub_session() -> Iterator[Session]:
    """A session that is never used.

    `tests/backend` has no database on purpose. Every path exercised below -- a missing token,
    an invalid body, an exhausted rate limit -- is refused before the session is touched, so
    handing over a placeholder proves something real: that none of them reaches the database.
    """
    yield cast(Session, None)


@pytest.fixture
def app() -> FastAPI:
    """The real application, plus probe routes that fail in each handled way."""
    application = create_app(Settings(environment="test", secret_key=SECRET))
    application.dependency_overrides[get_db] = _stub_session

    @application.post(
        "/probe/limited", dependencies=[Depends(login_rate_limit)], response_model=None
    )
    def _limited() -> dict[str, bool]:
        """Carries the REAL login limiter, without login's database dependency."""
        return {"ok": True}

    @application.get("/probe/ok")
    def _ok() -> dict[str, str | None]:
        # Read the ContextVar from inside a SYNC endpoint, which FastAPI runs in the AnyIO
        # threadpool -- the case a naive thread-local would get wrong.
        return {"seen": current_request_id()}

    @application.get("/probe/conflict", response_model=None)
    def _conflict() -> NoReturn:
        raise ConflictError()

    @application.get("/probe/notfound", response_model=None)
    def _notfound() -> NoReturn:
        raise NotFoundError("nope")

    @application.get("/probe/forbidden", response_model=None)
    def _forbidden() -> NoReturn:
        raise ForbiddenError()

    @application.get("/probe/boom", response_model=None)
    def _boom() -> NoReturn:
        raise RuntimeError("deliberate")

    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # `raise_server_exceptions=False` so the 500 path renders a real response instead of
    # re-raising into the test.
    return TestClient(app, raise_server_exceptions=False)


def header_of(response: httpx.Response) -> str:
    return response.headers[REQUEST_ID_HEADER]


# ======================================================================================
# Generation and validation
# ======================================================================================


def test_an_absent_header_produces_a_uuid4_hex(client: TestClient) -> None:
    response = client.get("/probe/ok")

    assert UUID4_HEX.fullmatch(header_of(response)), header_of(response)


def test_new_request_id_is_a_hex_string_not_a_uuid_object() -> None:
    """A string, so nothing downstream has to call str() and no two call sites can disagree
    about the punctuation."""
    value = new_request_id()

    assert isinstance(value, str)
    assert UUID4_HEX.fullmatch(value)


def test_every_request_gets_a_different_id(client: TestClient) -> None:
    ids = {header_of(client.get("/probe/ok")) for _ in range(20)}

    assert len(ids) == 20


def test_a_valid_supplied_id_is_used_and_returned(client: TestClient) -> None:
    response = client.get("/probe/ok", headers={"X-Request-ID": "abc123"})

    assert header_of(response) == "abc123"
    assert response.json()["seen"] == "abc123"


@pytest.mark.parametrize(
    "supplied",
    ["a", "A1", "with.dots", "with_underscores", "with-dashes", "Mixed.Case_1-2", "x" * 64],
    ids=["single", "alnum", "dots", "underscores", "dashes", "mixed", "max-length"],
)
def test_the_accepted_shapes_are_passed_through(client: TestClient, supplied: str) -> None:
    assert header_of(client.get("/probe/ok", headers={"X-Request-ID": supplied})) == supplied


@pytest.mark.parametrize(
    ("label", "supplied"),
    [
        ("empty", ""),
        ("oversized", "x" * 65),
        ("newline", "abc\ndef"),
        ("trailing-newline", "abc\n"),
        ("carriage-return", "abc\rdef"),
        ("crlf-log-injection", "abc\r\nERROR forged log line"),
        ("space", "abc def"),
        ("colon", "abc:def"),
        ("slash", "abc/def"),
        ("percent", "abc%sdef"),
        ("brace", "abc{def}"),
        ("null", "abc\x00def"),
        ("tab", "abc\tdef"),
    ],
)
def test_an_unacceptable_id_is_replaced_and_never_echoed(
    client: TestClient, label: str, supplied: str
) -> None:
    """Replaced silently -- never a 400. A correlation header must not be able to fail a
    request. And the rejected bytes appear nowhere in the response."""
    response = client.get("/probe/ok", headers={"X-Request-ID": supplied})

    effective = header_of(response)
    assert response.status_code == 200, f"{label} turned a correlation header into a failure"
    assert UUID4_HEX.fullmatch(effective), f"{label} was not replaced with a fresh id"
    assert effective != supplied
    assert supplied.strip() not in response.text or not supplied.strip()


def test_a_trailing_newline_is_not_accepted_by_the_pattern() -> None:
    """The one that a `$`-anchored regex would wave through.

    In Python, `$` also matches immediately before a trailing newline, so `^[A-Za-z0-9._-]+$`
    ACCEPTS "abc\\n" -- precisely the log injection this validation exists to stop. The
    implementation uses `fullmatch`, and this test fails the day somebody "simplifies" it.
    """
    assert sanitise_request_id("abc\n") != "abc\n"
    assert UUID4_HEX.fullmatch(sanitise_request_id("abc\n"))


def test_sanitise_accepts_none() -> None:
    assert UUID4_HEX.fullmatch(sanitise_request_id(None))


def test_a_non_ascii_header_is_read_as_absent() -> None:
    """Bytes that are not ASCII cannot be a valid id, and must not raise on decode.

    Exercised against the scope reader rather than through TestClient: httpx refuses to send a
    non-latin-1 header at all, so the client would never let such a request reach the
    middleware -- but a raw socket would.
    """
    scope = {"type": "http", "headers": [(b"x-request-id", b"caf\xe9")]}

    assert _supplied_header(scope) is None
    assert UUID4_HEX.fullmatch(sanitise_request_id(_supplied_header(scope)))


# ======================================================================================
# Every response carries it
# ======================================================================================


@pytest.mark.parametrize(
    ("expected_status", "method", "path", "body"),
    [
        (200, "GET", "/api/v1/", None),
        (401, "GET", "/api/v1/auth/me", None),
        (403, "GET", "/probe/forbidden", None),
        (404, "GET", "/probe/notfound", None),
        (404, "GET", "/api/v1/no-such-route", None),
        (409, "GET", "/probe/conflict", None),
        (422, "POST", "/api/v1/auth/login", {}),
        (500, "GET", "/probe/boom", None),
    ],
    ids=["200", "401", "403", "404-handler", "404-unrouted", "409", "422", "500"],
)
def test_the_header_is_present_on_every_status_class(
    client: TestClient, expected_status: int, method: str, path: str, body: dict[str, str] | None
) -> None:
    response = client.request(method, path, json=body)

    assert response.status_code == expected_status
    assert UUID4_HEX.fullmatch(header_of(response))


def test_the_500_header_survives_the_contextvar_being_unbound(client: TestClient) -> None:
    """The 500 is the one response produced OUTSIDE the middleware.

    Starlette routes the `Exception` handler to ServerErrorMiddleware, which wraps every user
    middleware -- so by the time it runs, the middleware's `finally` has already unbound the
    ContextVar. The id reaches it through the ASGI scope instead. Measured before it was
    written: without that, the 500 was the only response with no header.
    """
    response = client.get("/probe/boom", headers={"X-Request-ID": "traceme-1"})

    assert response.status_code == 500
    assert header_of(response) == "traceme-1"


def test_the_error_body_is_untouched(client: TestClient) -> None:
    """The id lives in the header. The JSON envelope three stages have pinned is unchanged."""
    body = client.get("/probe/conflict").json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert "request_id" not in body["error"]
    assert "request_id" not in client.get("/probe/boom").json()["error"]


# ======================================================================================
# Context isolation
# ======================================================================================


def test_one_requests_id_never_appears_in_another(client: TestClient) -> None:
    first = client.get("/probe/ok", headers={"X-Request-ID": "request-A"})
    second = client.get("/probe/ok", headers={"X-Request-ID": "request-B"})

    assert first.json()["seen"] == "request-A"
    assert second.json()["seen"] == "request-B"
    assert header_of(second) == "request-B"


def test_the_contextvar_is_unbound_after_a_request(client: TestClient) -> None:
    client.get("/probe/ok", headers={"X-Request-ID": "leaky"})

    assert current_request_id() is None


def test_the_contextvar_is_unbound_after_a_failing_request(client: TestClient) -> None:
    """The `finally` has to run on the exception path too, or a worker serves the next
    request under the previous one's id."""
    client.get("/probe/boom", headers={"X-Request-ID": "exploded"})

    assert current_request_id() is None


def test_a_generated_id_does_not_leak_either(client: TestClient) -> None:
    client.get("/probe/ok")

    assert current_request_id() is None


def test_outside_a_request_there_is_no_id() -> None:
    assert current_request_id() is None


def test_a_sync_endpoint_sees_the_id(client: TestClient) -> None:
    """`/probe/ok` is a sync endpoint, so FastAPI runs it in the AnyIO threadpool. A
    thread-local would return nothing here; a ContextVar is copied into the worker."""
    assert client.get("/probe/ok", headers={"X-Request-ID": "sync-1"}).json()["seen"] == "sync-1"


def test_non_http_scopes_pass_through_untouched() -> None:
    """Lifespan has no request to correlate and no headers to write."""
    seen: list[str] = []

    async def inner(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(str(scope["type"]))

    middleware = RequestIdMiddleware(inner)  # type: ignore[arg-type]

    import anyio

    anyio.run(middleware, {"type": "lifespan"}, None, None)  # type: ignore[arg-type]

    assert seen == ["lifespan"]
    assert current_request_id() is None


# ======================================================================================
# The id carries no authority
# ======================================================================================


def test_a_request_id_does_not_authenticate(client: TestClient) -> None:
    """It is not a credential. A protected route is still 401 however plausible the id."""
    response = client.get("/api/v1/auth/me", headers={"X-Request-ID": "administrator"})

    assert response.status_code == 401


def test_a_request_id_does_not_select_a_hotel(client: TestClient) -> None:
    """The hotel comes from the URL and the membership behind it, never from a header."""
    response = client.get(
        "/api/v1/hotels/00000000-0000-0000-0000-000000000001",
        headers={"X-Request-ID": "owner-of-everything"},
    )

    assert response.status_code == 401, "authentication still runs first"


def test_a_request_id_does_not_change_rate_limit_identity(app: FastAPI) -> None:
    """The limiter keys on the peer address. Varying the id must not mint a fresh budget --
    otherwise a header would undo Stage 4.5.3 entirely.
    """
    caller = TestClient(app, client=("198.51.100.7", 40000))

    codes = [
        caller.post("/probe/limited", headers={"X-Request-ID": f"attempt-{n}"}).status_code
        for n in range(7)
    ]

    assert codes[:5] == [200] * 5
    assert 429 in codes[5:], "a changing request id reset the rate-limit bucket"


def test_the_request_id_reaches_no_database_lookup(client: TestClient) -> None:
    """A syntactically valid id shaped like a public identifier still resolves nothing."""
    response = client.get(
        "/api/v1/auth/me", headers={"X-Request-ID": "00000000-0000-0000-0000-000000000001"}
    )

    assert response.status_code == 401


# ======================================================================================
# What reaches a log record
# ======================================================================================


def test_a_log_record_inside_a_request_carries_the_effective_id(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    from app.core.logging import RequestIdFilter

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    handler.addFilter(RequestIdFilter())
    logger = logging.getLogger("app.probe.request_id")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    @app.get("/probe/log")
    def _log() -> dict[str, bool]:
        logger.info("probe")
        return {"ok": True}

    try:
        TestClient(app).get("/probe/log", headers={"X-Request-ID": "correlate-me"})
    finally:
        logger.removeHandler(handler)

    assert [r.request_id for r in records] == ["correlate-me"]  # type: ignore[attr-defined]


def test_a_rejected_id_never_reaches_a_log_record(
    app: FastAPI,
) -> None:
    """The effective id is logged; the attacker's bytes are dropped at the door."""
    from app.core.logging import RequestIdFilter

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    handler.addFilter(RequestIdFilter())
    logger = logging.getLogger("app.probe.rejected")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    @app.get("/probe/log-rejected")
    def _log() -> dict[str, bool]:
        logger.info("probe")
        return {"ok": True}

    try:
        TestClient(app).get(
            "/probe/log-rejected", headers={"X-Request-ID": "bad\nFORGED ERROR line"}
        )
    finally:
        logger.removeHandler(handler)

    assert len(records) == 1
    stamped = records[0].request_id  # type: ignore[attr-defined]
    assert "FORGED" not in stamped
    assert "\n" not in stamped
    assert UUID4_HEX.fullmatch(stamped)


def test_a_record_outside_a_request_gets_the_placeholder() -> None:
    from app.core.logging import RequestIdFilter

    record = logging.LogRecord("app.probe", logging.INFO, __file__, 1, "m", None, None)

    assert RequestIdFilter().filter(record) is True
    assert record.request_id == NO_REQUEST_ID  # type: ignore[attr-defined]
