"""Operational probes.

Mounted at the root, NOT under /api/v1, and deliberately so: an orchestrator's probe URL
should not change when the API is versioned. `/api/v2` may arrive one day; `/health` must not
move when it does.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app import __version__
from app.api.deps import HealthServiceDep, SettingsDep
from app.schemas.health import DatabaseHealthResponse, HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Reports that the process is up. Consults no dependency.",
)
def health(settings: SettingsDep) -> HealthResponse:
    """Liveness. Reaching this function is itself the proof."""
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=__version__,
        environment=settings.environment,
    )


@router.get(
    "/health/db",
    response_model=DatabaseHealthResponse,
    summary="Database readiness probe",
    description=(
        "Runs a real query against PostgreSQL. Returns 200 when the database is reachable "
        "and 503 when it is not. The application is running in both cases."
    ),
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": DatabaseHealthResponse,
            "description": "The application is running but the database is unreachable.",
        }
    },
)
def database_health(service: HealthServiceDep, response: Response) -> DatabaseHealthResponse:
    """Readiness. Delegates the probe; only the status code is decided here."""
    result = service.database_status()
    if result.database == "unreachable":
        # 503, not 500: the fault is a dependency being down, and it is expected to recover.
        # A load balancer reads this as "stop routing here", not "restart this process".
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
