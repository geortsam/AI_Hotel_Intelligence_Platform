"""The database session dependency: lifecycle, rollback and single-pool guarantees.

Session behaviour is asserted with a recording stub, so the lifecycle contract is checked
exactly -- which calls happen, in which order, on both the success and failure paths. A real
connection cannot demonstrate that ``close()`` runs even when the body raises.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.api import deps
from app.db import session as session_module


class RecordingSession:
    """Records the lifecycle calls made against it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def rollback(self) -> None:
        self.calls.append("rollback")

    def commit(self) -> None:
        self.calls.append("commit")

    def close(self) -> None:
        self.calls.append("close")


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingSession:
    """Make get_db yield a recording session instead of a real one."""
    stub = RecordingSession()
    monkeypatch.setattr(deps, "get_session_factory", lambda: lambda: stub)
    return stub


# --- lifecycle -------------------------------------------------------------------------


def test_session_is_closed_on_the_success_path(recorded: RecordingSession) -> None:
    generator = deps.get_db()
    next(generator)
    with pytest.raises(StopIteration):
        next(generator)

    assert recorded.calls == ["close"]


def test_dependency_does_not_commit(recorded: RecordingSession) -> None:
    """Transaction boundaries belong to services. Committing here would persist
    half-finished work whenever a request happened to end without raising."""
    generator = deps.get_db()
    next(generator)
    with pytest.raises(StopIteration):
        next(generator)

    assert "commit" not in recorded.calls


def test_session_is_rolled_back_then_closed_on_failure(recorded: RecordingSession) -> None:
    """Order matters: a failed request must not leave a transaction holding locks."""
    generator = deps.get_db()
    next(generator)

    with pytest.raises(ValueError):
        generator.throw(ValueError("endpoint blew up"))

    assert recorded.calls == ["rollback", "close"]


def test_the_original_exception_still_propagates(recorded: RecordingSession) -> None:
    """Cleanup must not swallow the error that caused it."""
    generator = deps.get_db()
    next(generator)

    with pytest.raises(ValueError, match="endpoint blew up"):
        generator.throw(ValueError("endpoint blew up"))


# --- typing and wiring -------------------------------------------------------------------


def test_get_db_is_a_generator_dependency_yielding_a_session(
    recorded: RecordingSession,
) -> None:
    """FastAPI relies on the generator protocol to run teardown after the response."""
    import inspect

    assert inspect.isgeneratorfunction(deps.get_db)

    generator = deps.get_db()
    assert next(generator) is recorded


def test_db_session_alias_is_typed_as_a_session() -> None:
    from typing import get_args

    assert get_args(deps.DbSession)[0] is Session


def test_health_service_dependency_is_assembled_over_the_request_session() -> None:
    """Router -> service -> repository, with the session injected at the bottom."""
    from app.repositories.health import HealthRepository
    from app.services.health import HealthService

    stub: Any = RecordingSession()
    service = deps.get_health_service(stub)

    assert isinstance(service, HealthService)
    assert isinstance(service._repository, HealthRepository)


# --- one pool per process ------------------------------------------------------------------


def test_engine_and_session_factory_are_cached_singletons() -> None:
    """Connection logic is not duplicated: one engine, one pool, built once."""
    assert session_module.get_engine.cache_info().maxsize == 1
    assert session_module.get_session_factory.cache_info().maxsize == 1


def test_building_the_app_does_not_open_a_database_connection() -> None:
    """The engine is lazy on purpose: the process must be able to start and *report* that
    the database is down, which it could not do if booting required a live connection."""
    from app.core.config import Settings
    from app.main import create_app

    session_module.dispose_engine()
    assert session_module.get_engine.cache_info().currsize == 0

    create_app(Settings(environment="test"))

    assert session_module.get_engine.cache_info().currsize == 0


def test_dispose_engine_clears_the_cache() -> None:
    session_module.dispose_engine()

    assert session_module.get_engine.cache_info().currsize == 0
    assert session_module.get_session_factory.cache_info().currsize == 0
