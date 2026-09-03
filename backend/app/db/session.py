"""Engine and session construction.

This module is the ONLY place that builds a database connection. The FastAPI dependency in
``app.api.deps`` consumes the factory built here rather than creating its own, so there is
exactly one pool and one set of connection options in the process.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings


def create_db_engine(settings: Settings | None = None, url: str | None = None) -> Engine:
    """Build an engine from explicit settings, an explicit URL, or the process settings."""
    settings = settings or get_settings()
    resolved = url or settings.sqlalchemy_url
    if resolved is None:
        raise RuntimeError(
            "No database URL configured. Set DATABASE_URL, or the POSTGRES_* variables, "
            "in the environment."
        )
    return create_engine(
        resolved,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        future=True,
        # A SQLAlchemy exception renders as "... [SQL: INSERT ...] [parameters: (...)]", and
        # those parameters are the row being written -- for `users` that is the Argon2 digest,
        # for `guests` a real person's contact details. The centralised handler logs unexpected
        # database errors with `exc_info`, so without this the whole bound row reaches the log
        # on any driver failure nobody thought to catch. Verified live: with it off, a sentinel
        # digest appears in the message; with it on, it does not.
        #
        # The SQL text itself is still logged, which is what makes the error diagnosable.
        hide_parameters=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory. `expire_on_commit=False` keeps objects usable after commit."""
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """The process-wide engine, built on first use.

    Lazy on purpose: importing the app must not require a reachable database, or the
    application could not start to report that the database is down -- which is exactly
    what ``GET /health/db`` exists to do.
    """
    return create_db_engine()


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """The process-wide session factory, bound to :func:`get_engine`."""
    return create_session_factory(get_engine())


def dispose_engine() -> None:
    """Close every pooled connection and forget the cached engine.

    Used by application shutdown and by tests that need a clean slate.
    """
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
