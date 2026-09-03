"""Alembic migration environment.

The database URL is never stored in ``alembic.ini``. It is resolved here, in this order:

1. ``-x url=...`` passed on the Alembic command line,
2. ``TEST_DATABASE_URL`` -- so a migration can be applied to a throwaway test database,
3. ``app.core.config.Settings.sqlalchemy_url`` -- ``DATABASE_URL`` or the POSTGRES_* parts.

Keeping it out of the file means a connection string, and therefore a password, is never
committed.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import Settings

# Importing the models package registers every table on Base.metadata, which is what
# autogenerate compares the live database against.
from app.models import Base

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers` defaults to True, which would set `.disabled = True` on
    # every logger already created -- all thirteen `app.*` loggers when migrations run in a
    # process that has imported the application. Every service warning and both error-handler
    # `logger.exception` calls would then be silently dropped for the life of that process.
    #
    # This is not a test-only concern: any deployment that runs `alembic upgrade head` before
    # serving would come up with its own logging switched off. It also caused
    # `test_error_handling.py::test_internal_errors_are_still_logged_in_full` to fail whenever
    # an integration module ran first, which is how it was found.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Find a database URL, or fail with an actionable message."""
    from_cli = context.get_x_argument(as_dictionary=True).get("url")
    if from_cli:
        return str(from_cli)

    from_env = os.environ.get("TEST_DATABASE_URL")
    if from_env:
        return from_env

    url = Settings().sqlalchemy_url
    if url:
        return url

    raise RuntimeError(
        "No database URL available. Provide one of:\n"
        "  alembic -x url=postgresql+psycopg://user:pw@host:5432/db upgrade head\n"
        "  TEST_DATABASE_URL=postgresql+psycopg://...\n"
        "  DATABASE_URL=... (or the POSTGRES_* variables) in the environment"
    )


def _include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Keep autogenerate from proposing to drop objects it cannot model.

    The exclusion constraint, the constraint triggers and the overlap-detection view are
    created by hand-written SQL. Alembic's autogenerate does not understand them, and would
    otherwise offer to drop them on every subsequent revision.
    """
    return not (type_ == "table" and name == "historical_room_overlaps")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting -- useful for review and for DBA handover."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
