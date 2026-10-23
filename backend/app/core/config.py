"""Application settings.

Configuration is read from the environment (optionally via a local ``.env``), never from
hard-coded literals, so the same image can run in development, test and production.

Every field has a safe default and nothing is required, so the application still starts with
no ``.env`` present. The database settings added in Stage 2B follow the same rule: with no
password configured, :attr:`Settings.sqlalchemy_url` returns ``None`` and the engine is simply
never built. ``secret_key`` signs and verifies authentication tokens
(:func:`app.core.security.require_secret`); production refuses to start without one.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.client_address import parse_trusted_proxy

Environment = Literal["development", "test", "staging", "production"]

#: A variable the provider SDK reads from the process environment on its own, whatever the
#: application passes, and merges into the headers of EVERY request -- after, and so over, the
#: application's own, the API key included. An enabled copilot refuses to load while it is set
#: (`Settings._no_provider_headers_from_the_environment`, R2.7I).
PROVIDER_HEADERS_VARIABLE = "ANTHROPIC_CUSTOM_HEADERS"

#: The refusal. Fixed text: the variable is checked for presence only, its value never read.
PROVIDER_HEADERS_REFUSAL = (
    "ANTHROPIC_CUSTOM_HEADERS is set in the environment. The provider SDK would add its headers "
    "to every provider request, so the copilot will not start with it set. Unset it, or set "
    "LLM_ENABLED=false to run without a provider."
)

#: The model each provider uses when `LLM_MODEL` is not set (`Settings.effective_llm_model`).
DEFAULT_MODELS: dict[str, str] = {"ollama": "qwen3.5:4b", "anthropic": "claude-sonnet-5"}

#: The only hosts an `OLLAMA_BASE_URL` may name: this machine (R2.7O).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Why `LLM_BASE_URL` was refused beside `LLM_PROVIDER=ollama`. Fixed text, value never echoed.
LLM_BASE_URL_WITH_OLLAMA_REFUSAL = (
    "LLM_BASE_URL configures the anthropic provider and is not used by ollama. Unset it, or set "
    "LLM_PROVIDER=anthropic."
)

#: Why an enabled copilot with an API key but no explicit `LLM_PROVIDER` was refused (R2.7V).
#: Fixed text: no configured value is ever echoed.
IMPLICIT_PROVIDER_REFUSAL = (
    "LLM_ENABLED is true and LLM_API_KEY is set, but LLM_PROVIDER is not. Set LLM_PROVIDER "
    "explicitly to ollama or anthropic: the default, ollama, does not use the key."
)

#: Why an `OLLAMA_BASE_URL` was refused. Fixed text: the value itself is never echoed.
INVALID_OLLAMA_URL_MESSAGE = (
    "OLLAMA_BASE_URL must be an http:// or https:// URL on this machine (127.0.0.1, localhost or "
    "[::1]) with no credentials, query, fragment or whitespace."
)


def is_loopback_url(value: object) -> bool:
    """Whether *value* is an absolute http(s) URL whose host is a loopback name.

    Refuses credentials (`user@`), a query or fragment (even an empty `?` or `#`), whitespace or
    control characters anywhere, and a malformed port. A path prefix and a port are allowed.
    Pure and side-effect free: the settings check it at load, the Ollama adapter again at
    construction.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return False
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a malformed or out-of-range port
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and parts.hostname in LOOPBACK_HOSTS
        and parts.username is None
        and parts.password is None
        and "?" not in value
        and "#" not in value
    )


class Settings(BaseSettings):
    """Environment-driven configuration for the platform."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # A refused configuration stops the process, and the error lands in a log. Pydantic
        # would otherwise print the settings input beside it -- secrets included -- so the
        # input is withheld; the message names the variable at fault, which is what helps.
        hide_input_in_errors=True,
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

    # --- Request body size ----------------------------------------------------
    #: The most bytes a request body may carry; more is `413 REQUEST_TOO_LARGE`, refused before
    #: the body is buffered (`app.middleware.request_size`). Every field also has its own
    #: schema bound, but a schema runs only after the whole body has been read.
    #:
    #: Sized from the largest legitimate body, a knowledge-document upload: at most 100,000
    #: characters of content (`app.schemas.knowledge.MAX_CONTENT_LENGTH`). JSON-encoded with
    #: ASCII escaping, a character outside the Basic Multilingual Plane is a 12-byte surrogate
    #: pair (`\ud83d\ude00`), so the worst case is about 1.2 MB plus the small title and source
    #: fields. 2 MiB covers it with room to spare and still stops an unbounded body. A test pins
    #: that the worst-case upload fits.
    max_request_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)

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

    # --- Language model (Stage 7.5) ----------------------------------------
    #
    # The master switch, and it is **off by default**. A deployment that sets nothing gets the
    # documented posture of v2-architecture.md §5.2: the application starts, every V1 endpoint
    # serves, and the language-model paths answer a clean 503 with `LLM_DISABLED`. That is the
    # same stance `ml/demand-forecast` takes when its artifact is unavailable, and it is why
    # this is a flag rather than "an API key happens to be set" -- a deployment must say yes on
    # purpose, and an accidentally-present key must not be enough to start spending money.
    llm_enabled: bool = False
    #: Which adapter the factory builds. Never read by a service: §5.1 requires that no caller
    #: names a vendor, so the name is resolved here and nowhere above `app.llm`. `ollama` -- a
    #: local model on this machine -- is the default since R2.7O; the paid `anthropic` provider
    #: is used ONLY when selected explicitly, and neither ever falls back to the other.
    llm_provider: Literal["ollama", "anthropic"] = "ollama"
    #: The model id. None means the provider's default (`DEFAULT_MODELS`); read it through
    #: `effective_llm_model`, never directly, so every consumer resolves it the same way.
    llm_model: str | None = None
    #: Optional override for a gateway or a regional endpoint: an absolute https:// URL, passed
    #: through unchanged. None means https://api.anthropic.com -- never the SDK's
    #: ANTHROPIC_BASE_URL environment fallback (app.llm.providers.anthropic_provider, R2.7D).
    llm_base_url: str | None = None
    #: The provider's extended-thinking setting (R2.7F; default since R2.7I). `disabled` -- the
    #: default -- sends `thinking={"type": "disabled"}` on every request. The adapter does not
    #: replay thinking blocks, which the provider requires within a tool-use turn, so the default
    #: must not ask for any. `provider_default` sends no `thinking` parameter and leaves the
    #: model's own default in force: an explicit opt-in whose tool-use replay is NOT supported or
    #: verified (v2-architecture.md §5.2, A12 and A13). Anything else is refused when the
    #: settings load.
    llm_thinking: Literal["disabled", "provider_default"] = "disabled"
    #: Read from the environment and never logged, never placed in an error and never returned
    #: in a response. Tests assert all three.
    llm_api_key: str | None = None
    #: The per-attempt deadline the boundary enforces, and the per-request token ceiling of
    #: §4.5. Both are bounded above so a misconfiguration cannot express "no limit" -- an
    #: unbounded spend is the availability risk that section names.
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    llm_max_output_tokens: int = Field(default=1024, ge=1, le=32_000)

    # --- Local model: Ollama (R2.7O) -----------------------------------------
    #
    # Read only when `llm_provider` is `ollama`. The server must be on THIS machine: with the
    # copilot enabled on `ollama`, a URL whose host is not a loopback name is refused at load
    # (`is_loopback_url`), and the adapter refuses it again at construction. No key is sent.
    ollama_base_url: str = "http://127.0.0.1:11434"
    #: The context window requested on EVERY call. Sent explicitly because the server's own
    #: default can be far smaller (4096 on an 8 GB GPU) and a prompt longer than the window is
    #: cut, not refused.
    ollama_num_ctx: int = Field(default=16_384, ge=4096, le=262_144)
    #: Sampling, fixed for evaluation repeatability; the model's own defaults are random.
    ollama_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    ollama_seed: int = Field(default=7151, ge=0)

    # --- Copilot budgets (Stage 7.7) -----------------------------------------
    #
    # §4.5: "per-actor and per-hotel call limits". Counted by the existing
    # `FixedWindowRateLimiter`, keyed by the caller's and the hotel's PUBLIC identifiers, and
    # charged only after the caller's membership has been established -- see
    # `app.api.deps.copilot_budget`. One window for both, so the two allowances reset together.
    # (The limiter prunes each bucket by its own window, so differing windows would also be
    # safe; until the mixed-window prune fix they were not.)
    #
    # A refusal is `429 LLM_BUDGET_EXHAUSTED` with `Retry-After`, not `RATE_LIMITED`: the
    # caller spent an allowance, it did not flood an endpoint.
    copilot_actor_rate_limit: int = Field(default=20, ge=1)
    copilot_hotel_rate_limit: int = Field(default=100, ge=1)
    copilot_rate_limit_window_seconds: int = Field(default=3600, ge=1)

    # --- Knowledge document limits ------------------------------------------------
    #
    # Per caller, keyed by the caller's PUBLIC identifier and charged only after the caller's
    # membership (or manager role) is established -- see `app.api.deps.actor_rate_limited`.
    # Reads (list, read, search) and writes (upload, new version, withdrawal) are separate
    # buckets, with their own windows: the limiter prunes every bucket by its own window, so
    # policies with different windows cannot cut each other's counters short. A refusal is
    # `429 RATE_LIMITED` with `Retry-After`.
    knowledge_read_rate_limit: int = Field(default=120, ge=1)
    knowledge_read_rate_limit_window_seconds: int = Field(default=60, ge=1)
    knowledge_write_rate_limit: int = Field(default=30, ge=1)
    knowledge_write_rate_limit_window_seconds: int = Field(default=3600, ge=1)

    # --- Copilot conversation retention (Stage 7.11) --------------------------------------
    #
    # A conversation, and every turn in it, expires this many days after its last activity.
    # Applied in SQL by every read, list, continue and delete -- an expired conversation is a 404
    # at once and never reaches a model. Physical deletion is separate: a start or continuation
    # purges its own hotel, and `python -m app.jobs.purge_conversations` purges every hotel when an
    # operator (or the deployment's scheduler) runs it. See `app.services.copilot_conversation`.
    copilot_conversation_retention_days: int = Field(default=30, ge=1, le=365)

    # --- Copilot accounting retention (Issue 4) ---------------------------------------------
    #
    # An `llm_invocations` record is kept this many days after its `created_at` -- a day is
    # exactly 24 hours -- and then physically deleted by `python -m app.jobs.purge_llm_invocations`
    # when an operator (or the deployment's scheduler) runs it. 365 is the period the project
    # owner approved. The bounds are technical guards, not policy: at least a day, at most ten
    # years, and never shorter than the conversation retention -- see the validator below. The
    # database refuses to delete a record younger than the period the purge declares (migration
    # 0017). See `app.services.llm_invocation_retention`.
    llm_invocation_retention_days: int = Field(default=365, ge=1, le=3650)

    # --- Demand prediction retention (Issue H8) ---------------------------------------------
    #
    # A stored demand prediction is kept while its target date is no more than this many days
    # before its hotel's own today, and is then physically deleted by
    # `python -m app.jobs.purge_demand_predictions` when an operator (or the deployment's
    # scheduler) runs it. 730 keeps a 366-day forecast-performance window and a baseline a year
    # before it. The lower bound is the accuracy protocol's settlement lag (28 days): anything
    # shorter would delete a prediction before its first day of being scorable. The upper bound
    # is a technical guard. See `app.services.demand_prediction_retention`.
    demand_prediction_retention_days: int = Field(default=730, ge=28, le=3650)

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

    @model_validator(mode="after")
    def _llm_key_required_when_enabled(self) -> Settings:
        """Refuse to load a configuration that switches the copilot on without an API key.

        That is a misconfiguration, not a disabled copilot. Caught here, the process never
        starts: before this rule it started, `GET /api/v1/` reported `copilot_enabled: true`,
        and every question then failed at request time, because the chat model is built per
        request. Enforced in every environment; a whitespace-only key counts as none.
        """
        if (
            self.llm_enabled
            and self.llm_provider == "anthropic"
            and not (self.llm_api_key or "").strip()
        ):
            raise ValueError(
                "LLM_ENABLED is true but LLM_API_KEY is not set. Set the key, or set "
                "LLM_ENABLED=false to run without a provider."
            )
        return self

    @model_validator(mode="after")
    def _provider_explicit_beside_a_key(self) -> Settings:
        """Refuse an enabled copilot with an API key but no explicit `LLM_PROVIDER` (R2.7V, M2).

        Before R2.7O `anthropic` was the only provider, so `LLM_ENABLED=true` with a key meant
        the paid provider. The default is now `ollama`, which ignores the key: loaded as it is,
        such a configuration would silently switch to a local model -- in a container with none,
        every question a `503`. Its intent cannot be known, so it is refused, not guessed.
        "Explicit" is pydantic's `model_fields_set`: a value from the environment, a `.env` file
        or the constructor counts; the default does not. A whitespace-only key counts as none,
        as in the key rule above. The message is fixed and repeats no value.
        """
        if (
            self.llm_enabled
            and (self.llm_api_key or "").strip()
            and "llm_provider" not in self.model_fields_set
        ):
            raise ValueError(IMPLICIT_PROVIDER_REFUSAL)
        return self

    @model_validator(mode="after")
    def _ollama_url_is_loopback(self) -> Settings:
        """Refuse a non-loopback `OLLAMA_BASE_URL` when the copilot will use it (R2.7O, R2.7T).

        Only when the copilot is enabled AND `ollama` is selected: that is the one configuration
        that builds a client from this URL. `OLLAMA_BASE_URL` is also a name other tools use (an
        Open WebUI container, say, points it at another host), so checking it in every
        configuration let a setting meant for another program stop this one from starting --
        every V1 endpoint included -- with the copilot off (R2.7S, H1). The Ollama adapter checks
        the URL again, independently, when it is constructed, so a configuration that skips this
        validator (`model_construct`) still cannot reach a non-loopback host. The message is
        fixed and never repeats the value.
        """
        if (
            self.llm_enabled
            and self.llm_provider == "ollama"
            and not is_loopback_url(self.ollama_base_url)
        ):
            raise ValueError(INVALID_OLLAMA_URL_MESSAGE)
        return self

    @model_validator(mode="after")
    def _no_anthropic_endpoint_for_ollama(self) -> Settings:
        """Refuse `LLM_BASE_URL` while an enabled copilot uses Ollama (R2.7O).

        `LLM_BASE_URL` configures the paid provider's endpoint. Set beside `ollama` it would be
        ignored -- a configuration that says one thing and does another -- so it is refused.
        """
        if self.llm_enabled and self.llm_provider == "ollama" and self.llm_base_url is not None:
            raise ValueError(LLM_BASE_URL_WITH_OLLAMA_REFUSAL)
        return self

    @model_validator(mode="after")
    def _no_provider_headers_from_the_environment(self) -> Settings:
        """Refuse an enabled copilot while `ANTHROPIC_CUSTOM_HEADERS` is in the environment.

        The SDK reads that variable itself and adds its headers to every request, overriding the
        application's own -- so another tool's setting in the same shell could change what is
        sent, or under whose credential. Refused rather than stripped: a request this application
        did not shape must not be sent at all. Only PRESENCE is checked; the value is never read,
        so it cannot reach this message or a log. The process environment is what the SDK reads,
        so that is what is checked -- a `.env` entry never reaches the SDK. A disabled copilot
        builds no client, so it is not refused. The adapter checks again when it builds a client.
        """
        if (
            self.llm_enabled
            and self.llm_provider == "anthropic"
            and PROVIDER_HEADERS_VARIABLE in os.environ
        ):
            raise ValueError(PROVIDER_HEADERS_REFUSAL)
        return self

    @property
    def effective_llm_model(self) -> str:
        """The model id in force: `LLM_MODEL` when set, else the selected provider's default."""
        return self.llm_model or DEFAULT_MODELS[self.llm_provider]

    @model_validator(mode="after")
    def _accounting_outlives_conversations(self) -> Settings:
        """Refuse an accounting retention shorter than the conversation retention.

        A stored conversation turn names its `llm_invocations` record by `request_id`. Were the
        record purged first, a live turn would point at an accounting record that no longer
        exists. Equal periods are allowed.
        """
        if self.llm_invocation_retention_days < self.copilot_conversation_retention_days:
            raise ValueError(
                "LLM_INVOCATION_RETENTION_DAYS "
                f"({self.llm_invocation_retention_days}) must be at least "
                "COPILOT_CONVERSATION_RETENTION_DAYS "
                f"({self.copilot_conversation_retention_days})."
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
