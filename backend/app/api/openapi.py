"""What the OpenAPI document says about authentication and authorization failures (Issue G1).

The server answers a missing, malformed, expired or revoked bearer token with ``401
INVALID_TOKEN`` on every authenticated operation, and an authenticated member whose role is too
low -- or a caller without platform authority, on the global catalogues -- with ``403
FORBIDDEN``. Until G1 the document said so on a handful of routes only. Declaring both by hand on
every router would drift the first time a route is added, so the generated document is corrected
once, where it is generated, on the precedent of
:func:`app.core.errors.document_validation_errors`:

* **401** is added to every operation that carries a security requirement -- exactly the
  operations that resolve the current user.
* **403** is added to every operation whose route requires a role above viewer
  (:func:`app.api.deps.require_role`) or platform authority
  (:func:`app.api.deps.require_platform_admin`). Where it cannot happen it is not claimed: a
  viewer-level read cannot be refused for its role, and a non-member gets the hotel's 404, never
  a 403. A role checked inside a service rather than on the route is declared on that route by
  hand, as before.

Both point at the shared ``ErrorResponse`` envelope the handlers send. A 401 or 403 a route
already declared is left exactly as written. Paths, operations and every other response are
untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api.deps import require_platform_admin
from app.models.enums import HotelRole

UNAUTHENTICATED_DESCRIPTION = (
    "Not authenticated: the bearer token is missing, malformed, expired or revoked."
)
ROLE_FORBIDDEN_DESCRIPTION = (
    "The caller is a member of this hotel but holds too low a role for this operation."
)
PLATFORM_FORBIDDEN_DESCRIPTION = (
    "The caller is authenticated but holds no platform administrator grant."
)

_ERROR_RESPONSE = {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}


def _endpoints(routes: list[Any], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every route with the full path it is served on. FastAPI includes routers lazily, so the
    wrappers in ``app.routes`` are opened rather than read as they are."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router"):
            yield from _endpoints(
                list(route.original_router.routes),
                prefix + (route.include_context.prefix or ""),
            )


def _dependencies(dependant: Dependant) -> Iterator[Dependant]:
    yield dependant
    for sub in dependant.dependencies:
        yield from _dependencies(sub)


def forbidden_reason(route: APIRoute) -> str | None:
    """Why *route* can answer 403, or None when it cannot -- read from its own dependencies."""
    for dependency in _dependencies(route.dependant):
        if dependency.call is require_platform_admin:
            return PLATFORM_FORBIDDEN_DESCRIPTION
        role = getattr(dependency.call, "minimum_role", None)
        if role is not None and role is not HotelRole.VIEWER:
            return ROLE_FORBIDDEN_DESCRIPTION
    return None


def _error(description: str) -> dict[str, Any]:
    return {"description": description, "content": _ERROR_RESPONSE}


def document_authorization_errors(app: FastAPI) -> None:
    """Make the OpenAPI document state where 401 and 403 can be answered. See the module."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = generate()
        reasons: dict[tuple[str, str], str] = {}
        for path, route in _endpoints(list(app.routes)):
            reason = forbidden_reason(route)
            if reason is not None:
                for method in route.methods or ():
                    reasons[(path, method.lower())] = reason
        for path, operations in schema.get("paths", {}).items():
            for method, operation in operations.items():
                responses = operation.setdefault("responses", {})
                if operation.get("security") and "401" not in responses:
                    responses["401"] = _error(UNAUTHENTICATED_DESCRIPTION)
                reason = reasons.get((path, method))
                if reason is not None and "403" not in responses:
                    responses["403"] = _error(reason)
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


__all__ = [
    "PLATFORM_FORBIDDEN_DESCRIPTION",
    "ROLE_FORBIDDEN_DESCRIPTION",
    "UNAUTHENTICATED_DESCRIPTION",
    "document_authorization_errors",
    "forbidden_reason",
]
