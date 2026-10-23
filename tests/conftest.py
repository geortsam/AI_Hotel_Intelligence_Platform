"""Shared pytest fixtures.

``backend/`` is placed on ``sys.path`` by ``pythonpath`` in the root ``pyproject.toml``,
which is what lets these root-level tests import ``app``.
"""

from __future__ import annotations

import os
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


#: Prefixes of the language-model settings (`app.core.config.Settings`): `LLM_*` and `OLLAMA_*`.
LLM_SETTING_PREFIXES = ("LLM_", "OLLAMA_")


@pytest.fixture(autouse=True)
def _no_ambient_llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts without `LLM_*` or `OLLAMA_*` in its environment (R2.7T).

    These are read by `Settings` from the process environment, so a value exported in the
    developer's shell -- `OLLAMA_NUM_CTX`, `LLM_MODEL`, another tool's `OLLAMA_BASE_URL` -- would
    otherwise decide what a test sees. Removed unread; a test that needs one sets its own.
    """
    for name in list(os.environ):
        if name.upper().startswith(LLM_SETTING_PREFIXES):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    """Explicit test settings, independent of any ``.env`` on the machine."""
    return Settings(environment="test", debug=True)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A TestClient bound to an application built from the test settings."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client
