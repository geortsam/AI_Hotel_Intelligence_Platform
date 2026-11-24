"""``GET /metrics``: the request metrics, for a scraper on the internal network (A5).

Mounted at the root beside ``/health``, not under ``/api/v1``: it is an operational probe, not
part of the versioned API, and nginx forwards only ``/api/`` -- so the public entry point never
reaches it (a request for ``/metrics`` there gets the SPA). The API's port is not published
either (docker-compose.yml), so the only clients are on the Compose network.

**Not in the OpenAPI document**, which describes the API offered to clients.

**Refused through a proxy.** A request carrying a forwarding header -- every request nginx
forwards carries ``X-Forwarded-For`` and ``X-Real-IP`` -- is answered with the application's
ordinary 404, as if the route did not exist. So if a proxy rule ever does forward this path,
it still serves nothing, and says nothing about why.

No authentication, as for ``/health``: the documented scope is the internal network, and who
may scrape a deployment is decided with the alert destination (roadmap §3.12). The output holds
no identifier, no personal data and no configuration -- only route templates, methods, status
codes, counts and durations (``app.core.metrics``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.metrics import CONTENT_TYPE, RequestMetrics

router = APIRouter()

#: Headers a reverse proxy adds. Their presence means the request did not come straight from
#: the internal network.
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                      "x-real-ip")  # fmt: skip


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    if any(name in request.headers for name in FORWARDING_HEADERS):
        raise StarletteHTTPException(status_code=404)
    registry: RequestMetrics = request.app.state.metrics
    return Response(
        content=registry.render(),
        media_type=CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )
