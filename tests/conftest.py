"""Shared pytest fixtures.

``backend/`` is placed on ``sys.path`` by ``pythonpath`` in the root ``pyproject.toml``,
which is what lets these root-level tests import ``app``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Explicit test settings, independent of any ``.env`` on the machine."""
    return Settings(environment="test", debug=True)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A TestClient bound to an application built from the test settings."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client
