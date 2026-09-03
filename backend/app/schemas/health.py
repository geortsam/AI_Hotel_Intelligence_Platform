"""Health and readiness response contracts.

Liveness and readiness answer different questions and must not be conflated:

* **Liveness** (``GET /health``) -- is this process up? Checks nothing else. An orchestrator
  uses it to decide whether to restart the container.
* **Readiness** (``GET /health/db``) -- can this process serve traffic? Requires the database.
  An orchestrator uses it to decide whether to route requests here.

A process whose database is unreachable is alive but not ready. Reporting that as "unhealthy"
would get the container killed and restarted, which fixes nothing when the fault is in the
database.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Liveness. Reports the process only -- no dependency is consulted."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = Field(description="Always 'ok': reaching this code means alive.")
    app: str = Field(description="Application name.")
    version: str = Field(description="Running code version.")
    environment: str = Field(description="Deployment environment.")


class DatabaseHealthResponse(BaseModel):
    """Readiness. Distinguishes the application from its database, deliberately.

    ``application`` is always ``running`` -- the response could not be produced otherwise --
    while ``database`` carries the actual finding. Returned with HTTP 200 when reachable and
    503 when not.

    No connection string, host, port, credential or driver error text ever appears here.
    ``error_code`` is a fixed constant, not the database's message.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"] = Field(description="Overall readiness verdict.")
    application: Literal["running"] = Field(
        default="running", description="The process itself; always running if you got a reply."
    )
    database: Literal["reachable", "unreachable"] = Field(
        description="Whether a real query succeeded against PostgreSQL."
    )
    latency_ms: float | None = Field(
        default=None, description="Round-trip time of the probe query; null when it failed."
    )
    error_code: str | None = Field(
        default=None,
        description="Fixed code when unreachable (e.g. DATABASE_UNAVAILABLE). Never driver text.",
    )
