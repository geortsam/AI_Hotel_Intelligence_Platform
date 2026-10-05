"""The OpenAPI document states where 401 and 403 can be answered, and nowhere else (Issue G1).

Every operation that resolves the current user answers ``401 INVALID_TOKEN`` to a missing or bad
token; a member below a route's role, or a caller without platform authority on a global
catalogue, gets ``403 FORBIDDEN``. ``app.api.openapi`` declares both from the routes themselves.
These tests read the same routes independently -- the role is read from the ``require_role``
closure, not from the marker the hook reads -- so a hook that stopped seeing a gate fails here
rather than quietly documenting less.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api.deps import get_current_user, require_platform_admin, require_role
from app.api.openapi import (
    PLATFORM_FORBIDDEN_DESCRIPTION,
    ROLE_FORBIDDEN_DESCRIPTION,
    UNAUTHENTICATED_DESCRIPTION,
    document_authorization_errors,
)
from app.core.config import Settings
from app.main import create_app
from app.models.enums import HotelRole
from tests.backend.test_authorization_surface import discover

ERROR_RESPONSE = "#/components/schemas/ErrorResponse"
HOTEL = "/api/v1/hotels/{hotel_public_id}"
HOTEL_ITEM = "/api/v1/hotels/{public_id}"

#: Operations whose role is checked inside a service, not on the route, so the hook cannot see
#: it and the route declares its 403 by hand. Pinned: a new one must be declared on purpose.
SERVICE_LEVEL_403 = {
    ("patch", HOTEL_ITEM): "The caller is a member of this hotel but is not its owner.",
    ("delete", HOTEL_ITEM): "The caller is a member of this hotel but is not its owner.",
    ("get", f"{HOTEL}/audit-events"): "The caller is a member but holds a role below manager.",
}

#: 403s written on the route before G1, more specific than the hook's own wording. Kept verbatim.
HAND_WRITTEN_403 = {
    ("get", f"{HOTEL}/members"): "The caller is a member of this hotel but holds too low a role.",
    ("post", f"{HOTEL}/documents"): (
        "The caller is a member of this hotel but below the manager role."
    ),
    ("get", f"{HOTEL}/ml/forecast-accuracy"): (
        "The caller is a member of this hotel but below the manager role. Distinct from 404 on "
        "purpose: the hotel's existence is already known to a member."
    ),
}

#: The only operation that declares 401 without a bearer token: a failed sign-in.
LOGIN = ("post", "/api/v1/auth/login")
LOGIN_401 = (
    "Authentication failed. The same response is returned whether the address is unknown, the "
    "password is wrong, or the account is disabled."
)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app(Settings(environment="test", secret_key="openapi-authorization-secret"))


@pytest.fixture(scope="module")
def document(app: FastAPI) -> dict[str, Any]:
    return app.openapi()


def operations(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method, path): operation
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
    }


def dependencies(dependant: Dependant) -> list[Dependant]:
    found = [dependant]
    for sub in dependant.dependencies:
        found += dependencies(sub)
    return found


def gate(route: APIRoute) -> str | None:
    """Which 403 *route* can answer, read without the hook's marker."""
    for dependency in dependencies(route.dependant):
        if dependency.call is require_platform_admin:
            return "platform"
        call: Any = dependency.call
        if getattr(call, "__qualname__", "") != "require_role.<locals>.dependency":
            continue
        if inspect.getclosurevars(call).nonlocals["required"] is not HotelRole.VIEWER:
            return "role"
    return None


@pytest.fixture(scope="module")
def gates(app: FastAPI) -> dict[tuple[str, str], str | None]:
    return {
        (method.lower(), endpoint.path): gate(endpoint.route)
        for endpoint in discover(list(app.routes))
        for method in endpoint.route.methods or ()
    }


def refers_to_error_response(response: dict[str, Any]) -> bool:
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    return schema.get("$ref") == ERROR_RESPONSE


# --- 401 -------------------------------------------------------------------------------------


def test_every_authenticated_operation_declares_401(document: dict[str, Any]) -> None:
    secured = {key: op for key, op in operations(document).items() if op.get("security")}

    assert len(secured) >= 90, "the walk found too few authenticated operations to mean much"
    missing = [key for key, op in secured.items() if "401" not in op["responses"]]
    assert missing == []
    for key, operation in secured.items():
        assert refers_to_error_response(operation["responses"]["401"]), key


def test_an_operation_without_a_bearer_token_declares_no_401_but_a_failed_sign_in(
    document: dict[str, Any],
) -> None:
    unsecured = {key: op for key, op in operations(document).items() if not op.get("security")}

    assert {key for key, op in unsecured.items() if "401" in op["responses"]} == {LOGIN}


def test_the_generated_401_says_what_a_bearer_token_failure_is(document: dict[str, Any]) -> None:
    room_types = operations(document)[("get", f"{HOTEL}/room-types")]

    assert room_types["responses"]["401"]["description"] == UNAUTHENTICATED_DESCRIPTION


def test_a_hand_written_401_is_kept_as_written(document: dict[str, Any]) -> None:
    ops = operations(document)

    for key in (LOGIN, ("get", "/api/v1/auth/me"), ("post", "/api/v1/auth/change-password")):
        assert ops[key]["responses"]["401"]["description"] == LOGIN_401, key


# --- 403 -------------------------------------------------------------------------------------


def test_every_route_level_gate_declares_403(
    document: dict[str, Any], gates: dict[tuple[str, str], str | None]
) -> None:
    ops = operations(document)
    gated = {key: kind for key, kind in gates.items() if kind is not None}

    assert sum(kind == "role" for kind in gated.values()) >= 30
    assert sum(kind == "platform" for kind in gated.values()) >= 9
    missing = [key for key in gated if "403" not in ops[key]["responses"]]
    assert missing == []
    for key in gated:
        assert refers_to_error_response(ops[key]["responses"]["403"]), key


def test_a_403_is_claimed_only_where_it_can_be_answered(
    document: dict[str, Any], gates: dict[tuple[str, str], str | None]
) -> None:
    """A viewer-level route cannot refuse a member for their role; a non-member gets 404."""
    declared = {key for key, op in operations(document).items() if "403" in op["responses"]}
    gated = {key for key, kind in gates.items() if kind is not None}

    assert declared - gated == set(SERVICE_LEVEL_403)


def test_a_role_checked_inside_a_service_is_declared_on_its_route(
    document: dict[str, Any], gates: dict[tuple[str, str], str | None]
) -> None:
    ops = operations(document)

    for key, description in SERVICE_LEVEL_403.items():
        assert gates[key] is None, f"{key} is gated on the route now; drop it from this list"
        assert ops[key]["responses"]["403"]["description"] == description
        assert refers_to_error_response(ops[key]["responses"]["403"]), key


def test_the_generated_403_names_the_authority_that_is_missing(
    document: dict[str, Any], gates: dict[tuple[str, str], str | None]
) -> None:
    ops = operations(document)

    assert ops[("post", f"{HOTEL}/room-types")]["responses"]["403"]["description"] == (
        ROLE_FORBIDDEN_DESCRIPTION
    )
    platform = [key for key, kind in gates.items() if kind == "platform"]
    for key in platform:
        assert ops[key]["responses"]["403"]["description"] == PLATFORM_FORBIDDEN_DESCRIPTION


def test_a_hand_written_403_is_kept_as_written(document: dict[str, Any]) -> None:
    ops = operations(document)

    for key, description in HAND_WRITTEN_403.items():
        assert ops[key]["responses"]["403"]["description"] == description, key


# --- the hook on its own ---------------------------------------------------------------------


def isolated_app() -> FastAPI:
    app = FastAPI()
    user = [Depends(get_current_user)]

    @app.get("/open")
    def open_route() -> None: ...

    @app.get("/{hotel_public_id}/read", dependencies=[Depends(require_role(HotelRole.VIEWER))])
    def read() -> None: ...

    @app.post("/{hotel_public_id}/write", dependencies=[Depends(require_role(HotelRole.MANAGER))])
    def write() -> None: ...

    @app.post("/catalogue", dependencies=[Depends(require_platform_admin)])
    def catalogue() -> None: ...

    @app.get("/me", dependencies=user, responses={401: {"description": "Mine."}})
    def me() -> None: ...

    document_authorization_errors(app)
    return app


def test_the_hook_adds_only_401_and_403_where_they_can_be_answered() -> None:
    paths = isolated_app().openapi()["paths"]

    def codes(path: str, method: str) -> set[str]:
        return set(paths[path][method]["responses"])

    assert codes("/open", "get") == {"200"}
    assert codes("/{hotel_public_id}/read", "get") == {"200", "401", "422"}
    assert codes("/{hotel_public_id}/write", "post") == {"200", "401", "403", "422"}
    assert codes("/catalogue", "post") == {"200", "401", "403"}
    assert paths["/me"]["get"]["responses"]["401"]["description"] == "Mine."
    assert paths["/catalogue"]["post"]["responses"]["403"]["description"] == (
        PLATFORM_FORBIDDEN_DESCRIPTION
    )


def test_the_document_is_built_once() -> None:
    app = isolated_app()

    assert app.openapi() is app.openapi()
