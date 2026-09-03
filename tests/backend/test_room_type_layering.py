"""Room type layering, schemas and the hotel-scoped identity rule.

Needs no database: these assert the *structure* -- that the router holds no queries, that
the service owns the unit of work, that the repository offers no code-only lookup. The
behavioural proof against live PostgreSQL is in ``tests/integration/test_room_types_api.py``.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api import deps
from app.api.v1.endpoints import room_types as room_types_router
from app.core.config import Settings
from app.core.errors import (
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    ConflictError,
    NotFoundError,
    sqlstate_of,
)
from app.main import create_app
from app.models.room import RoomType
from app.repositories.hotel import HotelRepository
from app.repositories.room_type import RoomTypeRepository
from app.schemas.room_type import RoomTypeCreate, RoomTypeResponse, RoomTypeUpdate
from app.services.room_type import RoomTypeService
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy


def base_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "code": "DLX",
        "name": "Deluxe",
        "max_occupancy": 3,
        "standard_occupancy": 2,
        "bed_count": 1,
        "base_price": "120.00",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


# --- 18. layer separation ---------------------------------------------------------------


def test_router_contains_no_sqlalchemy() -> None:
    source = inspect.getsource(room_types_router)

    for forbidden in ["select(", "session.", "Session", "sqlalchemy"]:
        assert forbidden not in source, f"router references {forbidden!r}"


def test_service_contains_no_sql() -> None:
    source = inspect.getsource(inspect.getmodule(RoomTypeService))  # type: ignore[arg-type]

    for forbidden in ["select(", "text(", "func.count"]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_repository_never_commits() -> None:
    source = inspect.getsource(inspect.getmodule(RoomTypeRepository))  # type: ignore[arg-type]

    assert "commit()" not in source


def test_service_owns_the_transaction() -> None:
    source = inspect.getsource(inspect.getmodule(RoomTypeService))  # type: ignore[arg-type]

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


def test_repository_offers_no_code_only_lookup() -> None:
    """A code is unique only within a hotel. A `get_by_code(code)` would be able to return
    another property's row, so the method must not exist to be called by mistake."""
    methods = {name for name in dir(RoomTypeRepository) if not name.startswith("_")}

    assert "get_by_hotel_and_code" in methods
    assert "get_by_code" not in methods
    assert "list_page" not in methods  # the hotel-scoped variant is the only one


def test_every_repository_lookup_is_hotel_scoped() -> None:
    for method_name in [
        "get_by_hotel_and_code",
        "code_exists",
        "count_for_hotel",
        "list_page_for_hotel",
    ]:
        signature = inspect.signature(getattr(RoomTypeRepository, method_name))
        assert "hotel_id" in signature.parameters, method_name


def test_dependency_assembles_service_over_the_shared_resolver() -> None:
    """Since 3B.3 the parent chain is resolved by one shared seam rather than by each
    service holding its own hotel repository."""

    class _Session:
        pass

    stub: Any = _Session()
    scope = deps.get_scope_resolver(stub, AllowAllPolicy())
    service = deps.get_room_type_service(stub, scope)

    assert isinstance(service, RoomTypeService)
    assert isinstance(service._repository, RoomTypeRepository)
    assert isinstance(service._scope, HotelScopeResolver)
    assert service._session is stub


def test_the_shared_resolver_owns_the_hotel_repository() -> None:
    class _Session:
        pass

    resolver = deps.get_scope_resolver(_Session(), AllowAllPolicy())  # type: ignore[arg-type]

    assert isinstance(resolver._hotels, HotelRepository)
    assert isinstance(resolver._room_types, RoomTypeRepository)


# --- shared SQLSTATE vocabulary (reused, not duplicated) ---------------------------------


def test_sqlstate_vocabulary_is_shared_not_redefined() -> None:
    """The 23001-vs-23503 distinction cost a debugging round in 3B.1; it is defined once."""
    for module in (
        inspect.getmodule(RoomTypeService),
        __import__("app.services.hotel", fromlist=["x"]),
    ):
        source = inspect.getsource(module)  # type: ignore[arg-type]
        assert '"23001"' not in source, "SQLSTATE literal redefined in a service"
        assert '"23505"' not in source


def test_restrict_violation_is_recognised() -> None:
    assert "23001" in SQLSTATE_DEPENDENCY_VIOLATIONS
    assert "23503" in SQLSTATE_DEPENDENCY_VIOLATIONS


def test_sqlstate_of_returns_none_for_a_plain_exception() -> None:
    """Callers must fall back to their generic branch, not crash."""
    assert sqlstate_of(ValueError("nothing to do with the database")) is None


# --- schemas -------------------------------------------------------------------------------


def test_create_schema_requires_the_columns_the_database_requires() -> None:
    with pytest.raises(PydanticValidationError) as excinfo:
        RoomTypeCreate.model_validate({"name": "Only a name"})

    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert {"code", "max_occupancy", "standard_occupancy", "bed_count", "base_price"} <= missing


def test_create_schema_has_no_hotel_field() -> None:
    """The hotel comes from the path. Accepting it in the body too would let the two
    disagree."""
    assert "hotel_id" not in RoomTypeCreate.model_fields
    assert "hotel_public_id" not in RoomTypeCreate.model_fields


def test_create_schema_rejects_standard_above_max() -> None:
    with pytest.raises(PydanticValidationError, match="standard_occupancy"):
        RoomTypeCreate.model_validate(base_payload(max_occupancy=2, standard_occupancy=4))


def test_update_schema_checks_occupancy_only_when_both_are_supplied() -> None:
    """With one value missing the stored row supplies the other, so the service checks it."""
    with pytest.raises(PydanticValidationError):
        RoomTypeUpdate.model_validate({"max_occupancy": 2, "standard_occupancy": 4})

    # One alone is accepted here and validated later.
    assert RoomTypeUpdate.model_validate({"max_occupancy": 2}).max_occupancy == 2


def test_update_schema_has_no_code_field() -> None:
    assert "code" not in RoomTypeUpdate.model_fields


def test_update_schema_forbids_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        RoomTypeUpdate.model_validate({"nmae": "typo"})


def test_response_schema_exposes_no_internal_keys() -> None:
    fields = set(RoomTypeResponse.model_fields)

    assert "id" not in fields
    assert "hotel_id" not in fields
    assert "public_id" not in fields
    assert "hotel_public_id" in fields
    assert "code" in fields


@pytest.mark.parametrize("code", ["has space", "-leading", "_leading", "toolongcodevalue12345678"])
def test_code_pattern_rejects_unusable_codes(code: str) -> None:
    """The code travels in a URL path, so its shape is constrained."""
    with pytest.raises(PydanticValidationError):
        RoomTypeCreate.model_validate(base_payload(code=code))


def test_code_and_currency_are_upper_cased() -> None:
    parsed = RoomTypeCreate.model_validate(base_payload(code="dlx", currency="eur"))

    assert parsed.code == "DLX"
    assert parsed.currency == "EUR"


# --- service behaviour without a database ---------------------------------------------------


class _NoHotels:
    def get_by_public_id(self, _: uuid.UUID) -> None:
        return None


class _NoRoomTypes:
    def get_by_hotel_and_code(self, _hotel_id: int, _code: str) -> None:
        return None


class _StubSession:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def test_missing_hotel_raises_not_found_before_any_room_type_work() -> None:
    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    service = RoomTypeService(_StubSession(), object(), scope)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        service.get(uuid.uuid4(), "DLX")


def test_occupancy_check_uses_the_post_update_state() -> None:
    """A real (unsaved) RoomType, not a stub: constructing one needs no session, and it
    keeps the signature honest instead of silencing mypy."""
    stored = RoomType(max_occupancy=4, standard_occupancy=3)
    service = RoomTypeService(_StubSession(), object(), _NoHotels())  # type: ignore[arg-type]

    # Lowering max alone below the stored standard must be refused.
    with pytest.raises(ConflictError, match="standard_occupancy"):
        service._check_occupancy_after(stored, {"max_occupancy": 2})

    # Lowering both together is fine.
    service._check_occupancy_after(stored, {"max_occupancy": 2, "standard_occupancy": 2})


# --- OpenAPI surface and stage guards ----------------------------------------------------------


def test_room_type_routes_are_nested_under_the_hotel() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    collection = "/api/v1/hotels/{hotel_public_id}/room-types"
    item = "/api/v1/hotels/{hotel_public_id}/room-types/{code}"
    assert set(paths[collection]) == {"post", "get"}
    assert set(paths[item]) == {"get", "patch", "delete"}


def test_no_flat_room_type_route_exists() -> None:
    """The nested path is the only way in; a flat one would lose the hotel scope."""
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert "/api/v1/room-types" not in paths
    assert not any(p.startswith("/api/v1/room-types") for p in paths)


def test_no_room_type_id_appears_in_any_path() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any("room_type_id" in p or "room-type-id" in p for p in paths)
