"""Stage 4.2: every hotel-scoped route must declare its authorization.

The point of this module is that it is DISCOVERY-BASED. It carries no hand-written list of the
routes that exist today -- it enumerates the application's own routing table and holds each
route it finds to a rule. Adding a router, or a verb to an existing router, therefore cannot
ship an unauthorized endpoint: the new route is discovered here and fails until it declares
what it requires. A checklist test would have passed silently instead, which is precisely the
failure mode this guards against.

Four rules, and one deliberate, enumerated exemption:

0. Nothing outside :data:`PUBLIC_PATHS` is reachable without a token.
1. Every route addressed by a hotel reaches ``get_hotel_access_policy``. That is what makes it
   authenticated AND membership-checked: the policy needs a user, the scope resolver needs the
   policy, and there is no path to a domain service that skips the chain.
2. Every MUTATING hotel-scoped route additionally declares ``require_role(...)``, so the
   minimum role sits visibly on the route rather than inside a service.
3. Every write to a global catalogue requires platform authority, and every read of one does
   not.

The exemption: the hotel resource itself (``/hotels/{public_id}``) is exempt from rule 2,
because ``HotelService`` bypasses the scope resolver by nature -- it *is* the thing being
resolved -- and authorizes through the same policy inside its own resolution path. The
exemption is pinned to an exact pair of routes so a third cannot quietly join it.

No database and no HTTP: this reads the routing table, so it runs in the fast suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, NamedTuple

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api import deps
from app.core.config import Settings
from app.main import create_app

#: Routes reachable without a token. Every one is a deliberate decision: registration and login
#: cannot require the token they exist to obtain, and the probes are consumed by infrastructure
#: that holds no account. A new entry here is a new unauthenticated surface, so this list is
#: where it has to be argued for -- and rule 0 fails until it is.
PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/health/db",
        "/api/v1/",
        "/api/v1/auth/register",
        "/api/v1/auth/login",
    }
)

#: The exemption to rule 2, pinned exactly.
HOTEL_RESOURCE_PATH = "/api/v1/hotels/{public_id}"

CATALOGUE_PREFIXES = [
    "/api/v1/amenities",
    "/api/v1/revenue-categories",
    "/api/v1/expense-categories",
]

MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


class Endpoint(NamedTuple):
    """One route, at the path it is actually served on."""

    path: str
    route: APIRoute

    @property
    def methods(self) -> set[str]:
        # `APIRoute.methods` is Optional to the type checker; every route built by a decorator
        # has one, and an empty set here would silently exempt a route from every rule below.
        assert self.route.methods, f"{self.path} declares no HTTP method"
        return set(self.route.methods)

    @property
    def label(self) -> str:
        return f"{sorted(self.methods)[0]} {self.path}"

    @property
    def mutates(self) -> bool:
        return bool(self.methods & MUTATING_METHODS)

    @property
    def hotel_scoped(self) -> bool:
        return "{hotel_public_id}" in self.path or self.path == HOTEL_RESOURCE_PATH


def discover(routes: list[object], prefix: str = "") -> list[Endpoint]:
    """Flatten the routing table, reassembling each route's full path.

    FastAPI defers inclusion: ``app.routes`` holds wrapper objects whose ``original_router``
    carries the real ``APIRoute`` objects at paths relative to the include prefix. Walking that
    structure -- rather than reading ``app.routes`` directly -- is what makes the discovery
    complete; a shallow read finds four documentation routes and would pass vacuously.
    """
    found: list[Endpoint] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(Endpoint(prefix + route.path, route))
        elif hasattr(route, "original_router"):
            # An internal FastAPI wrapper with no public type, so it is read dynamically.
            wrapper: Any = route
            nested = list(wrapper.original_router.routes)
            found += discover(nested, prefix + (wrapper.include_context.prefix or ""))
    return found


@pytest.fixture(scope="module")
def endpoints() -> list[Endpoint]:
    app = create_app(Settings(environment="test", secret_key="surface-test-signing-secret"))
    return discover(list(app.routes))


def walk(dependant: Dependant) -> Iterator[Dependant]:
    """Every dependency a route resolves, transitively.

    Authorization is reached indirectly -- a route depends on a service, which depends on the
    scope resolver, which depends on the policy -- so a shallow look at the route's own
    ``dependencies=[...]`` would miss it and report false failures.
    """
    yield dependant
    for sub in dependant.dependencies:
        yield from walk(sub)


def calls(endpoint: Endpoint) -> set[object]:
    return {sub.call for sub in walk(endpoint.route.dependant) if sub.call is not None}


def declares_role(endpoint: Endpoint) -> bool:
    """True when the route carries a ``require_role(...)`` dependency.

    ``require_role`` returns a closure, so it cannot be compared by identity; its qualified
    name identifies it, and matching that exact string means renaming the factory breaks this
    test loudly rather than silently disarming it.
    """
    return any(
        getattr(call, "__qualname__", "").startswith("require_role.") for call in calls(endpoint)
    )


# --- the discovery itself has to work, or every rule below is vacuous ----------------------------


def test_discovery_finds_the_whole_routing_table(endpoints: list[Endpoint]) -> None:
    paths = {e.path for e in endpoints}

    assert len(endpoints) > 60, f"only {len(endpoints)} routes discovered -- the walk is broken"
    assert "/api/v1/hotels" in paths
    assert "/api/v1/hotels/{hotel_public_id}/bookings" in paths


def test_discovery_finds_the_hotel_scoped_routes(endpoints: list[Endpoint]) -> None:
    assert len([e for e in endpoints if e.hotel_scoped]) >= 40


# --- rule 0: nothing outside the public list is anonymous ----------------------------------------


def test_every_non_public_route_requires_a_token(endpoints: list[Endpoint]) -> None:
    """Including the global catalogues, which are readable but not anonymous."""
    anonymous = sorted(
        endpoint.label
        for endpoint in endpoints
        if endpoint.path not in PUBLIC_PATHS
        and deps.get_current_user not in calls(endpoint)
        and deps.get_hotel_access_policy not in calls(endpoint)
        and deps.get_platform_access_policy not in calls(endpoint)
    )

    assert anonymous == [], f"these routes are reachable without a token: {anonymous}"


def test_the_public_list_is_not_stale(endpoints: list[Endpoint]) -> None:
    """A path removed from the app but left here would widen the next route's licence."""
    paths = {endpoint.path for endpoint in endpoints}

    assert paths >= PUBLIC_PATHS, f"listed as public but not routed: {sorted(PUBLIC_PATHS - paths)}"


# --- rule 1: a hotel-scoped route is authenticated and membership-checked -------------------------


def test_every_hotel_scoped_route_reaches_the_access_policy(endpoints: list[Endpoint]) -> None:
    missing = sorted(
        endpoint.label
        for endpoint in endpoints
        if endpoint.hotel_scoped and deps.get_hotel_access_policy not in calls(endpoint)
    )

    assert missing == [], (
        "these hotel-scoped routes never resolve HotelAccessPolicy, so they are neither "
        f"authenticated nor membership-checked: {missing}"
    )


# --- rule 2: a mutating hotel-scoped route declares the role it needs -----------------------------


def test_every_hotel_scoped_write_declares_a_required_role(endpoints: list[Endpoint]) -> None:
    missing = sorted(
        endpoint.label
        for endpoint in endpoints
        if endpoint.hotel_scoped
        and endpoint.path != HOTEL_RESOURCE_PATH
        and endpoint.mutates
        and not declares_role(endpoint)
    )

    assert missing == [], (
        "these routes mutate hotel data without declaring require_role(...), so any member "
        f"-- a viewer included -- could call them: {missing}"
    )


def test_the_hotel_resource_is_the_only_exemption(endpoints: list[Endpoint]) -> None:
    """HotelService authorizes internally; nothing else may claim that."""
    exempt = sorted(
        endpoint.label
        for endpoint in endpoints
        if endpoint.hotel_scoped and endpoint.mutates and not declares_role(endpoint)
    )

    assert exempt == [
        "DELETE /api/v1/hotels/{public_id}",
        "PATCH /api/v1/hotels/{public_id}",
    ]


# --- rule 3: the global catalogues are readable, never writable -----------------------------------


@pytest.mark.parametrize("prefix", CATALOGUE_PREFIXES)
def test_every_global_catalogue_write_requires_platform_authority(
    endpoints: list[Endpoint], prefix: str
) -> None:
    """A global row belongs to no hotel, so no per-hotel role can authorize changing it.

    Stage 4.2 asserted these routes carried a blanket denial; Stage 4.3 replaced that denial
    with a grant check, so the assertion moves rather than relaxes -- an unguarded catalogue
    write still fails here.
    """
    writes = [e for e in endpoints if e.path.startswith(prefix) and e.mutates]

    assert writes, f"no write routes discovered under {prefix} -- has the prefix changed?"
    for endpoint in writes:
        assert deps.require_platform_admin in calls(endpoint), (
            f"{endpoint.label} writes a global catalogue without require_platform_admin"
        )


@pytest.mark.parametrize("prefix", CATALOGUE_PREFIXES)
def test_global_catalogue_reads_are_allowed_but_not_anonymous(
    endpoints: list[Endpoint], prefix: str
) -> None:
    """Denying reads too would break every hotel's use of a vocabulary it must reference."""
    reads = [e for e in endpoints if e.path.startswith(prefix) and not e.mutates]

    assert reads, f"no read routes discovered under {prefix}"
    for endpoint in reads:
        assert deps.require_platform_admin not in calls(endpoint), (
            f"{endpoint.label} demands platform authority to READ a shared vocabulary"
        )
        assert deps.get_current_user in calls(endpoint), f"{endpoint.label} is anonymous"


def test_no_catalogue_route_carries_a_hotel_role(endpoints: list[Endpoint]) -> None:
    """Platform authority and hotel roles are different questions, and no route asks both."""
    confused = sorted(
        e.label
        for e in endpoints
        if any(e.path.startswith(prefix) for prefix in CATALOGUE_PREFIXES) and declares_role(e)
    )

    assert confused == []


def test_no_hotel_scoped_route_requires_platform_authority(endpoints: list[Endpoint]) -> None:
    """The other direction, and the one that would be a tenant-isolation bug: a hotel route
    guarded by a platform grant would be reachable by someone with no membership."""
    confused = sorted(
        e.label for e in endpoints if e.hotel_scoped and deps.require_platform_admin in calls(e)
    )

    assert confused == []


def test_no_catalogue_route_is_hotel_scoped(endpoints: list[Endpoint]) -> None:
    """The tables have no hotel_id; a hotel-scoped catalogue route would imply they do."""
    scoped = sorted(
        e.label
        for e in endpoints
        if any(e.path.startswith(prefix) for prefix in CATALOGUE_PREFIXES) and e.hotel_scoped
    )

    assert scoped == []
