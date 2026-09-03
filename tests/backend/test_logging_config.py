"""Logging configuration, and the defect that made it necessary.

Stage 4.5.4.1. Three things are pinned here.

**`Settings.log_level` finally does something.** It was declared in Stage 1 and read by
nothing until now; a setting that silently governs nothing is worse than no setting.

**Alembic must not silence the application.** `logging.config.fileConfig` defaults to
`disable_existing_loggers=True`, and `database/migrations/env.py` calls it. Before this stage
that one call set `.disabled = True` on all thirteen `app.*` loggers -- every service warning
and both error-handler `logger.exception` calls became no-ops for the life of the process. It
surfaced as an order-dependent test; it would have surfaced in production as an application
that runs migrations at startup and then logs nothing at all.

**Bound parameters stay out of exception text.** A SQLAlchemy error renders as
`... [SQL: INSERT ...] [parameters: (...)]`, and those parameters are the row -- for `users`,
an Argon2 digest. The centralised handler logs unexpected database errors with `exc_info`.

These tests mutate global logging state, so each one restores what it found.
"""

from __future__ import annotations

import logging
import logging.config
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

from app.core.config import Settings
from app.core.logging import (
    LOG_FORMAT,
    RequestIdFilter,
    configure_logging,
    logging_config,
)
from app.core.request_id import NO_REQUEST_ID, bind_request_id, reset_request_id

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
ENV_PY = REPO_ROOT / "database" / "migrations" / "env.py"

#: Every logger the application actually creates, as of this stage.
APP_LOGGERS = [
    "app.core.errors",
    "app.services.amenity",
    "app.services.auth",
    "app.services.booking",
    "app.services.finance",
    "app.services.guest",
    "app.services.health",
    "app.services.hotel",
    "app.services.membership",
    "app.services.payment",
    "app.services.review",
    "app.services.room",
    "app.services.room_type",
]


@pytest.fixture
def pristine_logging() -> Iterator[None]:
    """Snapshot and restore the global logging state around a test that reconfigures it."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_app_level = logging.getLogger("app").level
    saved_disabled = {name: logging.getLogger(name).disabled for name in APP_LOGGERS}

    yield

    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    logging.getLogger("app").setLevel(saved_app_level)
    for name, disabled in saved_disabled.items():
        logging.getLogger(name).disabled = disabled


# ======================================================================================
# The Alembic defect
# ======================================================================================


def test_env_py_disables_no_existing_loggers() -> None:
    """Read from the source, so the argument cannot be dropped without this failing."""
    source = ENV_PY.read_text(encoding="utf-8")

    assert "disable_existing_loggers=False" in source
    assert "fileConfig(config.config_file_name)" not in source, (
        "env.py reverted to the default, which silences every application logger"
    )


def test_alembic_file_config_leaves_application_loggers_enabled(
    pristine_logging: None,
) -> None:
    """The regression test for the real defect, exercised rather than read.

    Creates the loggers the way the application does, runs exactly what `env.py` runs, and
    checks that all thirteen survive.
    """
    for name in APP_LOGGERS:
        logging.getLogger(name).disabled = False

    logging.config.fileConfig(str(ALEMBIC_INI), disable_existing_loggers=False)

    still_disabled = [name for name in APP_LOGGERS if logging.getLogger(name).disabled]
    assert still_disabled == [], f"Alembic silenced {len(still_disabled)} application loggers"


def test_the_default_would_have_disabled_them(pristine_logging: None) -> None:
    """Proves the fix is load-bearing rather than decorative.

    Runs `fileConfig` the way `env.py` used to and asserts the loggers DO go quiet -- so if a
    future Python or Alembic changed that default, the test above would stop being evidence
    and this one would tell us.
    """
    for name in APP_LOGGERS:
        logging.getLogger(name).disabled = False

    logging.config.fileConfig(str(ALEMBIC_INI))  # the old call, default True

    assert all(logging.getLogger(name).disabled for name in APP_LOGGERS)


# ======================================================================================
# configure_logging
# ======================================================================================


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def settings(level: LogLevel = "INFO") -> Settings:
    return Settings(environment="test", secret_key="logging-suite-secret", log_level=level)


def test_the_configuration_never_disables_existing_loggers() -> None:
    """The same trap, in our own configuration. This runs at startup, after every module has
    been imported -- the default would silence all of them."""
    assert logging_config(settings())["disable_existing_loggers"] is False


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_the_configured_level_reaches_the_app_logger(
    pristine_logging: None, level: LogLevel
) -> None:
    """`Settings.log_level` governs the application's logger, which is the point of the
    setting existing."""
    configure_logging(settings(level))

    assert logging.getLogger("app").level == getattr(logging, level)


def test_the_level_actually_changes_what_is_emitted(pristine_logging: None) -> None:
    """Not just the attribute: a DEBUG record is emitted at DEBUG and dropped at WARNING."""
    logger = logging.getLogger("app.services.probe")
    seen: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    configure_logging(settings("DEBUG"))
    logging.getLogger().addHandler(Capture())
    logger.debug("visible at debug")

    configure_logging(settings("WARNING"))
    logging.getLogger().addHandler(Capture())
    logger.debug("invisible at warning")

    assert "visible at debug" in seen
    assert "invisible at warning" not in seen


def test_the_default_level_is_info() -> None:
    """The public settings contract is unchanged by this stage."""
    assert Settings(environment="test").log_level == "INFO"


def test_the_request_id_filter_is_installed_on_the_handler() -> None:
    config = logging_config(settings())

    assert "request_id" in config["filters"]
    assert config["handlers"]["console"]["filters"] == ["request_id"]
    assert "%(request_id)s" in LOG_FORMAT


def test_the_handler_is_on_root_so_records_still_propagate() -> None:
    """`app` deliberately gets a level but no handler of its own.

    Giving it `propagate=False` would stop records reaching root -- and pytest's `caplog`
    captures through root, so every existing log-content assertion in the suite would go
    silently vacuous rather than fail.
    """
    config = logging_config(settings())

    assert config["root"]["handlers"] == ["console"]
    assert "handlers" not in config["loggers"]["app"]
    assert config["loggers"]["app"].get("propagate") is not False


def test_configure_logging_is_not_called_by_the_application_factory() -> None:
    """It is process-global state and the factory runs hundreds of times in this suite.

    Asserted against the source: `create_app` must not call it, and the lifespan must.
    """
    main_source = (REPO_ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    factory = main_source.split("def create_app(")[1]

    assert "configure_logging(" not in factory, "logging is configured per app instance"
    assert "configure_logging(app.state.settings)" in main_source.split("def create_app(")[0]


# ======================================================================================
# The filter
# ======================================================================================


def test_the_filter_stamps_the_current_request_id() -> None:
    record = logging.LogRecord("app.probe", logging.INFO, __file__, 1, "m", None, None)
    token = bind_request_id("abc-123")
    try:
        assert RequestIdFilter().filter(record) is True
    finally:
        reset_request_id(token)

    assert record.request_id == "abc-123"  # type: ignore[attr-defined]


def test_the_filter_never_drops_a_record() -> None:
    """A filter that rejected records without a request id would throw away exactly the
    startup and background logs most worth keeping."""
    record = logging.LogRecord("app.probe", logging.INFO, __file__, 1, "m", None, None)

    assert RequestIdFilter().filter(record) is True
    assert record.request_id == NO_REQUEST_ID  # type: ignore[attr-defined]


def test_the_filter_does_not_raise_outside_a_request() -> None:
    record = logging.LogRecord("app.probe", logging.INFO, __file__, 1, "m", None, None)

    RequestIdFilter().filter(record)

    assert record.request_id == NO_REQUEST_ID  # type: ignore[attr-defined]


def test_a_configured_handler_formats_the_request_id(pristine_logging: None) -> None:
    """End to end through dictConfig: the id lands in the rendered line."""
    import io

    configure_logging(settings("INFO"))
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    logging.getLogger().addHandler(handler)

    token = bind_request_id("rendered-id")
    try:
        logging.getLogger("app.services.probe").warning("a message")
    finally:
        reset_request_id(token)

    assert "[rendered-id]" in stream.getvalue()
    assert "a message" in stream.getvalue()


# ======================================================================================
# Bound parameters stay out of exception text
# ======================================================================================


def test_the_engine_hides_bound_parameters() -> None:
    """Asserted on the engine the application actually builds."""
    from app.db.session import create_db_engine

    engine = create_db_engine(
        Settings(environment="test", secret_key="x"),
        url="postgresql+psycopg://user:pw@localhost:5432/never_connected_test",
    )

    assert engine.hide_parameters is True


def test_a_sentinel_never_appears_in_rendered_exception_text() -> None:
    """A statement error is rendered without its parameters.

    Built offline against a compiled statement -- no connection, so this stays in the fast
    suite -- with a sentinel standing in for the digest that `users` inserts really bind.
    """
    from sqlalchemy.exc import StatementError

    sentinel = "SENTINEL-NEVER-LOG-THIS-DIGEST"
    error = StatementError(
        message="duplicate key value violates unique constraint",
        statement="INSERT INTO users (email, password_hash) VALUES (%(email)s, %(hash)s)",
        params={"email": "person@example.test", "hash": sentinel},
        orig=Exception("orig"),
        hide_parameters=True,
    )

    rendered = str(error)

    assert sentinel not in rendered, "the digest reached the exception text"
    assert "person@example.test" not in rendered, "the address reached the exception text"
    assert "INSERT INTO users" in rendered, "the SQL is still there to diagnose with"
