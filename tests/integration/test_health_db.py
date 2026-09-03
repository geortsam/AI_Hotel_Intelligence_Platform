"""Database readiness against real PostgreSQL.

The unavailable path is covered in ``tests/backend/test_health_endpoints.py`` with a stub;
this file covers the path that can only be proven for real -- that the probe executes an
actual statement against PostgreSQL and reports success. SQLite is not substituted.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_db
from app.core.config import Settings
from app.main import create_app
from app.repositories.health import HealthRepository
from app.services.health import HealthService
from tests.integration.conftest import requires_postgres

pytestmark = requires_postgres


def client_for(engine: Engine) -> TestClient:
    """A TestClient whose database dependency is bound to the live test engine."""
    app = create_app(Settings(environment="test"))
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def override() -> object:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app)


# --- the repository actually reaches PostgreSQL -------------------------------------------


def test_repository_probe_executes_against_postgresql(session: Session) -> None:
    assert HealthRepository(session).ping() == 1


def test_probe_runs_on_the_real_server_not_a_cached_value(session: Session) -> None:
    """Confirms the round trip is genuine by asking the server something only it knows."""
    backend_pid = session.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
    version = session.execute(sa.text("SHOW server_version")).scalar_one()

    assert isinstance(backend_pid, int) and backend_pid > 0
    assert str(version).startswith("18")


def test_service_reports_reachable_against_a_live_database(session: Session) -> None:
    result = HealthService(HealthRepository(session)).database_status()

    assert result.status == "ok"
    assert result.database == "reachable"
    assert result.application == "running"
    assert result.latency_ms is not None
    assert result.latency_ms >= 0


# --- the endpoint end to end ----------------------------------------------------------------


def test_health_db_returns_200_when_postgresql_is_reachable(engine: Engine) -> None:
    response = client_for(engine).get("/health/db")

    assert response.status_code == 200


def test_health_db_payload_reports_a_reachable_database(engine: Engine) -> None:
    payload = client_for(engine).get("/health/db").json()

    assert payload["status"] == "ok"
    assert payload["application"] == "running"
    assert payload["database"] == "reachable"
    assert payload["error_code"] is None
    assert isinstance(payload["latency_ms"], float)


def test_health_db_exposes_no_connection_details_when_healthy(engine: Engine) -> None:
    """Success responses leak just as easily as failures if nobody checks."""
    body = client_for(engine).get("/health/db").text.lower()

    for secret in ["postgresql://", "postgres:", "5432", "localhost", "dsn", "password"]:
        assert secret not in body, f"leaked {secret!r}"


def test_liveness_and_readiness_agree_when_everything_is_up(engine: Engine) -> None:
    client = client_for(engine)

    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/health/db").json()["status"] == "ok"
