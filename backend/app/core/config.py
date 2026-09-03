"""Application settings.

Configuration is read from the environment (optionally via a local ``.env``), never from
hard-coded literals, so the same image can run in development, test and production.

Every field has a safe default and nothing is required, so the application still starts with
no ``.env`` present. The database settings added in Stage 2B follow the same rule: with no
password configured, :attr:`Settings.sqlalchemy_url` returns ``None`` and the engine is simply
never built. ``secret_key`` remains declared but unread -- there is no authentication yet.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    """Environment-driven configuration for the platform."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application -------------------------------------------------------
    app_name: str = "AI Hotel Intelligence Platform"
    # Shown in the OpenAPI document and the interactive docs. Configurable because it is
    # deployment-facing prose; the code version is not (see app.__version__).
    app_description: str = "Operational and analytics API for the AI Hotel Intelligence Platform."
    environment: Environment = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # --- Observability -----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- CORS --------------------------------------------------------------
    # Comma-separated in the environment, a list once parsed.
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )

    # --- Database (Stage 2B) -----------------------------------------------
    # Either DATABASE_URL directly, or the POSTGRES_* parts below.
    database_url: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "hotel"
    postgres_password: str | None = None
    postgres_db: str = "hotel_intelligence"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # --- Authentication (Stage 4.1) ----------------------------------------
    #: Signs and verifies access tokens. There is deliberately NO default: a fallback secret
    #: committed to source control means every deployment that forgets to set one shares a
    #: key an attacker can read. `require_secret` raises rather than inventing one, and
    #: `_secret_required_in_production` below refuses to start a production app without it.
    secret_key: str | None = None

    # --- Authentication rate limiting (Stage 4.5.3) ------------------------
    #: Requests per window, per client address, per endpoint. Centralised here rather than
    #: written into a route decorator so the policy is one thing to read and one thing to
    #: change -- and so a deployment can tighten it without a code change.
    #:
    #: These are REQUEST limits, not failed-attempt limits: counting only failures would let
    #: an attacker with one valid credential mask an attack behind successes, and counting
    #: per-account would make the limiter an enumeration oracle and a way to lock a known
    #: person out. See `app.api.deps.rate_limited`.
    auth_login_rate_limit: int = Field(default=5, ge=1)
    auth_login_rate_limit_window_seconds: int = Field(default=60, ge=1)

    #: The password-change endpoint is protected against flooding, not against guessing:
    #: it already requires a valid token AND the current password, so it is not a brute-force
    #: surface. The same conservative numbers apply because there is no reason for a human to
    #: change their password five times a minute.
    auth_change_password_rate_limit: int = Field(default=5, ge=1)
    auth_change_password_rate_limit_window_seconds: int = Field(default=60, ge=1)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept ``a,b,c`` from the environment as well as a real list."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _secret_required_in_production(self) -> Settings:
        """Refuse to construct a production configuration with no signing secret.

        Failing at startup is far better than failing at the first login, and immeasurably
        better than falling back to a built-in value. Non-production environments may omit
        it -- the tests supply their own -- but any token operation without one still raises.
        """
        if self.environment == "production" and not (self.secret_key or "").strip():
            raise ValueError(
                "SECRET_KEY must be set in production. Authentication cannot sign tokens "
                "without it, and this application will not fall back to a default."
            )
        return self

    @property
    def docs_enabled(self) -> bool:
        """Interactive API docs are exposed everywhere except production."""
        return self.environment != "production"

    @property
    def sqlalchemy_url(self) -> str | None:
        """The URL the engine should use.

        An explicit ``DATABASE_URL`` always wins -- that is what Docker Compose and most
        hosting providers set. Otherwise a URL is assembled from the POSTGRES_* parts, and
        only when a password is present: silently connecting without one hides a
        misconfiguration that should surface at startup.

        The ``postgresql+psycopg`` driver prefix selects psycopg 3 explicitly rather than
        letting SQLAlchemy default to psycopg2, which is not installed.
        """
        if self.database_url:
            return self.database_url
        if not self.postgres_password:
            return None
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
