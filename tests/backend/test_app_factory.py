"""Application factory, metadata, routing and CORS.

Route registration is asserted through ``app.openapi()["paths"]`` rather than by walking
``app.routes``: this Starlette version keeps included routers nested as ``_IncludedRouter``
objects instead of flattening them, so a naive walk finds only the built-in docs routes and
would pass while proving nothing. The OpenAPI document is the version-independent source of
truth for what the API actually exposes.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import __version__
from app.core.config import Settings
from app.main import create_app

#: A production Settings now requires a signing secret (Stage 4.1). Supplied here so
#: these tests can build one for reasons unrelated to authentication.
PRODUCTION_SECRET = "a-test-only-secret-not-used-for-anything"


def paths_of(app: FastAPI) -> set[str]:
    return set(app.openapi()["paths"])


# --- construction ---------------------------------------------------------------------


def test_factory_builds_an_app_without_starting_a_server() -> None:
    app = create_app(Settings(environment="test"))

    assert isinstance(app, FastAPI)


def test_factory_returns_independent_instances() -> None:
    """Isolation is the reason this is a factory and not a module-level singleton."""
    first = create_app(Settings(environment="test"))
    second = create_app(Settings(environment="test", app_name="Other"))

    assert first is not second
    assert first.title != second.title


def test_factory_uses_the_settings_it_is_given_not_the_global_singleton() -> None:
    app = create_app(Settings(environment="test", app_name="Injected Name"))

    assert app.title == "Injected Name"
    assert app.state.settings.environment == "test"


# --- metadata -------------------------------------------------------------------------


def test_metadata_comes_from_settings_and_the_package_version() -> None:
    settings = Settings(environment="test", app_description="A description from config.")
    app = create_app(settings)

    assert app.title == settings.app_name
    assert app.description == "A description from config."
    assert app.version == __version__


def test_docs_are_enabled_outside_production() -> None:
    app = create_app(Settings(environment="test"))

    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"


def test_docs_are_disabled_in_production() -> None:
    """The schema must not be browsable on a production deployment."""
    app = create_app(Settings(environment="production", secret_key=PRODUCTION_SECRET))

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


# --- routing --------------------------------------------------------------------------


def test_operational_probes_are_registered_at_the_root() -> None:
    """Probe URLs must not move when a new API version is introduced."""
    paths = paths_of(create_app(Settings(environment="test")))

    assert "/health" in paths
    assert "/health/db" in paths


def test_v1_router_is_mounted_under_the_configured_prefix() -> None:
    app = create_app(Settings(environment="test"))

    assert "/api/v1/" in paths_of(app)


def test_version_prefix_is_configurable_not_hard_coded() -> None:
    app = create_app(Settings(environment="test", api_v1_prefix="/api/v9"))
    paths = paths_of(app)

    assert "/api/v9/" in paths
    assert "/api/v1/" not in paths
    # Root probes are unaffected by the version prefix.
    assert "/health" in paths


#: Resources approved so far. Everything else must stay off the API surface.
APPROVED_RESOURCE_SEGMENTS = {
    "hotels",
    # Stage 4.5.1. An action on the caller's own account, not a resource: it names no
    # subject because the token already does.
    "change-password",
    # Stage 4.4. Who may reach a hotel, administered under the hotel itself: a membership
    # belongs to one property even though the account it names is global.
    "members",
    "room-types",
    "rooms",
    "amenities",
    "guests",
    "bookings",
    "payments",
    "refunds",
    "reviews",
    # Singular: uq_reviews_booking_id makes a stay's review a singleton, and the URL says so.
    "review",
    "revenue",
    "expenses",
    "revenue-categories",
    "expense-categories",
    "analytics",
    "overview",
    "daily",
    "revenue-by-category",
    "expenses-by-category",
    "intelligence",
    "forecast",
    "occupancy",
    "demand-trend",
    "anomalies",
    "insights",
    "auth",
    "register",
    "login",
    "me",
}

#: Resources the SCHEMA models as global -- no hotel_id column, and a unique constraint with
#: no tenant column in it. These are the only collections allowed to sit outside the hotel
#: hierarchy, and each is listed deliberately rather than matched by a pattern.
#: Authentication (Stage 4.1). Not a domain resource and not a lookup table: `users` has no
#: hotel_id and no foreign key at all, so identity cannot hang off a hotel. Which hotels a
#: user may reach is Stage 4.2's question, answered elsewhere.
IDENTITY_COLLECTIONS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/me",
    # Stage 4.5.1. A password belongs to the ACCOUNT, which is global: requiring a hotel to
    # change your own credential would mean a user who belongs to no property could never
    # change it, and one who belongs to two would have to pick.
    "/api/v1/auth/change-password",
}

SCHEMA_GLOBAL_COLLECTIONS = {
    "/api/v1/amenities",
    "/api/v1/amenities/{code}",
    "/api/v1/revenue-categories",
    "/api/v1/revenue-categories/{code}",
    "/api/v1/expense-categories",
    "/api/v1/expense-categories/{code}",
}

#: Domains scheduled for later stages. Matched as whole path SEGMENTS, not substrings:
#: "room-types" contains "room", and a substring guard would either block the approved
#: resource or have to be loosened into uselessness.
NOT_YET_IMPLEMENTED_SEGMENTS = {
    "recommendations",
    "pricing",
    "agents",
    "chat",
    "embeddings",
}


def segments_of(path: str) -> set[str]:
    return {part for part in path.split("/") if part and not part.startswith("{")}


def test_no_unimplemented_domain_has_leaked_onto_the_api() -> None:
    """Stage guard: hotels (3B.1), room types (3B.2), rooms (3B.3), amenities (3B.4),
    guests (3B.5), bookings (3B.6), payments (3B.7), reviews (3B.8), the financial ledger
    (3B.9), analytics (3B.10) and the statistical intelligence layer (3B.11) are approved.
    Stage 4.1 adds authentication (register / login / me). Everything else -- notably
    authorization, and anything agentic, generative or operation-changing -- is still ahead
    of schedule."""
    paths = paths_of(create_app(Settings(environment="test")))

    offenders = {path for path in paths if segments_of(path) & NOT_YET_IMPLEMENTED_SEGMENTS}
    assert offenders == set()


def test_domain_surface_is_exactly_the_approved_hierarchy() -> None:
    paths = paths_of(create_app(Settings(environment="test")))
    domain_paths = {p for p in paths if p.startswith("/api/v1/") and p != "/api/v1/"}

    assert domain_paths == {
        "/api/v1/hotels",
        "/api/v1/hotels/{public_id}",
        "/api/v1/hotels/{hotel_public_id}/members",
        "/api/v1/hotels/{hotel_public_id}/members/{user_public_id}",
        "/api/v1/hotels/{hotel_public_id}/room-types",
        "/api/v1/hotels/{hotel_public_id}/room-types/{code}",
        "/api/v1/hotels/{hotel_public_id}/room-types/{room_type_code}/rooms",
        "/api/v1/hotels/{hotel_public_id}/room-types/{room_type_code}/rooms/{room_number}",
        "/api/v1/hotels/{hotel_public_id}/room-types/{room_type_code}/amenities",
        "/api/v1/hotels/{hotel_public_id}/room-types/{room_type_code}/amenities/{amenity_code}",
        "/api/v1/amenities",
        "/api/v1/amenities/{code}",
        "/api/v1/hotels/{hotel_public_id}/guests",
        "/api/v1/hotels/{hotel_public_id}/guests/{guest_public_id}",
        "/api/v1/hotels/{hotel_public_id}/bookings",
        "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}",
        "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/payments",
        "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/payments/refunds",
        "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}"
        "/payments/{payment_public_id}",
        "/api/v1/hotels/{hotel_public_id}/reviews",
        "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/review",
        "/api/v1/revenue-categories",
        "/api/v1/revenue-categories/{code}",
        "/api/v1/expense-categories",
        "/api/v1/expense-categories/{code}",
        "/api/v1/hotels/{hotel_public_id}/revenue",
        "/api/v1/hotels/{hotel_public_id}/expenses",
        "/api/v1/hotels/{hotel_public_id}/analytics/overview",
        "/api/v1/hotels/{hotel_public_id}/analytics/daily",
        "/api/v1/hotels/{hotel_public_id}/analytics/revenue-by-category",
        "/api/v1/hotels/{hotel_public_id}/analytics/expenses-by-category",
        "/api/v1/hotels/{hotel_public_id}/analytics/reviews",
        "/api/v1/hotels/{hotel_public_id}/intelligence/forecast/occupancy",
        "/api/v1/hotels/{hotel_public_id}/intelligence/forecast/revenue",
        "/api/v1/hotels/{hotel_public_id}/intelligence/demand-trend",
        "/api/v1/hotels/{hotel_public_id}/intelligence/anomalies",
        "/api/v1/hotels/{hotel_public_id}/intelligence/insights",
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/change-password",
        "/api/v1/auth/me",
    }


def test_every_domain_segment_is_an_approved_resource() -> None:
    """Catches a new resource appearing under a name the blocklist never anticipated."""
    paths = paths_of(create_app(Settings(environment="test")))
    used = set().union(*(segments_of(p) for p in paths if p.startswith("/api/v1/")))

    assert used - {"api", "v1"} <= APPROVED_RESOURCE_SEGMENTS


# --- CORS -----------------------------------------------------------------------------


def test_cors_allows_a_configured_origin() -> None:
    app = create_app(Settings(environment="test", cors_origins=["http://allowed.test"]))
    client = TestClient(app)

    response = client.get("/health", headers={"Origin": "http://allowed.test"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://allowed.test"


def test_cors_does_not_echo_an_unconfigured_origin() -> None:
    app = create_app(Settings(environment="test", cors_origins=["http://allowed.test"]))
    client = TestClient(app)

    response = client.get("/health", headers={"Origin": "http://evil.test"})

    # The request still succeeds; the browser is simply not told it may read the result.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_preflight_is_answered_for_a_configured_origin() -> None:
    app = create_app(Settings(environment="test", cors_origins=["http://allowed.test"]))
    client = TestClient(app)

    response = client.options(
        "/health",
        headers={
            "Origin": "http://allowed.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://allowed.test"


def test_no_production_origin_is_hard_coded() -> None:
    """Origins must come from configuration only."""
    defaults = Settings(environment="test").cors_origins

    assert all(origin.startswith("http://localhost") for origin in defaults)


# --- API metadata endpoint -------------------------------------------------------------


def test_api_meta_endpoint_identifies_the_version(client: TestClient) -> None:
    payload = client.get("/api/v1/").json()

    assert payload["api_version"] == "v1"
    assert payload["version"] == __version__
    assert payload["documentation"] == "/docs"


def test_api_meta_hides_documentation_link_in_production() -> None:
    client = TestClient(
        create_app(Settings(environment="production", secret_key=PRODUCTION_SECRET))
    )

    assert client.get("/api/v1/").json()["documentation"] is None


def test_every_domain_is_nested_unless_the_schema_makes_it_global() -> None:
    """Structural guard, independent of any blocklist.

    A domain resource must hang off a hotel unless the schema itself models it as global --
    amenities carry no hotel_id, and uq_amenities_code has no tenant column, so nesting the
    catalogue under one hotel would imply an ownership the database does not model.

    Every exception is enumerated in SCHEMA_GLOBAL_COLLECTIONS. A NEW top-level collection --
    the shape an unapproved domain would most likely take -- still fails here even if nobody
    remembered to add its name to NOT_YET_IMPLEMENTED_SEGMENTS.
    """
    paths = paths_of(create_app(Settings(environment="test")))
    domain_paths = {p for p in paths if p.startswith("/api/v1/") and p != "/api/v1/"}

    unexpected = {
        p
        for p in domain_paths
        if not p.startswith("/api/v1/hotels")
        and p not in SCHEMA_GLOBAL_COLLECTIONS
        and p not in IDENTITY_COLLECTIONS
    }
    assert unexpected == set(), sorted(unexpected)


def test_the_only_global_collection_has_no_tenant_column_in_the_schema() -> None:
    """The exception above is justified by the model, not by convenience."""
    from app.models.room import Amenity

    assert not hasattr(Amenity, "hotel_id")


def test_no_hotel_branch_is_deeper_than_the_approved_hierarchy() -> None:
    """The deepest approved chain is hotels -> bookings -> payments -> refunds, four
    segments as of Stage 3B.7. Rooms are three (hotels -> room-types -> rooms); guests and
    bookings branch off the hotel directly and are shallower.

    A FIFTH level means a domain arrived that nobody approved, and fails here regardless of
    what it is named.
    """
    paths = paths_of(create_app(Settings(environment="test")))
    resource_segments = [
        [s for s in p.split("/") if s and not s.startswith("{")][2:]
        for p in paths
        if p.startswith("/api/v1/hotels")
    ]

    assert max(len(segments) for segments in resource_segments) == 4


def test_guests_branch_directly_off_the_hotel() -> None:
    """Guests are hotel-scoped with no intermediate parent, unlike rooms."""
    paths = paths_of(create_app(Settings(environment="test")))
    guest_paths = {p for p in paths if "guests" in p}

    assert guest_paths == {
        "/api/v1/hotels/{hotel_public_id}/guests",
        "/api/v1/hotels/{hotel_public_id}/guests/{guest_public_id}",
    }


def test_bookings_expose_no_allocation_or_night_sub_resources() -> None:
    """The deferred night-completeness trigger requires an allocation and its nights to be
    written in one transaction. A sub-resource route could never commit on its own, so none
    exists -- allocations travel inside the booking payload.

    Payments DO nest under a booking (Stage 3B.7) and are approved, so the claim here is
    about allocations and nights specifically, not about the booking having no children.
    """
    paths = paths_of(create_app(Settings(environment="test")))

    # The booking resource itself is exactly these two.
    own = {
        p
        for p in paths
        if p.startswith("/api/v1/hotels/{hotel_public_id}/bookings")
        and "payments" not in p
        and "review" not in p
    }
    assert own == {
        "/api/v1/hotels/{hotel_public_id}/bookings",
        "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}",
    }

    # And nothing anywhere exposes allocations or nights.
    assert not any("booking-rooms" in p or "nights" in p for p in paths)


def test_payments_are_append_only() -> None:
    """No PATCH and no DELETE on any payment route.

    A financial record is never edited or removed; a mistake is corrected by posting a
    refund. The missing verbs are the contract, so they are asserted rather than assumed.
    """
    paths = create_app(Settings(environment="test")).openapi()["paths"]
    payment_paths = {p: set(v) for p, v in paths.items() if "payments" in p}

    assert payment_paths, "no payment routes registered"
    for path, verbs in payment_paths.items():
        assert "patch" not in verbs, f"{path} exposes PATCH"
        assert "delete" not in verbs, f"{path} exposes DELETE"
        assert "put" not in verbs, f"{path} exposes PUT"
