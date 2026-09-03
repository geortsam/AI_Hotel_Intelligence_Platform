"""ASGI middleware.

One module per concern, each a plain ASGI callable rather than a ``BaseHTTPMiddleware``
subclass: that base class runs the downstream application in a child task, which breaks
background tasks and buffers streaming responses. Nothing here needs either behaviour.
"""

from __future__ import annotations

from app.middleware.request_id import RequestIdMiddleware

__all__ = ["RequestIdMiddleware"]
