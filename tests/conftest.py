"""Shared pytest fixtures.

``backend/`` is placed on ``sys.path`` by ``pythonpath`` in the root ``pyproject.toml``,
which is what lets these root-level tests import ``app``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import PROVIDER_HEADERS_VARIABLE, Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _no_ambient_provider_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts without `ANTHROPIC_CUSTOM_HEADERS` (R2.7I).

    An enabled copilot refuses to load while it is set, so an ambient value -- another tool in
    the developer's shell -- would fail every test that builds an enabled configuration, for a
    reason unrelated to the test. Removed unread; a test of the refusal sets its own.
    """
    monkeypatch.delenv(PROVIDER_HEADERS_VARIABLE, raising=False)


@pytest.fixture
def settings() -> Settings:
    """Explicit test settings, independent of any ``.env`` on the machine."""
    return Settings(environment="test", debug=True)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A TestClient bound to an application built from the test settings."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client
