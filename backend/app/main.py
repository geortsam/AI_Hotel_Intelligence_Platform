"""FastAPI application entrypoint.

This module wires the application together and contains no business logic: no queries, no
domain rules, no request handling. Routing lives in ``app.api``, behaviour in ``app.services``,
persistence in ``app.repositories``.

The dependency direction is one-way throughout:

    api/router -> schemas -> services -> repositories -> SQLAlchemy -> PostgreSQL
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import health as health_routes
from app.api.v1.router import api_router as api_v1_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.rate_limit import FixedWindowRateLimiter
from app.db.session import dispose_engine
from app.middleware.request_id import RequestIdMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan.

    Logging is configured HERE rather than in the factory. It is process-global state, and the
    factory runs hundreds of times in the test suite; reconfiguring it per instance would
    replace root's handlers underneath whatever was already capturing. Startup runs once, for
    an application that is actually about to serve.

    Nothing else is opened: the engine is built lazily on first use, so the app can start and
    report an unreachable database rather than refusing to boot. Connections are returned to
    the operating system on shutdown.
    """
    configure_logging(app.state.settings)
    yield
    dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    A factory, not a module-level singleton, so tests can construct isolated instances with
    overridden settings instead of mutating global state. Calling it starts no server and
    opens no database connection.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.app_name,
        description=settings.app_description,
        version=__version__,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )

    # Endpoints read their settings from here via the get_app_settings dependency, so an
    # app built with explicit settings behaves consistently everywhere.
    app.state.settings = settings

    # One limiter per application instance, holding its counters in memory. Per-instance
    # rather than module-level so every app a test builds starts clean and nothing leaks
    # between them; per-PROCESS in production, which is the documented limitation --
    # see app.core.rate_limit.
    app.state.rate_limiter = FixedWindowRateLimiter()

    # Origins come from configuration; nothing is hard-coded, so a production deployment is
    # locked down by its environment rather than by editing this file.
    # Added LAST, which in Starlette makes it OUTERMOST: every response passes back through
    # it, including CORS preflights and the envelopes the exception handlers return, so each
    # one carries `X-Request-ID`. It is also the first thing to run inbound, so the id is bound
    # before any dependency -- authentication, rate limiting -- can log.
    app.add_middleware(RequestIdMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    # Operational probes sit at the root: an orchestrator's probe URL must not move when a
    # new API version is introduced.
    app.include_router(health_routes.router)
    app.include_router(api_v1_router, prefix=settings.api_v1_prefix)

    return app


app = create_app()
