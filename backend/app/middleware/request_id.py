"""Attach a correlation id to every request and every response.

Pure ASGI. The alternative, ``BaseHTTPMiddleware``, runs the application in a child task and
would put a task boundary between where the id is set and where an endpoint reads it -- the
one thing this middleware exists to avoid.

The header is written by wrapping ``send`` rather than by touching a response object, so it
lands on whatever the application produced: a rendered model, a redirect, or any of the
envelopes the centralised exception handlers return.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.request_id import (
    REQUEST_ID_HEADER,
    bind_request_id,
    reset_request_id,
    sanitise_request_id,
)


class RequestIdMiddleware:
    """Bind a sanitised request id for the duration of one HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan and websocket scopes have no request to correlate and no headers to
            # write. Passing them through untouched keeps this middleware out of startup.
            await self.app(scope, receive, send)
            return

        request_id = sanitise_request_id(_supplied_header(scope))
        token = bind_request_id(request_id)

        # Also on the scope, which outlives the ContextVar for one specific case.
        #
        # Starlette's ServerErrorMiddleware -- the thing that turns an unhandled exception into
        # the 500 envelope -- sits OUTSIDE all user middleware. An exception therefore unwinds
        # through the `finally` below, unbinding the id, before that handler ever runs. The
        # scope is the same object either way, so the handler can still find the id there.
        # Measured, not assumed: without this the 500 is the one response with no header.
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # In a `finally`, so the id is unbound whether the endpoint returned, raised,
            # was cancelled, or failed part-way through streaming a body. Without this a
            # worker could serve the next request under the previous request's id.
            reset_request_id(token)


def _supplied_header(scope: Scope) -> str | None:
    """The raw ``X-Request-ID``, if the client sent one.

    Returned undecoded-safe: a header whose bytes are not valid ASCII cannot be a valid id
    either, so it is reported as absent rather than raising on a decode.
    """
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == REQUEST_ID_HEADER.encode("latin-1"):
            try:
                return raw_value.decode("ascii")
            except UnicodeDecodeError:
                return None
    return None


__all__ = ["RequestIdMiddleware"]
