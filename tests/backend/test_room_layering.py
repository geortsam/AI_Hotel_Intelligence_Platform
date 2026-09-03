"""Room layering, schemas and the two-level scoping rule.

Needs no database: these assert the *structure* -- that the router holds no queries, that the
service owns the unit of work, that the repository offers no unscoped lookup. The behavioural
proof against live PostgreSQL is in ``tests/integration/test_rooms_api.py``.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any, get_args

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api import deps
from app.api.v1.endpoints import rooms as rooms_router
from app.core.config import Settings
from app.main import create_app
from app.models.enums import RoomStatus
from app.repositories.room import RoomRepository
from app.schemas.room import RoomCreate, RoomResponse, RoomStatusLiteral, RoomUpdate
from app.services.room import RoomService
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy


def base_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"room_number": "101"}
    body.update(overrides)
    return body


# --- 25-26. layer separation --------------------------------------------------------------


def test_router_contains_no_persistence_logic() -> None:
    source = inspect.getsource(rooms_router)

    for forbidden in ["select(", "session.", "Session", "sqlalchemy", "commit"]:
        assert forbidden not in source, f"router references {forbidden!r}"


def test_service_contains_no_sqlalchemy_query_expressions() -> None:
    source = inspect.getsource(inspect.getmodule(RoomService))  # type: ignore[arg-type]

    for forbidden in ["select(", "text(", "func.count", "order_by"]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_repository_never_commits() -> None:
    source = inspect.getsource(inspect.getmodule(RoomRepository))  # type: ignore[arg-type]

    assert "commit()" not in source


def test_service_owns_the_transaction() -> None:
    source = inspect.getsource(inspect.getmodule(RoomService))  # type: ignore[arg-type]

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


def test_repository_uses_the_core_delete_technique() -> None:
    """PostgreSQL's RESTRICT stays authoritative; the ORM's nullify cascade is bypassed."""
    source = inspect.getsource(inspect.getmodule(RoomRepository))  # type: ignore[arg-type]

    assert "sql_delete" in source
    assert "self._session.delete(" not in source


# --- 24. repository scoping ------------------------------------------------------------------


def test_repository_offers_no_unscoped_lookup() -> None:
    """A room number is unique only within a hotel. Any lookup that could reach outside the
    requested scope must not exist to be called by mistake."""
    methods = {name for name in dir(RoomRepository) if not name.startswith("_")}

    assert "get_in_room_type" in methods
    assert "get_in_hotel" in methods
    for forbidden in ["get_by_number", "get_by_code", "get_by_id", "list_page", "get"]:
        assert forbidden not in methods, f"unscoped lookup {forbidden!r} exists"


def test_every_repository_query_is_hotel_scoped() -> None:
    for method_name in [
        "get_in_room_type",
        "get_in_hotel",
        "number_exists_in_hotel",
        "count_in_room_type",
        "list_page_in_room_type",
    ]:
        parameters = inspect.signature(getattr(RoomRepository, method_name)).parameters
        assert "hotel_id" in parameters, method_name


def test_room_type_scoped_methods_also_take_the_room_type() -> None:
    for method_name in ["get_in_room_type", "count_in_room_type", "list_page_in_room_type"]:
        parameters = inspect.signature(getattr(RoomRepository, method_name)).parameters
        assert "room_type_id" in parameters, method_name


# --- shared resolver seam ----------------------------------------------------------------------


def test_resolution_logic_is_shared_not_duplicated() -> None:
    """Both nested services resolve the parent chain through the same object."""
    import app.services.room_type as room_type_module

    room_source = inspect.getsource(inspect.getmodule(RoomService))  # type: ignore[arg-type]
    type_source = inspect.getsource(room_type_module)

    for source in (room_source, type_source):
        assert "HotelScopeResolver" in source
        # The lookup itself lives in the resolver, not copied into each service.
        assert "get_by_public_id" not in source


def test_dependency_assembles_the_room_service_over_the_shared_resolver() -> None:
    class _Session:
        pass

    stub: Any = _Session()
    scope = deps.get_scope_resolver(stub, AllowAllPolicy())
    service = deps.get_room_service(stub, scope)

    assert isinstance(service, RoomService)
    assert isinstance(service._repository, RoomRepository)
    assert isinstance(service._scope, HotelScopeResolver)
    assert service._session is stub


def test_resolver_reports_the_hotel_before_the_room_type() -> None:
    """A caller must be able to tell which half of the path is wrong."""
    source = inspect.getsource(HotelScopeResolver.require_hotel_and_room_type)

    assert source.index("require_hotel") < source.index("require_room_type")


# --- 18. status vocabulary comes from the frozen schema ------------------------------------------


def test_status_literal_matches_the_database_check_constraint_exactly() -> None:
    """No status is invented, and none of the schema's is omitted."""
    assert set(get_args(RoomStatusLiteral)) == set(RoomStatus.values())


def test_every_schema_status_is_accepted() -> None:
    for status_value in RoomStatus.values():
        assert RoomCreate.model_validate(base_payload(status=status_value)).status == (status_value)


@pytest.mark.parametrize("status_value", ["booked", "dirty", "vacant", "AVAILABLE", ""])
def test_invented_statuses_are_rejected(status_value: str) -> None:
    with pytest.raises(PydanticValidationError):
        RoomCreate.model_validate(base_payload(status=status_value))


def test_status_defaults_to_available() -> None:
    assert RoomCreate.model_validate(base_payload()).status == "available"


# --- schemas -------------------------------------------------------------------------------------


def test_create_schema_has_no_hotel_or_room_type_field() -> None:
    """Both come from the path. Accepting them here would let a client name a room type the
    URL never authorised."""
    for forbidden in ["hotel_id", "hotel_public_id", "room_type_id", "room_type_code"]:
        assert forbidden not in RoomCreate.model_fields


def test_create_schema_requires_a_room_number() -> None:
    with pytest.raises(PydanticValidationError) as excinfo:
        RoomCreate.model_validate({})

    assert {"room_number"} <= {error["loc"][0] for error in excinfo.value.errors()}


@pytest.mark.parametrize("number", ["has space", "-leading", ".leading", "x" * 21, ""])
def test_room_number_pattern_rejects_unusable_values(number: str) -> None:
    """The number travels in a URL path, so its shape is constrained."""
    with pytest.raises(PydanticValidationError):
        RoomCreate.model_validate(base_payload(room_number=number))


@pytest.mark.parametrize("number", ["101", "12A", "P-3", "B.2"])
def test_room_number_pattern_accepts_real_room_numbers(number: str) -> None:
    assert RoomCreate.model_validate(base_payload(room_number=number)).room_number == number


def test_room_number_is_upper_cased() -> None:
    """PostgreSQL's unique constraint is case-sensitive, so 101a and 101A would otherwise be
    two different rooms in the same corridor."""
    assert RoomCreate.model_validate(base_payload(room_number="12a")).room_number == "12A"


# --- 16. identity immutability -------------------------------------------------------------------


def test_update_schema_cannot_change_the_room_number() -> None:
    assert "room_number" not in RoomUpdate.model_fields


def test_update_schema_cannot_move_a_room_between_types_or_hotels() -> None:
    for forbidden in ["room_type_id", "room_type_code", "hotel_id", "hotel_public_id"]:
        assert forbidden not in RoomUpdate.model_fields


def test_update_schema_forbids_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        RoomUpdate.model_validate({"nmae": "typo"})


def test_update_schema_fields_are_all_optional() -> None:
    assert RoomUpdate.model_validate({}).model_dump(exclude_unset=True) == {}


def test_update_distinguishes_omitted_from_explicit_null() -> None:
    omitted = RoomUpdate.model_validate({"status": "cleaning"})
    explicit = RoomUpdate.model_validate({"status": "cleaning", "notes": None})

    assert omitted.model_dump(exclude_unset=True) == {"status": "cleaning"}
    assert explicit.model_dump(exclude_unset=True) == {"status": "cleaning", "notes": None}


# --- 21. response schema -------------------------------------------------------------------------


def test_response_exposes_no_internal_keys() -> None:
    fields = set(RoomResponse.model_fields)

    for forbidden in ["id", "hotel_id", "room_type_id", "public_id"]:
        assert forbidden not in fields
    # Enough to rebuild the resource's own URL from the payload alone.
    assert {"hotel_public_id", "room_type_code", "room_number"} <= fields


# --- service behaviour without a database --------------------------------------------------------


class _NoHotels:
    def get_by_public_id(self, _: uuid.UUID) -> None:
        return None


class _NoRoomTypes:
    def get_by_hotel_and_code(self, _hotel_id: int, _code: str) -> None:
        return None


class _StubSession:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def test_missing_hotel_is_reported_before_any_room_work() -> None:
    from app.core.errors import NotFoundError

    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    service = RoomService(_StubSession(), object(), scope)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        service.get(uuid.uuid4(), "DLX", "101")


# --- OpenAPI surface and stage guards ------------------------------------------------------------


def test_room_routes_are_nested_under_hotel_and_room_type() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    collection = "/api/v1/hotels/{hotel_public_id}/room-types/{room_type_code}/rooms"
    item = collection + "/{room_number}"
    assert set(paths[collection]) == {"post", "get"}
    assert set(paths[item]) == {"get", "patch", "delete"}


def test_no_flat_room_route_exists() -> None:
    """The nested path is the only way in; a flat one would lose the hierarchy scope."""
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any(p.startswith("/api/v1/rooms") for p in paths)


def test_no_internal_room_id_appears_in_any_path() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any("room_id" in p for p in paths)
