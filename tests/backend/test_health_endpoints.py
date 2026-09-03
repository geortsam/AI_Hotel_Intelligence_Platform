"""Health and readiness endpoints, including the database-unavailable path.

The failure cases here use a stub session rather than a real database: the point is to prove
the *endpoint* behaves correctly when the database misbehaves, which is impossible to arrange
reliably against a healthy server. The success path against real PostgreSQL lives in
``tests/integration/test_health_db.py``; the two are complementary, and neither substitutes
SQLite for PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import __version__
from app.api.deps import get_db
from app.core.config import Settings
from app.main import create_app


class _BrokenSession:
    """Stands in for a Session whose connection is gone."""

    def execute(self, *_: Any, **__: Any) -> NoReturn:
        # The message deliberately carries content that must NOT reach the client: a
        # connection string, a password and a host.
        raise OperationalError(
            "SELECT 1",
            {},
            Exception(
                "connection to server at 127.0.0.1 port 5432 failed: "
                "password authentication failed for user 'postgres' "
                "(dsn: postgresql://postgres:hunter2@127.0.0.1:5432/hotel_intelligence)"
            ),
        )

    def rollback(self) -> None: ...

    def close(self) -> None: ...


@pytest.fixture
def broken_db_client() -> TestClient:
    """A client whose database dependency yields a session that always fails."""
    app = create_app(Settings(environment="test"))

    def override_get_db() -> Iterator[_BrokenSession]:
        yield _BrokenSession()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


# --- liveness --------------------------------------------------------------------------


def test_health_returns_200(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_health_reports_process_facts(client: TestClient) -> None:
    payload = client.get("/health").json()

    assert payload["status"] == "ok"
    assert payload["environment"] == "test"
    assert payload["version"] == __version__


def test_health_consults_no_dependency(broken_db_client: TestClient) -> None:
    """Liveness must stay green while the database is down, or an orchestrator will restart
    a perfectly healthy process over a fault it cannot fix."""
    response = broken_db_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_matches_its_response_model(client: TestClient) -> None:
    assert set(client.get("/health").json()) == {"status", "app", "version", "environment"}


# --- readiness: database unavailable ----------------------------------------------------


def test_health_db_returns_503_when_the_database_is_unreachable(
    broken_db_client: TestClient,
) -> None:
    response = broken_db_client.get("/health/db")

    assert response.status_code == 503


def test_health_db_distinguishes_a_running_app_from_a_dead_database(
    broken_db_client: TestClient,
) -> None:
    """The whole purpose of the endpoint: two separate facts, separately reported."""
    payload = broken_db_client.get("/health/db").json()

    assert payload["application"] == "running"
    assert payload["database"] == "unreachable"
    assert payload["status"] == "degraded"
    assert payload["latency_ms"] is None
    assert payload["error_code"] == "DATABASE_UNAVAILABLE"


def test_health_db_leaks_no_credentials_or_connection_details(
    broken_db_client: TestClient,
) -> None:
    """The driver error carries a password, a DSN and a host. None may reach the client."""
    body = broken_db_client.get("/health/db").text.lower()

    for secret in ["hunter2", "postgresql://", "dsn", "password authentication", "5432"]:
        assert secret not in body, f"leaked {secret!r}"


def test_health_db_leaks_no_sql_or_stack_trace(broken_db_client: TestClient) -> None:
    body = broken_db_client.get("/health/db").text.lower()

    for leak in ["select 1", "traceback", "sqlalchemy", "operationalerror", "site-packages"]:
        assert leak not in body, f"leaked {leak!r}"
