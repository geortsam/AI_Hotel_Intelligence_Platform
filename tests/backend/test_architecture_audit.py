"""Whole-codebase architecture audit.

Every previous stage asserted its own layering. This file asserts it across **all** modules
at once, by discovery rather than by an enumerated list -- so a domain added later is audited
the day it appears, instead of the day someone remembers to add it here.

The checks are AST-based. Text scanning has produced four false positives across this project
(``RoomRevenueRow`` contains "Revenue"; a docstring explaining that ``exc_info`` is avoided
contains "exc_info"; a router description explaining that payments are not a revenue proxy
contains "Payment"), so identifiers are matched exactly and docstrings are stripped before any
source is examined.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from types import ModuleType

import pytest

import app.api.v1.endpoints as endpoints_package
import app.repositories as repositories_package
import app.services as services_package
from app.core.config import Settings
from app.main import create_app


def _modules(package: ModuleType) -> list[ModuleType]:
    """Every module in a package, discovered rather than listed."""
    found = []
    for info in pkgutil.iter_modules(package.__path__):
        found.append(importlib.import_module(f"{package.__name__}.{info.name}"))
    assert found, f"{package.__name__} looks empty -- the audit would be vacuous"
    return sorted(found, key=lambda module: module.__name__)


ROUTERS = _modules(endpoints_package)
REPOSITORIES = _modules(repositories_package)
SERVICES = _modules(services_package)

#: Domain modules only -- see HEALTH_MODULES below.
DOMAIN_REPOSITORIES = [m for m in REPOSITORIES if not m.__name__.endswith(".health")]
DOMAIN_SERVICES = [m for m in SERVICES if not m.__name__.endswith(".health")]

#: The health domain deliberately runs a connectivity probe from its repository and owns no
#: business rules; it is a liveness check, not a domain. Listed explicitly so the exemption
#: is visible rather than pattern-matched away.
HEALTH_MODULES = {"app.repositories.health", "app.services.health"}


def code_of(module: ModuleType) -> str:
    """Module source with every docstring removed.

    Prose explaining why a construct is absent must not read as that construct being present.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def identifiers(module: ModuleType) -> set[str]:
    """Every name, attribute and imported symbol a module references, matched exactly."""
    tree = ast.parse(inspect.getsource(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[-1])
    return found


def imported_modules(module: ModuleType) -> set[str]:
    """The dotted module paths a module imports from."""
    tree = ast.parse(inspect.getsource(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def ids(modules: list[ModuleType]) -> list[str]:
    return [module.__name__.rsplit(".", 1)[-1] for module in modules]


# --- API layer -----------------------------------------------------------------------------


@pytest.mark.parametrize("module", ROUTERS, ids=ids(ROUTERS))
def test_routers_issue_no_queries(module: ModuleType) -> None:
    """Matched on CALLS, not on bare names: a router legitimately defines a handler called
    ``delete_amenity`` or ``update_hotel``, and neither is a SQLAlchemy construct."""
    tree = ast.parse(inspect.getsource(module))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    for forbidden in ["select", "insert", "update", "delete", "func", "text", "and_", "or_"]:
        assert forbidden not in called, f"{module.__name__} calls {forbidden}()"


@pytest.mark.parametrize("module", ROUTERS, ids=ids(ROUTERS))
def test_routers_never_touch_a_session(module: ModuleType) -> None:
    names = identifiers(module)

    assert "Session" not in names
    assert "commit" not in names
    assert "rollback" not in names
    assert "flush" not in names


#: The one module under ``app.models`` a router may import.
#:
#: Stage 4.2 put ``HotelRole`` here, beside the other closed vocabularies, because the CHECK
#: constraint it mirrors lives in the database and every layer has to agree on its spelling.
#: A router names the minimum role a route requires -- a fact about the HTTP surface, not about
#: the hotel -- so it needs the vocabulary, and only the vocabulary. The exemption is safe
#: because :func:`test_the_vocabulary_module_maps_no_table` proves this module defines no ORM
#: class; the day one is added there, that test fails rather than this exemption widening.
VOCABULARY_MODULE = "app.models.enums"


def test_the_vocabulary_module_maps_no_table() -> None:
    """Guards the exemption below: ``app.models.enums`` must stay pure vocabulary."""
    import app.models.enums as vocabulary
    from app.models import Base

    mapped = sorted(
        name
        for name, obj in vars(vocabulary).items()
        if inspect.isclass(obj) and issubclass(obj, Base) and obj is not Base
    )

    assert mapped == [], (
        f"{VOCABULARY_MODULE} now defines ORM classes {mapped}, so a router importing it is "
        "importing models -- move them out, or drop the exemption."
    )


@pytest.mark.parametrize("module", ROUTERS, ids=ids(ROUTERS))
def test_routers_import_no_orm_model_or_repository(module: ModuleType) -> None:
    """A router reaches the database only through a service, and the shared vocabulary."""
    for imported in imported_modules(module):
        if imported != VOCABULARY_MODULE:
            assert not imported.startswith("app.models"), f"{module.__name__} imports {imported}"
        assert not imported.startswith("app.repositories"), f"{module.__name__} imports {imported}"
        assert not imported.startswith("sqlalchemy"), f"{module.__name__} imports {imported}"


@pytest.mark.parametrize("module", ROUTERS, ids=ids(ROUTERS))
def test_routers_raise_no_domain_errors(module: ModuleType) -> None:
    """Status codes come from the shared exception handlers, not from ad-hoc raises."""
    names = identifiers(module)

    assert "HTTPException" not in names
    for domain_error in ["NotFoundError", "ConflictError", "ValidationError"]:
        assert domain_error not in names, f"{module.__name__} raises {domain_error}"


# --- repository layer ------------------------------------------------------------------------


@pytest.mark.parametrize("module", REPOSITORIES, ids=ids(REPOSITORIES))
def test_repositories_never_commit_or_roll_back(module: ModuleType) -> None:
    """Flushing is the repository's job; the transaction boundary is the service's."""
    names = identifiers(module)

    assert "commit" not in names, f"{module.__name__} commits"
    assert "rollback" not in names, f"{module.__name__} rolls back"


@pytest.mark.parametrize("module", DOMAIN_REPOSITORIES, ids=ids(DOMAIN_REPOSITORIES))
def test_repositories_translate_no_domain_errors(module: ModuleType) -> None:
    names = identifiers(module)

    for forbidden in [
        "NotFoundError",
        "ConflictError",
        "ValidationError",
        "sqlstate_of",
        "IntegrityError",
    ]:
        assert forbidden not in names, f"{module.__name__} translates {forbidden}"


@pytest.mark.parametrize("module", REPOSITORIES, ids=ids(REPOSITORIES))
def test_repositories_hold_no_http_concerns(module: ModuleType) -> None:
    for imported in imported_modules(module):
        assert not imported.startswith("fastapi"), f"{module.__name__} imports {imported}"
        assert not imported.startswith("starlette"), f"{module.__name__} imports {imported}"
    assert "status_code" not in identifiers(module)


@pytest.mark.parametrize("module", REPOSITORIES, ids=ids(REPOSITORIES))
def test_repositories_import_no_service(module: ModuleType) -> None:
    """The dependency runs one way: service -> repository."""
    for imported in imported_modules(module):
        assert not imported.startswith("app.services"), f"{module.__name__} imports {imported}"


# --- service layer ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", DOMAIN_SERVICES, ids=ids(DOMAIN_SERVICES))
def test_services_build_no_sqlalchemy_queries(module: ModuleType) -> None:
    """Query construction belongs to the repository, without exception."""
    names = identifiers(module)

    for forbidden in ["select", "func", "and_", "or_", "group_by", "order_by", "join"]:
        assert forbidden not in names, f"{module.__name__} builds queries with {forbidden!r}"


@pytest.mark.parametrize("module", SERVICES, ids=ids(SERVICES))
def test_services_import_no_http_framework(module: ModuleType) -> None:
    for imported in imported_modules(module):
        assert not imported.startswith("fastapi"), f"{module.__name__} imports {imported}"


def test_every_writing_service_owns_its_transaction_boundary() -> None:
    """A service that writes must both commit and roll back. Read-only services do neither,
    and that asymmetry is the point: it is visible here rather than assumed."""
    writers: list[str] = []
    readers: list[str] = []
    for module in SERVICES:
        source = code_of(module)
        (writers if "commit()" in source else readers).append(module.__name__)
        if "commit()" in source:
            assert "rollback()" in source, f"{module.__name__} commits but never rolls back"

    assert "app.services.analytics" in readers
    assert "app.services.intelligence" in readers
    # Stage 6.9. It holds no session at all, so it could not commit even if it wanted to --
    # a deliberate contrast with app.services.ml_serving, which took one in Stage 6.8 because
    # it owns a unit of work.
    assert "app.services.ml_accuracy" in readers
    # Stage 6.10, same structural argument: it holds no session either, so it observes
    # distributions over rows it can only read.
    assert "app.services.ml_drift" in readers
    # Stage 6.11, same structural argument again: no session, so nothing to commit to.
    assert "app.services.ml_prediction_read" in readers
    for domain in ("hotel", "booking", "payment", "review", "finance", "guest"):
        assert f"app.services.{domain}" in writers


def test_no_service_declares_its_own_sqlstate_constants() -> None:
    """One vocabulary, in app.core.errors. A per-service copy would drift."""
    for module in SERVICES:
        source = code_of(module)
        for literal in ['"23505"', '"23514"', '"23503"', '"23001"', '"23502"', '"23P01"']:
            assert literal not in source, f"{module.__name__} declares {literal}"


def test_the_shared_sqlstate_vocabulary_is_the_one_that_is_used() -> None:
    from app.core import errors

    source = code_of(errors)
    # ast.unparse renders string literals single-quoted.
    for literal in ["'23505'", "'23514'", "'23502'"]:
        assert literal in source, f"the shared vocabulary is missing {literal}"


def test_no_service_logs_a_driver_exception() -> None:
    """exc_info on an IntegrityError writes the offending row into the log: guest names,
    review text, amounts, card fragments, invoice references."""
    for module in SERVICES:
        if module.__name__ in HEALTH_MODULES:
            # A connectivity failure carries no row data, and its traceback is precisely
            # what an operator needs when the database is unreachable.
            continue
        assert "exc_info" not in code_of(module), f"{module.__name__} logs exc_info"


# --- analytics and intelligence ----------------------------------------------------------------


def test_intelligence_defines_no_analytics_metric_of_its_own() -> None:
    """One definition of occupancy, room revenue and booking counts in this codebase."""
    from app.services import intelligence

    names = identifiers(intelligence)
    for model in ["BookingRoomNight", "BookingRoom", "Booking", "Revenue", "Expense", "Review"]:
        assert model not in names, f"intelligence references the {model} model directly"
    assert "OCCUPANCY_STATUSES" not in names


def test_intelligence_reaches_the_database_only_through_the_analytics_repository() -> None:
    from app.services import intelligence

    imported = imported_modules(intelligence)
    repositories = {name for name in imported if name.startswith("app.repositories")}

    assert repositories == {"app.repositories.analytics"}


def test_there_is_no_intelligence_repository() -> None:
    assert not any(module.__name__.endswith("intelligence") for module in REPOSITORIES)


def test_the_ml_package_is_free_of_application_imports() -> None:
    """It takes dated observations and returns results; it knows nothing else."""
    from app.ml import timeseries

    for imported in imported_modules(timeseries):
        assert not imported.startswith("app."), f"ml imports {imported}"
        assert not imported.startswith("sqlalchemy"), f"ml imports {imported}"


@pytest.mark.parametrize(
    "name", ["analytics", "intelligence", "ml_accuracy", "ml_drift", "ml_prediction_read"]
)
def test_the_read_only_services_cannot_write(name: str) -> None:
    module = importlib.import_module(f"app.services.{name}")
    source = code_of(module)

    for forbidden in ["commit", "rollback", "flush", "session.add", "insert(", "delete("]:
        assert forbidden not in source, f"{name} contains {forbidden!r}"


# --- the API surface as a whole ------------------------------------------------------------------


def openapi() -> dict:
    return create_app(Settings(environment="test")).openapi()


def test_no_path_carries_an_internal_identifier() -> None:
    """Every addressable resource is named by a public id or a natural key."""
    for path in openapi()["paths"]:
        for segment in path.split("/"):
            if not segment.startswith("{"):
                continue
            name = segment.strip("{}")
            assert name.endswith("public_id") or name in {
                "code",
                "room_type_code",
                "amenity_code",
                "room_number",
            }, f"{path} exposes {name!r}"
            assert name != "id", path


def test_no_response_schema_exposes_an_internal_key() -> None:
    """Audited across every generated component, not a sampled few."""
    schemas = openapi()["components"]["schemas"]
    banned = {"id", "hotel_id", "guest_id", "booking_id", "room_id", "category_id", "payment_id"}

    offenders = {
        f"{name}.{field}"
        for name, schema in schemas.items()
        for field in schema.get("properties", {})
        if field in banned
    }
    assert offenders == set(), offenders


def test_every_domain_collection_is_hotel_scoped_or_a_declared_global() -> None:
    """The only collections allowed outside the hotel hierarchy are the three the SCHEMA
    models as global -- no hotel_id column on any of them."""
    globals_ = {
        "/api/v1/amenities",
        "/api/v1/amenities/{code}",
        "/api/v1/revenue-categories",
        "/api/v1/revenue-categories/{code}",
        "/api/v1/expense-categories",
        "/api/v1/expense-categories/{code}",
    }
    infrastructure = {"/api/v1/", "/health", "/health/db", "/api/v1/hotels"}
    # Stage 4.5.13. Audit events that belong to no property: `audit_events.hotel_id` is NULL
    # for them, so there is no hotel segment to put them under and inventing one would be the
    # falsehood the nullable column exists to avoid. Reached by the platform grant alone --
    # `test_no_hotel_scoped_route_requires_platform_authority` in the authorization surface
    # keeps the converse true, so this exemption cannot be used to smuggle a tenant route out
    # of the hierarchy.
    platform = {"/api/v1/platform/audit-events"}
    # Authentication (Stage 4.1). A user is not owned by a property -- `users` has no
    # hotel_id and no foreign key at all -- so identity cannot sit under the hotel
    # hierarchy. Which hotels a user may reach is Stage 4.2's question.
    identity = {
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        # Stage 4.5.1: changing your own password is account state, not hotel state.
        "/api/v1/auth/change-password",
    }

    for path in openapi()["paths"]:
        if path in globals_ or path in infrastructure or path in identity or path in platform:
            continue
        assert path.startswith("/api/v1/hotels/{"), f"{path} sits outside the hotel hierarchy"


def test_the_three_global_tables_really_have_no_hotel_column() -> None:
    """The justification for those exemptions, checked against the models rather than taken
    on trust."""
    from app.models.finance import ExpenseCategory, RevenueCategory
    from app.models.room import Amenity

    for model in (Amenity, RevenueCategory, ExpenseCategory):
        table = model.metadata.tables[model.__tablename__]
        assert "hotel_id" not in table.columns, model.__tablename__


def test_every_error_response_uses_the_shared_envelope() -> None:
    schemas = openapi()["components"]["schemas"]

    assert set(schemas["ErrorResponse"]["properties"]) == {"error"}
    assert set(schemas["ErrorBody"]["properties"]) == {"code", "message", "details"}


def test_every_collection_declares_the_shared_pagination_envelope() -> None:
    """Page[T] is generated per item type; every one must have the same five fields."""
    schemas = openapi()["components"]["schemas"]
    pages = {name: schema for name, schema in schemas.items() if name.startswith("Page_")}

    assert pages, "no paginated schema was generated -- the audit would be vacuous"
    for name, schema in pages.items():
        assert set(schema["properties"]) == {
            "items",
            "total",
            "page",
            "page_size",
            "pages",
        }, name


def test_every_paginated_route_bounds_its_page_size() -> None:
    """An unbounded page_size is a denial-of-service parameter."""
    paths = openapi()["paths"]
    checked = 0

    for path, operations in paths.items():
        get = operations.get("get")
        if not get:
            continue
        parameters = {p["name"]: p for p in get.get("parameters", [])}
        if "page_size" not in parameters:
            continue
        checked += 1
        schema = parameters["page_size"]["schema"]
        assert schema.get("minimum") == 1, path
        assert schema.get("maximum") == 100, path
        assert parameters["page"]["schema"].get("minimum") == 1, path

    assert checked >= 8, f"only {checked} paginated routes found -- the audit looks incomplete"


def test_no_endpoint_accepts_an_unbounded_free_text_identifier_in_a_path() -> None:
    """Path parameters that are not UUIDs carry an explicit pattern or enum."""
    paths = openapi()["paths"]

    for path, operations in paths.items():
        for verb, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") != "path":
                    continue
                schema = parameter["schema"]
                if schema.get("format") == "uuid":
                    continue
                assert "pattern" in schema or "maxLength" in schema or "enum" in schema, (
                    f"{verb.upper()} {path} parameter {parameter['name']} is unconstrained"
                )


# --- HTTP semantics -------------------------------------------------------------------------------


def test_no_collection_offers_delete_or_patch() -> None:
    """Those verbs belong on a resource, never on a collection."""
    singletons = {
        "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/review",
        # Stage 4.5.11. A booking has exactly one stay and it carries no identifier of its
        # own, so PATCH addresses a resource here just as it does on the review singleton.
        "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/stay",
    }
    infrastructure = {"/api/v1/", "/health", "/health/db"}

    for path, operations in openapi()["paths"].items():
        if path.endswith("}") or path in singletons | infrastructure:
            continue
        assert "delete" not in operations, path
        assert "patch" not in operations, path


def test_put_is_used_nowhere() -> None:
    """Partial update is PATCH throughout; a PUT would imply full replacement semantics no
    domain in this codebase offers."""
    for path, operations in openapi()["paths"].items():
        assert "put" not in operations, path


#: POSTs that perform an ACTION rather than create a resource, mapped to the success code
#: each one declares. Listed explicitly so a new one has to be justified here rather than
#: quietly slipping past the rule, and pinned per-path so admitting a second shape did not
#: loosen what is asserted about the first.
NON_CREATING_POSTS = {
    # Exchanging credentials for a token: 200, and the token is the body.
    "/api/v1/auth/login": "200",
    # Changing your own password: 200 and a token. Stage 4.5.1 returned 204 with no body,
    # which was right while the change revoked nothing. From 4.5.2 the change revokes the
    # caller's own token, so the replacement is not a convenience -- without it a successful
    # change signs the caller out.
    "/api/v1/auth/change-password": "200",
    # Stage 4.5.27. Extending an in-house stay creates no separately addressable
    # resource: the added nights belong to the booking that was already there, and its
    # URL does not change. 201 would promise a Location that does not exist.
    "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/stay/extension": "200",
}


def test_every_creating_route_declares_201() -> None:
    for path, operations in openapi()["paths"].items():
        post = operations.get("post")
        if not post or path in NON_CREATING_POSTS:
            continue
        assert "201" in post["responses"], f"POST {path} does not declare 201"


def test_the_non_creating_posts_declare_their_exact_success_code() -> None:
    """An action creates no resource, so 201 would be a lie about what happened.

    Each path's success code is pinned individually: a route that quietly changed from 204
    to 200 would be changing its contract, and that should be a decision rather than a
    detail nobody notices.
    """
    for path, expected in NON_CREATING_POSTS.items():
        responses = openapi()["paths"][path]["post"]["responses"]
        assert expected in responses, f"POST {path} does not declare {expected}"
        assert "201" not in responses, path


def test_every_deleting_route_declares_204() -> None:
    for path, operations in openapi()["paths"].items():
        delete = operations.get("delete")
        if not delete:
            continue
        assert "204" in delete["responses"], f"DELETE {path} does not declare 204"


def test_every_hotel_scoped_route_declares_404() -> None:
    """An unknown hotel is always a 404, and the contract must say so."""
    for path, operations in openapi()["paths"].items():
        if not path.startswith("/api/v1/hotels/{"):
            continue
        for verb, operation in operations.items():
            assert "404" in operation["responses"], f"{verb.upper()} {path}"


# --- mass assignment ------------------------------------------------------------------------------


def test_no_request_schema_accepts_additional_properties() -> None:
    """extra="forbid" everywhere: an unknown field must be a 422, never silently dropped and
    never mass-assigned."""
    schemas = openapi()["components"]["schemas"]
    request_like = {
        name: schema
        for name, schema in schemas.items()
        if name.endswith(("Create", "Update")) and "properties" in schema
    }

    assert request_like, "no request schema found -- the audit would be vacuous"
    for name, schema in request_like.items():
        assert schema.get("additionalProperties") is False, f"{name} allows extra fields"


def test_no_update_schema_exposes_an_identity_field() -> None:
    """A URL identity must not be reassignable through the body."""
    schemas = openapi()["components"]["schemas"]

    for name, schema in schemas.items():
        if not name.endswith("Update"):
            continue
        properties = set(schema.get("properties", {}))
        for identity in ["id", "public_id", "code", "slug", "hotel_id", "hotel_public_id"]:
            assert identity not in properties, f"{name} exposes {identity!r}"


def test_no_create_schema_accepts_a_server_assigned_identifier() -> None:
    schemas = openapi()["components"]["schemas"]

    for name, schema in schemas.items():
        if not name.endswith("Create"):
            continue
        properties = set(schema.get("properties", {}))
        for assigned in ["id", "public_id", "created_at", "updated_at"]:
            assert assigned not in properties, f"{name} accepts {assigned!r}"


def test_no_schema_exposes_a_generated_column_as_writable() -> None:
    """rating_normalized, nights, rating and the metrics ratios are GENERATED ALWAYS;
    PostgreSQL rejects a direct write with 428C9."""
    schemas = openapi()["components"]["schemas"]

    for name, schema in schemas.items():
        if not name.endswith(("Create", "Update")):
            continue
        properties = set(schema.get("properties", {}))
        for generated in ["rating_normalized", "nights", "occupancy_rate", "adr", "revpar"]:
            assert generated not in properties, f"{name} accepts generated column {generated!r}"


# --- the excluded surface -------------------------------------------------------------------------


def test_authorization_exists_with_no_administration_surface() -> None:
    """Authorization exists at two levels, and neither can be granted through the API.

    A hotel membership is granted by creating a hotel, and otherwise out of band. A platform
    grant is only ever out of band -- Stage 4.3 added the privilege that can edit every
    hotel's shared vocabulary and deliberately added no endpoint for handing it out.

    There is still no membership endpoint, no role endpoint, no invitation flow and no
    admin console. Each would be a privilege-GRANTING surface, which is the kind that has to
    be noticed in the commit that adds it rather than in review later.
    """
    paths = set(openapi()["paths"])

    assert {"/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/me"} <= paths
    for banned in (
        "oauth",
        "openid",
        "permission",
        "role",
        "grant",
        "policy",
        "membership",
        "invite",
        "admin",
    ):
        assert not any(banned in path for path in paths), banned


def test_no_infrastructure_or_agentic_dependency_was_introduced() -> None:
    for module in [*ROUTERS, *REPOSITORIES, *SERVICES]:
        imported = imported_modules(module)
        for banned in ["celery", "redis", "kafka", "openai", "anthropic", "langchain", "httpx"]:
            assert not any(name.startswith(banned) for name in imported), (
                f"{module.__name__} imports {banned}"
            )


def test_no_operational_action_endpoint_exists() -> None:
    """Nothing in this backend changes a rate, moves a booking or allocates a room by
    itself."""
    paths = set(openapi()["paths"])

    for banned in ("pricing", "recommend", "optimi", "auto", "apply", "execute"):
        assert not any(banned in path for path in paths), banned


# --- Stage 4.2: authorization lives in exactly one place ------------------------------------------
#
# The whole point of the design is that "may they?" is answered once. Forty-eight domain query
# sites each deciding for themselves is how tenant isolation bugs are born -- one of them
# forgets, and the forgetting is invisible until someone finds it from the outside. These rules
# pin the concentration itself, not just its current behaviour.


#: The only module allowed to compare roles.
POLICY_MODULE = "app.services.authorization"

#: The only modules allowed to know a user exists.
IDENTITY_AWARE = {
    "app.repositories.user",  # authentication's own storage
    "app.repositories.membership",  # the user-to-hotel relation, and nothing else
    "app.repositories.platform_admin",  # the user-to-platform grant, and nothing else
    "app.services.auth",  # issues the token
    POLICY_MODULE,  # answers "may they?", for both scopes
    "app.services.hotel",  # creates the owner membership in the same transaction
    "app.services.membership",  # Stage 4.4: memberships ARE its domain
    # Stage 4.5.12. The audit trail records WHO acted and reports it back, so knowing that
    # users exist is the table's entire purpose rather than a capability it grew. Admitted on
    # the same footing as the membership repository, and with the same limit: the guards that
    # matter are unchanged for it -- it compares no role, reads no membership, and raises no
    # authorization error. `test_only_one_module_compares_roles` and
    # `test_no_layer_module_raises_forbidden_for_itself` still cover it, because this list
    # exempts a module from neither.
    "app.repositories.audit",
    # Stage 4.5.14. The archive denormalises the actor's PUBLIC id beside the internal one, so
    # an archived row identifies who acted without joining a table that may one day not have
    # the row. Admitted on exactly the same footing as the repository above, with the same
    # limit: it compares no role, reads no membership, and raises no authorization error.
    "app.repositories.audit_archive",
}


def test_only_one_module_compares_roles() -> None:
    """Rank comparison is the authorization decision. If a second module can make it, the
    matrix has two sources of truth and they will diverge."""
    offenders = []
    for module in [*ROUTERS, *REPOSITORIES, *SERVICES]:
        if module.__name__ == POLICY_MODULE:
            continue
        names = identifiers(module)
        if "ROLE_RANK" in names or "outranks_or_equals" in names or "rank" in names:
            offenders.append(module.__name__)

    assert offenders == [], f"these compare roles outside the policy: {offenders}"


def test_no_layer_module_raises_forbidden_for_itself() -> None:
    """403 is the policy's word.

    Checked across routers, repositories and services -- the three layers this audit walks.
    Both 403s in this codebase come from :mod:`app.services.authorization`: the hotel policy's
    rank check, and (from Stage 4.3) the platform policy's grant check. ``app.api.deps``
    declares the requirement on a route but raises nothing itself.
    """
    offenders = [
        module.__name__
        for module in [*ROUTERS, *REPOSITORIES, *SERVICES]
        if module.__name__ != POLICY_MODULE and "ForbiddenError" in identifiers(module)
    ]

    assert offenders == [], f"these raise ForbiddenError themselves: {offenders}"


def test_no_domain_repository_knows_that_users_exist() -> None:
    """A repository that took a user would be a repository that could be asked the wrong
    question. Only the membership repository joins the two worlds."""
    offenders = []
    for module in REPOSITORIES:
        if module.__name__ in IDENTITY_AWARE:
            continue
        imported = imported_modules(module)
        if {"app.models.user", "app.models.membership"} & imported:
            offenders.append(module.__name__)

    assert offenders == [], f"these repositories import identity models: {offenders}"


@pytest.mark.parametrize("module", DOMAIN_REPOSITORIES, ids=ids(DOMAIN_REPOSITORIES))
def test_no_domain_repository_accepts_a_user_identity(module: ModuleType) -> None:
    """Not by import, and not by argument either: the second is how the rule usually erodes."""
    if module.__name__ in IDENTITY_AWARE:
        pytest.skip("the membership repository is the one that legitimately takes a user_id")

    tree = ast.parse(inspect.getsource(module))
    taken = {
        argument.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for argument in [*node.args.args, *node.args.kwonlyargs]
    }

    for forbidden in ("user", "user_id", "current_user", "principal", "actor"):
        assert forbidden not in taken, f"{module.__name__} takes {forbidden}"


def test_no_domain_service_performs_its_own_membership_check() -> None:
    """Services resolve a hotel through the scope resolver; the resolver consults the policy.
    A service reading ``user_hotels`` for itself would be a second, unreviewed gate."""
    offenders = []
    for module in DOMAIN_SERVICES:
        if module.__name__ in IDENTITY_AWARE:
            continue
        names = identifiers(module)
        for forbidden in ("MembershipRepository", "UserHotel", "role_for", "hotel_ids_for"):
            if forbidden in names:
                offenders.append(f"{module.__name__} uses {forbidden}")

    assert offenders == [], offenders


def test_the_scope_resolver_still_resolves() -> None:
    """Stage 4.2 added authorization to the resolver; it must not have replaced resolution
    with it, or an unknown hotel would report the wrong thing to a legitimate member."""
    from app.services.scope import HotelScopeResolver

    assert hasattr(HotelScopeResolver, "require_hotel")
    assert hasattr(HotelScopeResolver, "require_hotel_with_role")


#: The only modules allowed to know platform authority exists.
#:
#: Stage 4.2 asserted that NOTHING knew -- that guard was the reason the catalogues were
#: read-only, and Stage 4.3 was authorised to reverse it. What replaces it is narrower than a
#: ban and more useful: the concept exists in exactly four places, and the list is what a
#: reviewer reads to see that it has not spread into a domain service.
PLATFORM_AWARE = {
    "app.repositories.platform_admin",  # reads the grant
    POLICY_MODULE,  # decides on it
    "app.api.v1.endpoints.amenities",  # declares the requirement on its writes
    "app.api.v1.endpoints.revenue_categories",
    "app.api.v1.endpoints.expense_categories",
    # Stage 4.5.13. The platform audit ROUTER declares the same requirement on its single GET,
    # in the same way the three above declare it -- one `Depends(require_platform_admin)` on
    # the route, where a reviewer reads it.
    #
    # The admission is narrow on purpose and the shape of the list is why it stays useful:
    # every entry is a router or the policy itself, and no SERVICE has ever been admitted.
    # `PlatformAuditQueryService` deliberately does NOT appear here -- it decides no
    # authorization, holds no policy, and would fail this test if it tried to.
    "app.api.v1.endpoints.platform_audit",
}

#: Names that would mean somebody invented a second, informal platform role. Stage 4.3
#: authorised exactly one, spelled `platform_admin`, held in a CHECK-constrained column.
INVENTED_ROLES = ("is_admin", "is_superuser", "is_staff", "superuser", "root", "sysadmin")


def test_no_invented_administrative_role_exists() -> None:
    """One platform capability was authorised. A second arriving as a boolean flag on some
    service would be an authorization model nobody reviewed."""
    offenders = []
    for module in [*ROUTERS, *REPOSITORIES, *SERVICES]:
        names = identifiers(module)
        for forbidden in INVENTED_ROLES:
            if forbidden in names:
                offenders.append(f"{module.__name__} defines {forbidden}")

    assert offenders == [], offenders


def test_platform_authority_is_confined_to_the_modules_that_declare_it() -> None:
    """The privilege must not leak into the domain.

    A service that could ask "is this caller a platform admin?" could grow an exception to a
    tenant rule, and that exception would be invisible from the route.
    """
    offenders = []
    for module in [*ROUTERS, *REPOSITORIES, *SERVICES]:
        if module.__name__ in PLATFORM_AWARE:
            continue
        names = identifiers(module)
        for forbidden in (
            "PlatformAdmin",
            "PlatformAdminRepository",
            "PlatformAccessPolicy",
            "PlatformRole",
            "require_platform_admin",
            "is_platform_admin",
        ):
            if forbidden in names:
                offenders.append(f"{module.__name__} uses {forbidden}")

    assert offenders == [], offenders


def test_the_hotel_policy_cannot_see_platform_authority() -> None:
    """The bypass this stage exists to avoid, asserted at its narrowest point.

    If ``HotelAccessPolicy`` could read a platform grant, one ``or`` would turn the 404 wall
    into a door. It is given no repository that reaches ``platform_admins``, and its source
    names nothing platform-shaped.
    """
    import inspect as _inspect
    import textwrap

    from app.services.authorization import HotelAccessPolicy

    tree = ast.parse(textwrap.dedent(_inspect.getsource(HotelAccessPolicy)))
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }
    for forbidden in ("PlatformAdminRepository", "PlatformAccessPolicy", "PlatformRole"):
        assert forbidden not in names, f"HotelAccessPolicy reaches {forbidden}"

    # And on the code with its prose removed, so an attribute this project has not thought of
    # is caught too. The docstrings legitimately discuss the platform policy; the CODE must
    # not mention it at all.
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree).lower()

    assert "platform" not in code, "HotelAccessPolicy's code mentions the platform"
    assert "admin" not in code, "HotelAccessPolicy's code mentions an administrator"


def test_the_two_policies_share_no_repository() -> None:
    """Constructed from different tables, so neither can answer the other's question."""
    import inspect as _inspect

    from app.services.authorization import HotelAccessPolicy, PlatformAccessPolicy

    hotel_params = set(_inspect.signature(HotelAccessPolicy.__init__).parameters)
    platform_params = set(_inspect.signature(PlatformAccessPolicy.__init__).parameters)

    assert hotel_params == {"self", "user", "memberships"}
    assert platform_params == {"self", "user", "admins"}


def test_the_platform_role_is_not_ranked_against_the_hotel_roles() -> None:
    """A shared ordering is how "platform admin outranks owner" gets written by accident."""
    from app.models.enums import ROLE_RANK, HotelRole, PlatformRole

    assert set(ROLE_RANK) == set(HotelRole)
    assert not hasattr(PlatformRole, "rank")
    assert [role.value for role in PlatformRole] == ["platform_admin"]
    assert set(PlatformRole.values()) & set(HotelRole.values()) == set()


def test_no_request_schema_accepts_a_platform_role() -> None:
    """The grant is server-side state. A payload field naming it -- even one the service
    ignores -- is an invitation for a later handler to read it."""
    schemas = openapi()["components"]["schemas"]
    offenders = []
    for name, schema in schemas.items():
        for field in schema.get("properties", {}):
            if any(
                token in field.lower()
                for token in ("platform", "is_admin", "superuser", "privilege")
            ):
                offenders.append(f"{name}.{field}")

    assert offenders == [], offenders


def test_the_token_declares_no_authority() -> None:
    """Read from the signing function itself, so a claim added later fails here."""
    import ast
    import inspect as _inspect

    from app.core import security

    tree = ast.parse(_inspect.getsource(security.create_access_token))
    claims = {
        node.value
        for call in ast.walk(tree)
        if isinstance(call, ast.Dict)
        for node in call.keys
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert claims == {"sub", "iat", "exp", "jti", "typ"}


def test_the_role_vocabulary_is_exactly_the_four_authorised_roles() -> None:
    """The database CHECK admits four values; a fifth in Python would be accepted by the
    application and rejected by PostgreSQL, which is the worst of both."""
    from app.models.enums import ROLE_RANK, HotelRole

    assert [role.value for role in HotelRole] == ["viewer", "staff", "manager", "owner"]
    assert [ROLE_RANK[role] for role in HotelRole] == [0, 1, 2, 3]


# --- Stage 4.4: membership administration -------------------------------------------------------
#
# Membership is the one domain whose rows decide authorization for every other domain. That
# makes two things worth pinning structurally: that administering it did not become a second
# place where "may they?" is answered, and that it stayed hotel-scoped -- a flat route over
# memberships would sit outside the resolver that makes the 404 wall work.


def test_membership_administration_is_reached_only_through_the_hotel() -> None:
    """No flat route over users or memberships.

    ``GET /users/{id}/memberships`` would answer a question no single hotel is entitled to ask
    -- which OTHER properties a colleague works at -- and would do it outside
    ``HotelScopeResolver``, so the 404 wall would not apply to it.
    """
    paths = set(openapi()["paths"])

    membership_paths = [p for p in paths if "member" in p.lower()]
    assert membership_paths, "no membership routes found -- has the prefix changed?"
    for path in membership_paths:
        assert path.startswith("/api/v1/hotels/{hotel_public_id}/"), path
    for path in paths:
        assert not path.startswith("/api/v1/users"), f"{path} addresses users outside a hotel"


def test_the_membership_service_decides_no_authorization() -> None:
    """It resolves through the scope resolver like every other hotel-scoped service.

    The ownership rules it DOES own are integrity rules, and they live in the policy module;
    what must not appear here is a rank comparison or a 403.
    """
    import app.services.membership as membership_service

    names = identifiers(membership_service)

    for forbidden in ("ForbiddenError", "ROLE_RANK", "outranks_or_equals", "role_for"):
        assert forbidden not in names, f"the membership service performs its own {forbidden}"
    assert "HotelScopeResolver" in names, "it must resolve through the shared resolver"


def test_the_ownership_rule_lives_in_exactly_one_place() -> None:
    """One rule, one home. A second copy would be the one that gets forgotten."""
    offenders = []
    for module in [*ROUTERS, *REPOSITORIES, *SERVICES]:
        if module.__name__ in {POLICY_MODULE, "app.services.membership"}:
            continue
        if "MembershipPolicy" in identifiers(module):
            offenders.append(module.__name__)

    assert offenders == [], offenders


def test_the_ownership_rule_reads_no_table_of_its_own() -> None:
    """The policy is handed counts the service has locked.

    If it queried for itself it would read outside that lock, and a rule that decides on an
    unlocked count is a rule that loses a race.
    """
    import ast
    import inspect as _inspect
    import textwrap

    from app.services.authorization import MembershipPolicy

    tree = ast.parse(textwrap.dedent(_inspect.getsource(MembershipPolicy)))
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }

    for forbidden in ("select", "session", "_session", "execute", "MembershipRepository"):
        assert forbidden not in names, f"MembershipPolicy reaches {forbidden}"


def test_the_membership_repository_takes_the_lock_the_rule_depends_on() -> None:
    """The invariant is only as strong as this one statement.

    Asserted structurally as well as behaviourally: the integration suite proves a second
    transaction blocks, and this proves the lock has not quietly been dropped from the query
    that the suite's guard would then no longer cover.
    """
    import inspect as _inspect
    import textwrap

    from app.repositories.membership import MembershipRepository

    source = textwrap.dedent(_inspect.getsource(MembershipRepository.lock_owner_ids))
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "with_for_update" in called, "lock_owner_ids no longer locks anything"


def test_no_membership_schema_accepts_a_hotel_or_an_internal_key() -> None:
    """The hotel comes from the URL. A payload that could name one could name a different
    one, which is the shape of every cross-tenant write bug."""
    schemas = openapi()["components"]["schemas"]
    offenders = []
    for name, schema in schemas.items():
        if "Member" not in name:
            continue
        for field in schema.get("properties", {}):
            if field in {"hotel_id", "hotel_public_id", "user_id", "id"}:
                offenders.append(f"{name}.{field}")

    assert offenders == [], offenders


def test_the_member_response_exposes_no_account_secret() -> None:
    """Built field by field in the service, so a column added to `users` later cannot appear
    in a membership listing by default. Pinned against the published contract."""
    properties = set(openapi()["components"]["schemas"]["MemberResponse"]["properties"])

    assert properties == {
        "user_public_id",
        "email",
        "full_name",
        "is_active",
        "role",
        "joined_at",
    }


def test_the_hotel_role_vocabulary_did_not_gain_a_platform_role() -> None:
    """Stage 4.4 grants hotel roles through an API for the first time. The two vocabularies
    must still be disjoint, or `platform_admin` becomes grantable by any hotel owner."""
    from app.models.enums import HotelRole, PlatformRole

    assert [role.value for role in HotelRole] == ["viewer", "staff", "manager", "owner"]
    assert set(HotelRole.values()) & set(PlatformRole.values()) == set()
