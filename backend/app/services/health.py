"""Readiness logic.

Knows what "ready" means and how to describe it; knows no SQL and no HTTP. Translating the
verdict into a status code is the router's job, and issuing the query is the repository's.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy.exc import SQLAlchemyError

from app.repositories.health import HealthRepository
from app.schemas.health import DatabaseHealthResponse

logger = logging.getLogger(__name__)


class HealthService:
    """Answers "can this process serve traffic?"."""

    def __init__(self, repository: HealthRepository) -> None:
        self._repository = repository

    def database_status(self) -> DatabaseHealthResponse:
        """Probe the database and describe the result without leaking how it failed.

        A failure is caught rather than raised: an unreachable database is a *reportable
        finding* for this endpoint, not an error. Letting it propagate would produce a
        generic 503 error envelope and lose the distinction the probe exists to draw --
        that the application is running while its database is not reachable.

        The driver's message is logged, never returned: it routinely carries the host,
        the user and the connection string.
        """
        started = time.perf_counter()
        try:
            self._repository.ping()
        except SQLAlchemyError as exc:
            logger.warning("Database readiness probe failed", exc_info=exc)
            return DatabaseHealthResponse(
                status="degraded",
                database="unreachable",
                latency_ms=None,
                error_code="DATABASE_UNAVAILABLE",
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        return DatabaseHealthResponse(
            status="ok",
            database="reachable",
            latency_ms=round(elapsed_ms, 3),
        )


__all__ = ["HealthService"]
