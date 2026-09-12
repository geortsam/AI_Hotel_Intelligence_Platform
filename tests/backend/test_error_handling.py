"""Error envelope, and the leak boundary.

Every handler is exercised through a real request against a throwaway app carrying routes that
raise on purpose. Testing the handlers by calling them directly would prove they build the
right object but not that FastAPI actually routes exceptions to them.
"""

from __future__ import annotations

from typing import NoReturn

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.main import create_app

SECRET = "hunter2"
DSN = "postgresql://postgres:hunter2@db.internal:5432/hotel_intelligence"


class Payload(BaseModel):
    name: str
    nights: int


@pytest.fixture
def error_client() -> TestClient:
    """An app with routes that fail in each of the ways the handlers cover."""
    app = create_app(Settings(environment="test"))

    @app.get("/boom/app-error", response_model=None)
    def _app_error() -> NoReturn:
        raise NotFoundError("Hotel 42 does not exist.")

    @app.get("/boom/conflict", response_model=None)
    def _conflict() -> NoReturn:
        raise ConflictError()

    @app.get("/boom/custom-code", response_model=None)
    def _custom() -> NoReturn:
        raise AppError("Deliberate failure.", code="CUSTOM_CODE")

    @app.get("/boom/database", response_model=None)
    def _database() -> NoReturn:
        raise OperationalError("SELECT * FROM hotels", {}, Exception(f"dsn: {DSN}"))

    @app.get("/boom/unexpected", response_model=None)
    def _unexpected() -> NoReturn:
        raise RuntimeError(f"internal detail with {SECRET} at C:/Users/example/secret.py")

    @app.post("/boom/validate")
    def _validate(payload: Payload) -> dict[str, str]:
        return {"ok": payload.name}

    return TestClient(app, raise_server_exceptions=False)


# --- envelope shape ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_code"),
    [
        ("/boom/app-error", 404, "NOT_FOUND"),
        ("/boom/conflict", 409, "CONFLICT"),
        ("/boom/custom-code", 500, "CUSTOM_CODE"),
        ("/boom/database", 503, "DATABASE_UNAVAILABLE"),
        ("/boom/unexpected", 500, "INTERNAL_ERROR"),
        ("/no-such-route", 404, "NOT_FOUND"),
    ],
)
def test_every_failure_uses_the_same_envelope(
    error_client: TestClient, path: str, expected_status: int, expected_code: str
) -> None:
    response = error_client.get(path)
    body = response.json()

    assert response.status_code == expected_status
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == expected_code
    assert isinstance(body["error"]["message"], str)


def test_app_error_message_is_returned_because_it_was_written_for_clients(
    error_client: TestClient,
) -> None:
    assert error_client.get("/boom/app-error").json()["error"]["message"] == (
        "Hotel 42 does not exist."
    )


# --- validation ---------------------------------------------------------------------------


def test_validation_error_returns_422_with_field_detail(error_client: TestClient) -> None:
    response = error_client.post("/boom/validate", json={"nights": "many"})
    body = response.json()

    assert response.status_code == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"
    locations = {tuple(d["location"]) for d in body["error"]["details"]}
    assert ("body", "name") in locations  # missing
    assert ("body", "nights") in locations  # wrong type


def test_validation_detail_does_not_echo_the_submitted_value(
    error_client: TestClient,
) -> None:
    """A rejected payload may contain a password; echoing it back would write the secret
    into client logs and error trackers."""
    response = error_client.post("/boom/validate", json={"name": "ok", "nights": SECRET})
    body = response.json()

    assert response.status_code == 422
    assert SECRET not in response.text
    for detail in body["error"]["details"]:
        assert set(detail) == {"location", "message", "type"}


# --- the leak boundary --------------------------------------------------------------------


def test_database_error_leaks_no_sql_dsn_or_password(error_client: TestClient) -> None:
    text = error_client.get("/boom/database").text.lower()

    for leak in [SECRET, "postgresql://", "db.internal", "select", "hotels", "dsn"]:
        assert leak not in text, f"leaked {leak!r}"


def test_unexpected_error_leaks_no_message_path_or_traceback(
    error_client: TestClient,
) -> None:
    text = error_client.get("/boom/unexpected").text

    for leak in [SECRET, "C:/Users", "secret.py", "Traceback", "RuntimeError"]:
        assert leak not in text, f"leaked {leak!r}"


def test_unexpected_error_message_is_a_fixed_sentence(error_client: TestClient) -> None:
    message = error_client.get("/boom/unexpected").json()["error"]["message"]

    assert message == "An internal error occurred. The incident has been logged."


def test_internal_errors_are_still_logged_in_full(
    error_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Suppressed for the client, preserved for the operator -- otherwise the incident is
    simply lost."""
    with caplog.at_level("ERROR"):
        error_client.get("/boom/unexpected")

    assert any(SECRET in record.getMessage() or record.exc_info for record in caplog.records)
