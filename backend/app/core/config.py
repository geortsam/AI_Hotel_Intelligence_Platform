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
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.client_address import parse_trusted_proxy

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
    #
    # `NoDecode` is what makes that sentence true, and without it the field is unusable from
    # the environment at all. pydantic-settings treats a complex annotation -- `list[str]` is
    # one -- as JSON, so `EnvSettingsSource` calls `json.loads` on the raw value BEFORE any
    # `mode="before"` validator runs. `http://localhost:5173` is not JSON, so the process dies
    # at import with `SettingsError: error parsing value for field "cors_origins"`, and so does
    # every other documented form including the one in .env.example. `NoDecode` suppresses that
    # decode step and hands `_split_origins` below the raw string it was written to accept.
    #
    # This escaped five stages of review because the init source does NOT decode:
    # `Settings(cors_origins="a,b")` -- which is how every test builds settings -- has always
    # worked. Only the environment path was broken, and that is the only path a deployment
    # uses. It surfaced as a crashed `migrate` container in CI run 35090204615.
    cors_origins: Annotated[list[str], NoDecode] = Field(
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

    # --- Trusted proxies (Stage 4.5.6) -------------------------------------
    #: IP addresses or CIDR blocks of reverse proxies whose forwarding headers may be read.
    #: Comma-separated in the environment, a list once parsed.
    #:
    #: **Empty by default, and that default is the safe one.** While this is empty no
    #: forwarding header is consulted at all, so no client can influence its own address by
    #: sending one. Nothing is implicitly trusted -- not 127.0.0.1, not a private range --
    #: because "the proxy is on loopback" is a deployment fact this application cannot verify
    #: and must not assume. See `app.core.client_address` for the resolution algorithm.
    #:
    #: `NoDecode` for the same reason as ``cors_origins`` above: without it the environment
    #: source JSON-decodes this field before ``_split_trusted_proxies`` can split it, so
    #: ``TRUSTED_PROXIES=10.0.0.0/8,192.168.1.5`` -- the form this file documents and
    #: .env.example prints -- raises instead of parsing. A deployment behind a proxy could
    #: therefore never configure the one setting that makes rate limiting and audit attribution
    #: correct.
    trusted_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Audit retention (Stage 4.5.14) ------------------------------------
    #: How long an audit event stays OUT of the archive, in days.
    #:
    #: **Two years, and the default is deliberately conservative.** Retention here does not
    #: mean deletion -- nothing is ever deleted from ``audit_events`` (see
    #: :mod:`app.services.retention`) -- so the only cost of a long window is that the archive
    #: fills later. The cost of a short one is that recent evidence is copied out of the table
    #: the read APIs serve while an incident is still being investigated. Erring long is the
    #: safe direction, and two years covers the usual commercial dispute window.
    #:
    #: A floor of one day, so a misconfigured ``0`` cannot make every event written this
    #: second immediately eligible.
    audit_retention_days: int = Field(default=730, ge=1)

    #: How many events one archival batch may copy.
    #:
    #: The archival job is bounded by this and loops; it never attempts the whole table in one
    #: statement, and it never loads the eligible set into Python. A thousand rows is a short
    #: transaction on any hardware this runs on, which matters because the batch holds row
    #: locks on the rows it is inserting.
    audit_archive_batch_size: int = Field(default=1_000, ge=1, le=10_000)

    #: The most batches one invocation of the job may run.
    #:
    #: A stop condition that does not depend on the data: without it, a first run against a
    #: large backlog would hold a process for an unbounded time, and an operator would have no
    #: way to say "make progress, then let me look". The job reports whether more work remains.
    audit_archive_max_batches: int = Field(default=100, ge=1)

    # --- Security headers (Stage 4.5.6) ------------------------------------
    #: HSTS is sent only in production, and only when the ORIGINAL client spoke HTTPS. One
    #: year is the interval the preload list requires and the usual recommendation.
    hsts_max_age: int = Field(default=31_536_000, ge=0)
    hsts_include_subdomains: bool = True
    #: Opt-in, and deliberately off. Preloading is close to irreversible -- removal takes
    #: months to reach users -- so it is a decision a deployment makes on purpose.
    hsts_preload: bool = False

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept ``a,b,c`` from the environment as well as a real list."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_trusted_proxies(cls, value: object) -> object:
        """Accept ``a,b,c`` from the environment as well as a real list."""
        if isinstance(value, str):
            return [entry.strip() for entry in value.split(",") if entry.strip()]
        return value

    @field_validator("trusted_proxies", mode="after")
    @classmethod
    def _validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        """Refuse to start with a proxy entry that is not a usable address or CIDR.

        Parsed here rather than at the first request so a typo is a startup failure with a
        message naming the entry, not a silently empty trust list that quietly turns every
        forwarded header back off in production.
        """
        for entry in value:
            parse_trusted_proxy(entry)
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
