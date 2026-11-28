"""Count and time every HTTP request into ``app.state.metrics`` (A5).

Pure ASGI, like the request-id middleware: it reads the status from the response-start
message it forwards and the route from the scope the router filled in, and it changes neither.
No header is added, no body is read and no response is altered, so a request is answered
exactly as it was without it.

It is the outermost middleware, so the time covers the whole stack and the requests other
middleware answer themselves -- a body refused for size, a CORS preflight -- are counted too.
An exception that escapes the application is counted as the 500 that Starlette's error
middleware, outside this one, turns it into.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.metrics import UNMATCHED, RequestMetrics


def route_label(scope: Scope) -> str:
    """The matched route's full template, or ``unmatched``. Never the requested path.

    FastAPI (0.141) dispatches an included router's request to the ORIGINAL route, whose
    ``path_format`` lacks the ``include_router`` prefix (``/hotels/{hotel_public_id}/bookings``
    for ``/api/v1/hotels/...``). The full template is the effective route context's ``path``,
    which FastAPI records on the scope beside it; it is used when it is a template ending in
    the route's own, so the label is a declared template either way and never request text.
    """
    template = getattr(scope.get("route"), "path_format", None)
    if not (isinstance(template, str) and template):
        return UNMATCHED
    fastapi_scope = scope.get("fastapi")
    context = (
        fastapi_scope.get("effective_route_context") if isinstance(fastapi_scope, dict) else None
    )
    full = getattr(context, "path", None)
    if isinstance(full, str) and full.endswith(template):
        return full
    return template


class RequestMetricsMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        metrics: RequestMetrics,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = self.clock()
        # 500 until a response starts: an exception before then becomes Starlette's 500.
        status = 500

        async def send_and_record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_and_record_status)
        finally:
            self.metrics.observe(
                scope.get("method", ""), route_label(scope), status, self.clock() - started
            )
