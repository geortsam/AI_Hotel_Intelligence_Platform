"""Settings read from the ENVIRONMENT, which is the only path a deployment uses.

``test_config.py`` already covers construction in Python -- ``Settings(cors_origins="a,b")``.
That is the *init* source, and it was never the problem. pydantic-settings treats a complex
annotation such as ``list[str]`` as JSON and, in the *environment* source, calls ``json.loads``
on the raw value before any ``mode="before"`` validator runs. So a field could parse perfectly
from Python and be impossible to set from the environment at all -- which is exactly what
happened: ``CORS_ORIGINS=http://localhost:5173,http://localhost:3000``, the form printed in
.env.example, raised ``SettingsError`` and killed the process at import.

Nothing caught it, because no test had ever set these variables in the environment. The whole
suite built settings the one way that worked. The defect surfaced only when a container tried
to start: the ``migrate`` service in CI run 35090204615.

These tests exist to make that impossible to regress. Each one sets a real environment
variable and asserts on what ``Settings()`` produces, so they exercise
environment -> EnvSettingsSource -> validators, end to end.

``_env_file=None`` on every construction so a developer's local ``.env`` cannot influence the
result; the environment variables set here are the only input under test.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

#: The variables under test. Cleared before each case so an ambient value -- a developer's
#: shell, or a previous test -- cannot decide the outcome.
LIST_VARIABLES = ("CORS_ORIGINS", "TRUSTED_PROXIES")


@pytest.fixture(autouse=True)
def _clear_list_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LIST_VARIABLES:
        monkeypatch.delenv(name, raising=False)


# --- CORS_ORIGINS ----------------------------------------------------------------------


def test_cors_origins_reads_a_single_value_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173")

    assert Settings(_env_file=None).cors_origins == ["http://localhost:5173"]


def test_cors_origins_reads_the_documented_comma_separated_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact value .env.example prints. This is the one that used to raise."""
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000")

    assert Settings(_env_file=None).cors_origins == [
        "http://localhost:5173",
        "http://localhost:3000",
    ]


def test_an_explicitly_empty_cors_origins_means_no_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty is a decision, not an omission: it allows no origin at all.

    Deliberately NOT the unset default. Someone who writes ``CORS_ORIGINS=`` has said which
    origins may call this API -- none -- and falling back to localhost would quietly grant
    two that were not asked for.
    """
    monkeypatch.setenv("CORS_ORIGINS", "")

    assert Settings(_env_file=None).cors_origins == []


def test_cors_origins_tolerates_whitespace_around_the_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORS_ORIGINS", " http://a.test , http://b.test ")

    assert Settings(_env_file=None).cors_origins == ["http://a.test", "http://b.test"]


# --- TRUSTED_PROXIES -------------------------------------------------------------------


def test_trusted_proxies_reads_a_single_value_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Compose network subnet, which is what a deployment behind nginx actually sets."""
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")

    assert Settings(_env_file=None).trusted_proxies == ["172.18.0.0/16"]


def test_trusted_proxies_reads_the_documented_comma_separated_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact value .env.example prints."""
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8,192.168.1.5")

    assert Settings(_env_file=None).trusted_proxies == ["10.0.0.0/8", "192.168.1.5"]


def test_an_explicitly_empty_trusted_proxies_trusts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty is the safe default and stays the safe default: no forwarding header is read."""
    monkeypatch.setenv("TRUSTED_PROXIES", "")

    assert Settings(_env_file=None).trusted_proxies == []


def test_a_malformed_trusted_proxy_is_still_refused_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation is not what was switched off.

    ``NoDecode`` suppresses JSON decoding only; every validator still runs. Without this
    test, a future change that reached the same parsing result by loosening the field would
    look identical from the outside -- and a typo in TRUSTED_PROXIES would become a silently
    empty trust list rather than a startup failure.
    """
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8,not-an-address")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- nothing else moved ----------------------------------------------------------------


def test_unset_list_variables_still_produce_their_defaults() -> None:
    """The fix changes how a value is read, never what happens when there is none."""
    settings = Settings(_env_file=None)

    assert settings.cors_origins == ["http://localhost:5173", "http://localhost:3000"]
    assert settings.trusted_proxies == []


def test_scalar_settings_are_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scalars were never JSON-decoded, and must keep reading from the environment as before."""
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("API_V1_PREFIX", "/api/v1")

    settings = Settings(_env_file=None)

    assert settings.environment == "staging"
    assert settings.log_level == "WARNING"
    assert settings.api_v1_prefix == "/api/v1"


def test_construction_in_python_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The init path that always worked must keep working, and must still win over the
    environment -- an explicit argument is a stronger statement than an ambient variable."""
    monkeypatch.setenv("CORS_ORIGINS", "http://from-env.test")

    settings = Settings(_env_file=None, cors_origins="http://a.test, http://b.test")

    assert settings.cors_origins == ["http://a.test", "http://b.test"]
