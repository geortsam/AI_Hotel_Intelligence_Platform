"""Configuration behaviour that the rest of the platform will rely on."""

from __future__ import annotations

from app.core.config import Settings

#: A production Settings now requires a signing secret (Stage 4.1). Supplied here so
#: these tests can build one for reasons unrelated to authentication.
PRODUCTION_SECRET = "a-test-only-secret-not-used-for-anything"


def test_defaults_allow_startup_without_an_env_file() -> None:
    """A fresh checkout with no .env must still produce usable settings."""
    settings = Settings()

    assert settings.environment == "development"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.docs_enabled is True


def test_cors_origins_accepts_a_comma_separated_string() -> None:
    settings = Settings(cors_origins="http://a.test, http://b.test")

    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_docs_are_disabled_in_production() -> None:
    assert Settings(environment="production", secret_key=PRODUCTION_SECRET).docs_enabled is False
