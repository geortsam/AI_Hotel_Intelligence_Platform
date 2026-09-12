"""Amenity layering, schemas and the global-vs-scoped distinction.

The interesting structural claim in this domain is that the two halves are scoped
*differently* on purpose: the catalogue is global because ``uq_amenities_code`` has no tenant
column, while assignments are hierarchy-scoped like every other nested resource. These tests
pin both, so a later refactor cannot quietly make the catalogue tenant-scoped or the
assignments global.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api import deps
from app.api.v1.endpoints import amenities as amenities_router
from app.api.v1.endpoints import room_type_amenities as assignments_router
from app.core.config import Settings
from app.main import create_app
from app.models.room import Amenity
from app.repositories.amenity import AmenityRepository, RoomTypeAmenityRepository
from app.schemas.amenity import (
    AmenityAssignment,
    AmenityCreate,
    AmenityResponse,
    AmenityUpdate,
)
from app.services.amenity import AmenityService, RoomTypeAmenityService
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy, audit_trail

# --- 25. routers hold no persistence logic -----------------------------------------------


@pytest.mark.parametrize("module", [amenities_router, assignments_router])
def test_routers_contain_no_sqlalchemy(module: object) -> None:
    source = inspect.getsource(module)  # type: ignore[arg-type]

    for forbidden in ["select(", "session.", "Session", "sqlalchemy", "commit"]:
        assert forbidden not in source, f"router references {forbidden!r}"


# --- 24. services own the transaction; 23. repositories never commit ----------------------


def test_services_contain_no_sqlalchemy_query_expressions() -> None:
    source = inspect.getsource(inspect.getmodule(AmenityService))  # type: ignore[arg-type]

    for forbidden in ["select(", "text(", "func.count", "order_by", "join("]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_repositories_never_commit() -> None:
    source = inspect.getsource(inspect.getmodule(AmenityRepository))  # type: ignore[arg-type]

    assert "commit()" not in source


def test_services_own_commit_and_rollback() -> None:
    source = inspect.getsource(inspect.getmodule(AmenityService))  # type: ignore[arg-type]

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


def test_amenity_delete_uses_the_core_delete_technique() -> None:
    """RESTRICT must be the database's decision, not the ORM's."""
    source = inspect.getsource(inspect.getmodule(AmenityRepository))  # type: ignore[arg-type]

    assert "sql_delete" in source
    assert "self._session.delete(" not in source


# --- 26. scoping: global catalogue vs hierarchy-scoped associations -------------------------


def test_catalogue_lookup_is_global_because_the_unique_constraint_is() -> None:
    """``uq_amenities_code`` has no tenant column, so an unscoped get_by_code is correct
    here -- the one place in the project where that is true."""
    parameters = inspect.signature(AmenityRepository.get_by_code).parameters

    assert set(parameters) == {"self", "code"}


def test_association_lookups_are_all_scoped_to_a_room_type() -> None:
    for method_name in [
        "assign",
        "is_assigned",
        "count_for_room_type",
        "list_page_for_room_type",
        "unassign",
    ]:
        parameters = inspect.signature(getattr(RoomTypeAmenityRepository, method_name)).parameters
        assert "room_type_id" in parameters, method_name


def test_association_repository_offers_no_unscoped_listing() -> None:
    """Nothing may enumerate assignments across hotels."""
    methods = {name for name in dir(RoomTypeAmenityRepository) if not name.startswith("_")}

    for forbidden in ["list_all", "list_page", "get_by_amenity", "count"]:
        assert forbidden not in methods, f"unscoped method {forbidden!r} exists"


def test_assignment_service_resolves_through_the_shared_scope_resolver() -> None:
    source = inspect.getsource(inspect.getmodule(RoomTypeAmenityService))  # type: ignore[arg-type]

    assert "require_hotel_and_room_type" in source


def test_catalogue_service_takes_no_scope_resolver() -> None:
    """Threading a hotel through a global catalogue would imply ownership the schema does
    not model."""
    parameters = inspect.signature(AmenityService.__init__).parameters

    assert "scope" not in parameters
    # Stage 4.5.12 added `audit`. The guard this test exists for is the line above --
    # no hotel scope on a global catalogue -- and it is unchanged; the exact set is
    # extended rather than relaxed, so a THIRD collaborator would still fail here.
    assert set(parameters) == {"self", "session", "repository", "audit"}


def test_dependencies_assemble_both_services() -> None:
    class _Session:
        pass

    stub: Any = _Session()

    catalogue = deps.get_amenity_service(stub, audit_trail(stub))
    assert isinstance(catalogue, AmenityService)
    assert isinstance(catalogue._repository, AmenityRepository)

    scope = deps.get_scope_resolver(stub, AllowAllPolicy())
    assignments = deps.get_room_type_amenity_service(stub, scope)
    assert isinstance(assignments, RoomTypeAmenityService)
    assert isinstance(assignments._associations, RoomTypeAmenityRepository)
    assert isinstance(assignments._amenities, AmenityRepository)
    assert isinstance(assignments._scope, HotelScopeResolver)


# --- schemas -------------------------------------------------------------------------------


def test_create_requires_code_and_name() -> None:
    with pytest.raises(PydanticValidationError) as excinfo:
        AmenityCreate.model_validate({})

    assert {"code", "name"} <= {error["loc"][0] for error in excinfo.value.errors()}


def test_category_is_optional() -> None:
    assert AmenityCreate.model_validate({"code": "WIFI", "name": "WiFi"}).category is None


def test_code_is_upper_cased() -> None:
    """The unique constraint is case-sensitive, so wifi and WIFI would otherwise be two
    entries for one concept."""
    assert AmenityCreate.model_validate({"code": "wifi", "name": "WiFi"}).code == "WIFI"


@pytest.mark.parametrize("code", ["has space", "-leading", "_leading", "x" * 51, ""])
def test_code_pattern_rejects_unusable_values(code: str) -> None:
    with pytest.raises(PydanticValidationError):
        AmenityCreate.model_validate({"code": code, "name": "Something"})


# --- 9. forbidden and internal fields ---------------------------------------------------------


@pytest.mark.parametrize("field", ["id", "hotel_id", "room_type_id", "amenity_id"])
def test_create_rejects_internal_identifiers(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        AmenityCreate.model_validate({"code": "WIFI", "name": "WiFi", field: 1})


def test_update_has_no_code_field() -> None:
    """The code is the URL identity, exactly as slug is for a hotel."""
    assert "code" not in AmenityUpdate.model_fields


def test_update_forbids_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        AmenityUpdate.model_validate({"nmae": "typo"})


def test_assignment_payload_carries_only_a_code() -> None:
    """Hotel and room type come from the path; the junction table has no attributes."""
    assert set(AmenityAssignment.model_fields) == {"code"}


@pytest.mark.parametrize("field", ["room_type_id", "amenity_id", "hotel_public_id"])
def test_assignment_payload_rejects_identifiers_the_url_determines(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        AmenityAssignment.model_validate({"code": "WIFI", field: 1})


# --- 27. response exposes no internal ids, and invents no timestamps ------------------------------


def test_response_is_exactly_the_three_business_columns() -> None:
    assert set(AmenityResponse.model_fields) == {"code", "name", "category"}


def test_response_exposes_no_internal_id() -> None:
    assert "id" not in AmenityResponse.model_fields


def test_response_invents_no_timestamps() -> None:
    """``Amenity`` does not use TimestampMixin: the table has no created_at/updated_at, and
    the API must not pretend otherwise."""
    assert "created_at" not in AmenityResponse.model_fields
    assert "updated_at" not in AmenityResponse.model_fields
    assert not hasattr(Amenity, "created_at")


# --- service behaviour without a database ------------------------------------------------------


class _EmptyCatalogue:
    def get_by_code(self, _: str) -> None:
        return None


class _NoHotels:
    def get_by_public_id(self, _: uuid.UUID) -> None:
        return None


class _NoRoomTypes:
    def get_by_hotel_and_code(self, _hotel_id: int, _code: str) -> None:
        return None


class _StubSession:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def test_missing_amenity_raises_not_found() -> None:
    from app.core.errors import NotFoundError

    service = AmenityService(_StubSession(), _EmptyCatalogue(), audit_trail())  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Amenity not found\."):
        service.get("NOSUCH")


def test_missing_hotel_is_reported_before_the_amenity_is_looked_up() -> None:
    from app.core.errors import NotFoundError

    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    service = RoomTypeAmenityService(_StubSession(), object(), _EmptyCatalogue(), scope)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        service.assign(uuid.uuid4(), "DLX", AmenityAssignment(code="WIFI"))


# --- OpenAPI surface -----------------------------------------------------------------------------


def test_catalogue_endpoints_are_registered_at_the_top_level() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    assert set(paths["/api/v1/amenities"]) == {"post", "get"}
    assert set(paths["/api/v1/amenities/{code}"]) == {"get", "patch", "delete"}


def test_assignment_endpoints_are_nested_under_the_room_type() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    collection = "/api/v1/hotels/{hotel_public_id}/room-types/{room_type_code}/amenities"
    assert set(paths[collection]) == {"get", "post"}
    assert set(paths[collection + "/{amenity_code}"]) == {"delete"}


def test_no_internal_id_appears_in_any_amenity_path() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any("amenity_id" in p and "amenity_code" not in p for p in paths)
