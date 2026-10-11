"""The request-body cap (`app.middleware.request_size`), through the real application factory.

No database: the probe route is added to an application built by `create_app`, so the refusal
is checked with every real middleware and exception handler around it. The streamed-body case
drives the ASGI application directly, because a test client may buffer a body and declare its
length, which would test the other path twice.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message

from app.core.config import Settings
from app.main import create_app
from app.middleware.request_size import _declared_length
from app.schemas.knowledge import MAX_CONTENT_LENGTH, MAX_SOURCE_LENGTH, MAX_TITLE_LENGTH

SECRET = "request-size-suite-signing-secret-not-real"
CAP = 1024
ORIGIN = "http://localhost:5173"


class Probe:
    """A route that records whether it ran and echoes the size of what it was given."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, payload: dict[str, Any]) -> dict[str, int]:
        self.calls += 1
        return {"characters": len(payload.get("text", ""))}


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest.fixture
def app(probe: Probe) -> FastAPI:
    application = create_app(
        Settings(
            environment="test",
            secret_key=SECRET,
            max_request_body_bytes=CAP,
            cors_origins=[ORIGIN],
        )
    )
    application.add_api_route("/probe", probe, methods=["POST"])
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def body_of(size: int) -> bytes:
    """A JSON body of exactly *size* bytes."""
    overhead = len(b'{"text":""}')
    return json.dumps({"text": "x" * (size - overhead)}, separators=(",", ":")).encode()


def assert_refused(status: int, payload: dict[str, Any]) -> None:
    assert status == 413
    assert payload["error"]["code"] == "REQUEST_TOO_LARGE"
    assert payload["error"]["message"] == "The request body is too large."


# --- a declared Content-Length -----------------------------------------------------------------


def test_a_body_at_the_cap_is_accepted(client: TestClient, probe: Probe) -> None:
    response = client.post(
        "/probe", content=body_of(CAP), headers={"content-type": "application/json"}
    )

    assert response.status_code == 200
    assert probe.calls == 1


def test_a_body_one_byte_over_the_cap_is_refused_before_the_application_runs(
    client: TestClient, probe: Probe
) -> None:
    response = client.post(
        "/probe", content=body_of(CAP + 1), headers={"content-type": "application/json"}
    )

    assert_refused(response.status_code, response.json())
    assert probe.calls == 0


def test_the_refusal_carries_the_headers_every_response_carries(client: TestClient) -> None:
    response = client.post(
        "/probe",
        content=body_of(CAP * 4),
        headers={"content-type": "application/json", "origin": ORIGIN},
    )

    assert response.status_code == 413
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_a_real_route_is_refused_before_authentication(client: TestClient) -> None:
    """The cap runs before routing: an oversized login is a 413, never a 401 or a 422."""
    response = client.post(
        "/api/v1/auth/login", content=body_of(CAP + 1), headers={"content-type": "application/json"}
    )

    assert_refused(response.status_code, response.json())


def test_a_request_without_a_body_is_untouched(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


# --- a streamed body, with no Content-Length ----------------------------------------------------


def stream(app: FastAPI, chunks: list[bytes]) -> tuple[int, dict[str, Any]]:
    """POST *chunks* to /probe as a chunked body, through the full ASGI stack."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/probe",
        "raw_path": b"/probe",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"transfer-encoding", b"chunked"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body)


def test_a_streamed_body_within_the_cap_is_accepted(app: FastAPI, probe: Probe) -> None:
    body = body_of(CAP)
    status, payload = stream(app, [body[:500], body[500:]])

    assert status == 200
    assert payload == {"characters": CAP - len(b'{"text":""}')}
    assert probe.calls == 1


def test_a_streamed_body_over_the_cap_is_refused_as_it_arrives(app: FastAPI, probe: Probe) -> None:
    body = body_of(CAP * 3)
    status, payload = stream(app, [body[:CAP], body[CAP : CAP * 2], body[CAP * 2 :]])

    assert_refused(status, payload)
    assert probe.calls == 0


# --- which Content-Length is trusted ------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([(b"content-length", b"42")], 42),
        ([(b"Content-Length", b"0")], 0),
        ([], None),
        ([(b"content-length", b"12abc")], None),
        ([(b"content-length", b"-5")], None),
        ([(b"content-length", b" 42")], None),
        ([(b"content-length", b"42"), (b"content-length", b"42")], None),
    ],
)
def test_only_a_single_plain_number_is_a_declared_length(
    headers: list[tuple[bytes, bytes]], expected: int | None
) -> None:
    assert _declared_length({"headers": headers}) == expected


# --- the default fits every legitimate body -----------------------------------------------------


def test_the_largest_possible_knowledge_upload_fits_under_the_default_cap() -> None:
    """The worst case: every character outside the Basic Multilingual Plane, JSON-encoded with
    ASCII escaping, so each one is a 12-byte surrogate pair -- and the longest title and source."""
    default = Settings.model_fields["max_request_body_bytes"].default
    upload = {
        "title": "\U0001f600" * MAX_TITLE_LENGTH,
        "source": "\U0001f600" * MAX_SOURCE_LENGTH,
        "language": "english",
        "content": "\U0001f600" * MAX_CONTENT_LENGTH,
        "contains_no_guest_personal_data": True,
    }

    escaped = json.dumps(upload, ensure_ascii=True).encode()
    raw = json.dumps(upload, ensure_ascii=False).encode()

    assert len(escaped) <= default
    assert len(raw) <= default
    assert default == 2 * 1024 * 1024


def test_the_cap_cannot_be_configured_below_a_kilobyte() -> None:
    with pytest.raises(ValueError, match="max_request_body_bytes"):
        Settings(environment="test", secret_key=SECRET, max_request_body_bytes=100)
