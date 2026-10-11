"""Refuse a request body larger than ``Settings.max_request_body_bytes``.

Pure ASGI, like :mod:`app.middleware.request_id` and :mod:`app.middleware.security_headers`.
Every field already has a length bound in its Pydantic schema, but a schema runs only after the
whole body has been read into memory and parsed. Without this, a client could send any number
of bytes and the server would buffer every one of them before saying no.

Two checks, because a client need not declare its length:

* **A declared ``Content-Length`` over the cap** is answered ``413`` at once. The application is
  never called and no byte of the body is read.
* **A body without a usable length** (chunked transfer, or a length header this does not trust)
  is counted as it arrives. The first chunk that takes the total over the cap raises a ``413``
  ``HTTPException`` from ``receive``. FastAPI re-raises an ``HTTPException`` met while reading a
  body rather than turning it into its generic 400, so the ordinary handler renders it, and
  nothing past the cap is buffered.

Both produce the one error envelope, code ``REQUEST_TOO_LARGE``. The middleware is the INNERMOST
one, so the refusal still passes out through CORS, ``X-Request-ID`` and the security headers.
"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.status import HTTP_413_CONTENT_TOO_LARGE
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import REQUEST_TOO_LARGE_CODE, REQUEST_TOO_LARGE_MESSAGE, error_response


class RequestSizeLimitMiddleware:
    """Cap the bytes an HTTP request body may carry."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_body_bytes:
            response = error_response(
                HTTP_413_CONTENT_TOO_LARGE, REQUEST_TOO_LARGE_CODE, REQUEST_TOO_LARGE_MESSAGE
            )
            await response(scope, receive, send)
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise HTTPException(
                        status_code=HTTP_413_CONTENT_TOO_LARGE, detail=REQUEST_TOO_LARGE_MESSAGE
                    )
            return message

        await self.app(scope, counting_receive, send)


def _declared_length(scope: Scope) -> int | None:
    """The request's ``Content-Length``, or ``None`` when there is no single, plain number.

    Anything else -- two headers, a sign, spaces, letters -- is not trusted to decide the request
    either way; such a body is simply counted as it arrives.
    """
    values = [
        value for name, value in scope.get("headers", []) if name.lower() == b"content-length"
    ]
    if len(values) != 1 or not values[0].isdigit():
        return None
    return int(values[0])


__all__ = ["RequestSizeLimitMiddleware"]
