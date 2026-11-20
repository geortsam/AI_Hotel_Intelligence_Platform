"""R1.4: every route that can answer 429 declares it in OpenAPI.

A route answers 429 when one of its dependencies is a rate limit or a copilot budget. The
dependencies are found by walking each route's dependency tree, not by listing routes, so a new
limited route that forgets its 429 response fails here. The set of limiting dependencies is
itself checked against `app.api.deps`: every dependency built by `rate_limited` or
`actor_rate_limited` must be in it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant

from app.api import deps
from app.core.config import Settings
from app.main import create_app
from tests.backend.test_authorization_surface import discover

#: Every dependency that can refuse with 429.
LIMITERS: dict[str, Callable[..., Any]] = {
    "login_rate_limit": deps.login_rate_limit,
    "change_password_rate_limit": deps.change_password_rate_limit,
    "knowledge_read_rate_limit": deps.knowledge_read_rate_limit,
    "knowledge_write_rate_limit": deps.knowledge_write_rate_limit,
    "copilot_budget": deps.copilot_budget,
    "conversation_turn_budget": deps.conversation_turn_budget,
}

#: The routes `LIMITERS` reach today; a change here is a deliberate change of the limited surface.
H = "/api/v1/hotels/{hotel_public_id}"
EXPECTED: dict[tuple[str, str], set[str]] = {
    ("POST", "/api/v1/auth/login"): {"login_rate_limit"},
    # R1.1. The cookie sign-in shares the login's budget: one brute-force surface, one limit.
    ("POST", "/api/v1/auth/session"): {"login_rate_limit"},
    ("POST", "/api/v1/auth/change-password"): {"change_password_rate_limit"},
    ("GET", f"{H}/documents"): {"knowledge_read_rate_limit"},
    ("GET", f"{H}/documents/{{document_public_id}}"): {"knowledge_read_rate_limit"},
    ("GET", f"{H}/knowledge/search"): {"knowledge_read_rate_limit"},
    ("POST", f"{H}/documents"): {"knowledge_write_rate_limit"},
    ("POST", f"{H}/documents/{{document_public_id}}/versions"): {"knowledge_write_rate_limit"},
    ("POST", f"{H}/documents/{{document_public_id}}/withdrawal"): {"knowledge_write_rate_limit"},
    ("POST", f"{H}/copilot/ask"): {"copilot_budget"},
    ("POST", f"{H}/copilot/conversations"): {"copilot_budget"},
    ("POST", f"{H}/copilot/conversations/{{conversation_public_id}}/messages"): {
        "conversation_turn_budget"
    },
}


def calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from calls(sub)


@pytest.fixture(scope="module")
def app() -> Any:
    return create_app(Settings(environment="test", secret_key="rate-limit-openapi-secret"))


def limited_routes(app: Any) -> dict[tuple[str, str], set[str]]:
    by_call = {id(call): name for name, call in LIMITERS.items()}
    found: dict[tuple[str, str], set[str]] = {}
    # `discover` flattens FastAPI's deferred inclusion; a shallow read of `app.routes` finds
    # only the documentation routes, and every check below would pass vacuously.
    for endpoint in discover(list(app.routes)):
        names = {
            by_call[id(call)] for call in calls(endpoint.route.dependant) if id(call) in by_call
        }
        if names:
            for method in endpoint.methods:
                found[(method, endpoint.path)] = names
    return found


def test_every_rate_limit_in_deps_is_a_known_limiter() -> None:
    built = {
        name
        for name, value in vars(deps).items()
        if callable(value)
        and getattr(value, "__qualname__", "").startswith(
            ("rate_limited.<locals>", "actor_rate_limited.<locals>")
        )
    }
    assert built == set(LIMITERS) - {"copilot_budget", "conversation_turn_budget"}


def test_the_limiters_reach_the_routes_they_are_meant_to(app: Any) -> None:
    routes = limited_routes(app)
    assert routes == EXPECTED
    reached = set().union(*routes.values())
    assert reached == set(LIMITERS), "a limiter no route uses is a limiter nobody tested"


def test_every_limited_route_declares_429(app: Any) -> None:
    paths = app.openapi()["paths"]
    missing = [
        f"{method} {path} ({', '.join(sorted(names))})"
        for (method, path), names in sorted(limited_routes(app).items())
        if "429" not in paths[path][method.lower()]["responses"]
    ]
    assert missing == []


def test_the_429_response_names_retry_after(app: Any) -> None:
    paths = app.openapi()["paths"]
    for method, path in sorted(limited_routes(app)):
        declared = paths[path][method.lower()]["responses"]["429"]
        assert declared.get("description"), (method, path)
