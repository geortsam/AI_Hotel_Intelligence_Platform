"""Apply the security headers to every response.

Pure ASGI, mirroring :mod:`app.middleware.request_id`: the headers are written by wrapping
``send`` rather than by touching a response object, so they land on whatever the application
produced -- a rendered model, a redirect, a CORS preflight, or any of the envelopes the
centralised exception handlers return. Scattering these across routers would guarantee that
the next router forgets one.

WHICH headers and WHEN is not decided here. That is :mod:`app.core.security_headers`, so the
one error handler that runs outside all user middleware can reach the same decision without
``app.core`` importing ``app.middleware``. This module only resolves the request's scheme and
writes the result.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.client_address import FORWARDED_PROTO_HEADER, resolve_scheme, trusted_networks
from app.core.config import Settings
from app.core.security_headers import security_headers


class SecurityHeadersMiddleware:
    """Write the security headers onto every HTTP response.

    *csp_exempt_paths* carries the interactive documentation URLs, which the application object
    already knows; passing them in keeps this middleware from hard-coding ``/docs``.
    """

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        csp_exempt_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self.settings = settings
        self.csp_exempt_paths = csp_exempt_paths
        # Parsed once per application rather than per request. The configuration cannot change
        # for the life of the process, and an invalid entry has already been refused by
        # Settings, so this cannot raise here.
        self.networks = trusted_networks(tuple(settings.trusted_proxies))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan and websocket scopes carry no response headers to write.
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        # The ORIGINAL client's scheme, which is what HSTS must be decided on: behind a
        # TLS-terminating proxy the connection we see is plain HTTP. `resolve_scheme` reads
        # X-Forwarded-Proto only when the peer is a configured trusted proxy, because that
        # header is exactly as forgeable as X-Forwarded-For.
        resolved_scheme = resolve_scheme(
            scope.get("scheme", "http"),
            client[0] if client else None,
            _header_values(scope, FORWARDED_PROTO_HEADER),
            self.networks,
        )
        include_csp = scope.get("path") not in self.csp_exempt_paths
        headers = security_headers(self.settings, resolved_scheme, include_csp=include_csp)

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                mutable = MutableHeaders(scope=message)
                for name, value in headers.items():
                    # Assignment REPLACES any existing value of that name rather than adding a
                    # second one, so a response cannot end up carrying a header twice.
                    mutable[name] = value
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


def _header_values(scope: Scope, name: str) -> list[str]:
    """Every value of *name* in the raw ASGI headers, in the order received.

    Bytes that are not valid ASCII cannot be a valid value here, so such a header is dropped
    rather than raising on a decode.
    """
    wanted = name.encode("latin-1")
    values: list[str] = []
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == wanted:
            try:
                values.append(raw_value.decode("ascii"))
            except UnicodeDecodeError:
                continue
    return values


__all__ = ["SecurityHeadersMiddleware"]
