"""Hotel domain layering, schemas and transaction boundaries.

Needs no database: these assert the *structure* of the domain -- that the router holds no
queries, that the service owns the unit of work, that the schemas encode the contract. The
behavioural proof against real PostgreSQL is in ``tests/integration/test_hotels_api.py``.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api import deps
from app.api.v1.endpoints import hotels as hotels_router
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.main import create_app
from app.repositories.hotel import HotelRepository
from app.schemas.common import Page
from app.schemas.hotel import HotelCreate, HotelResponse, HotelUpdate
from app.services.hotel import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, HotelService
from tests.backend.authorization_stubs import AllowAllPolicy

# --- 15. layer separation -----------------------------------------------------------------


def test_router_module_imports_no_sqlalchemy_query_constructs() -> None:
    """A query in the router would bypass both the service and the repository."""
    source = inspect.getsource(hotels_router)

    for forbidden in ["select(", "session.", "Session", "sqlalchemy"]:
        assert forbidden not in source, f"router references {forbidden!r}"


def test_service_module_contains_no_sql() -> None:
    """The service coordinates; it does not query."""
    source = inspect.getsource(inspect.getmodule(HotelService))  # type: ignore[arg-type]

    for forbidden in ["select(", "text(", "func.count"]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_repository_never_commits() -> None:
    """Committing in the repository would make composing two writes into one unit of work
    impossible."""
    source = inspect.getsource(inspect.getmodule(HotelRepository))  # type: ignore[arg-type]

    assert "commit()" not in source


def test_service_owns_the_transaction() -> None:
    source = inspect.getsource(inspect.getmodule(HotelService))  # type: ignore[arg-type]

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


def test_get_db_still_does_not_commit() -> None:
    """The Stage 3A decision must survive the arrival of a write-heavy domain.

    Asserts on the CALL, not the word: the docstring explains the decision and legitimately
    contains "commit".
    """
    source = inspect.getsource(deps.get_db)

    assert "commit()" not in source
    assert "rollback()" in source


def test_dependency_assembles_service_over_the_request_session() -> None:
    class _Session:
        pass

    stub: Any = _Session()
    service = deps.get_hotel_service(stub, AllowAllPolicy())

    assert isinstance(service, HotelService)
    assert isinstance(service._repository, HotelRepository)
    # The service holds the same session it will commit.
    assert service._session is stub


# --- schemas -------------------------------------------------------------------------------


def test_create_schema_requires_the_columns_the_database_requires() -> None:
    with pytest.raises(PydanticValidationError) as excinfo:
        HotelCreate.model_validate({"name": "Only a name"})

    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert {"slug", "address_line1", "city", "country_code", "currency"} <= missing


def test_create_schema_defaults_timezone_to_utc() -> None:
    hotel = HotelCreate.model_validate(
        {
            "slug": "x-hotel",
            "name": "X",
            "address_line1": "1 Road",
            "city": "Athens",
            "country_code": "GR",
            "currency": "EUR",
        }
    )

    assert hotel.timezone == "UTC"


@pytest.mark.parametrize("slug", ["Has Spaces", "UPPER", "trailing-", "-leading", "dou--ble"])
def test_slug_pattern_rejects_non_slugs(slug: str) -> None:
    with pytest.raises(PydanticValidationError):
        HotelCreate.model_validate(
            {
                "slug": slug,
                "name": "X",
                "address_line1": "1 Road",
                "city": "Athens",
                "country_code": "GR",
                "currency": "EUR",
            }
        )


def test_update_schema_distinguishes_omitted_from_explicit_null() -> None:
    """This is the whole basis of PATCH semantics."""
    omitted = HotelUpdate.model_validate({"name": "New"})
    explicit = HotelUpdate.model_validate({"name": "New", "region": None})

    assert omitted.model_dump(exclude_unset=True) == {"name": "New"}
    assert explicit.model_dump(exclude_unset=True) == {"name": "New", "region": None}


def test_update_schema_forbids_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        HotelUpdate.model_validate({"nmae": "typo"})


def test_update_schema_has_no_slug_field() -> None:
    assert "slug" not in HotelUpdate.model_fields


def test_response_schema_excludes_the_internal_id() -> None:
    assert "id" not in HotelResponse.model_fields
    assert "public_id" in HotelResponse.model_fields


# --- pagination envelope ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "page_size", "expected_pages"),
    [(0, 20, 0), (1, 20, 1), (20, 20, 1), (21, 20, 2), (5, 2, 3)],
)
def test_page_count_is_derived_correctly(total: int, page_size: int, expected_pages: int) -> None:
    page: Page[str] = Page.build(items=[], total=total, page=1, page_size=page_size)

    assert page.pages == expected_pages


def test_page_size_limits_are_sane() -> None:
    assert 0 < DEFAULT_PAGE_SIZE <= MAX_PAGE_SIZE
    assert MAX_PAGE_SIZE == 100


# --- error translation, without a database ------------------------------------------------------


class _StubRepository:
    """Repository double: returns nothing, records nothing, touches no database."""

    def __init__(self) -> None:
        self.deleted = False

    def get_by_public_id(self, _: uuid.UUID) -> None:
        return None

    def slug_exists(self, _: str) -> bool:
        return True


class _StubSession:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class _StubMemberships:
    """The hotel service takes a membership repository from Stage 4.2. These tests exercise
    slug and not-found behaviour, which never reaches it."""

    def add(self, membership: object) -> object:
        return membership

    def count_for_user(self, user_id: int) -> int:
        return 0

    def hotels_page_for_user(self, user_id: int, *, limit: int, offset: int) -> list:
        return []


def test_get_missing_hotel_raises_not_found() -> None:
    service = HotelService(
        _StubSession(),  # type: ignore[arg-type]
        _StubRepository(),  # type: ignore[arg-type]
        _StubMemberships(),  # type: ignore[arg-type]
        AllowAllPolicy(),
    )

    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        service.get(uuid.uuid4())


def test_duplicate_slug_raises_conflict_before_touching_the_database() -> None:
    """The pre-check turns the common case into a clear 409 without an insert attempt."""
    service = HotelService(
        _StubSession(),  # type: ignore[arg-type]
        _StubRepository(),  # type: ignore[arg-type]
        _StubMemberships(),  # type: ignore[arg-type]
        AllowAllPolicy(),
    )

    with pytest.raises(ConflictError, match="already exists"):
        service.create(
            HotelCreate.model_validate(
                {
                    "slug": "taken",
                    "name": "X",
                    "address_line1": "1 Road",
                    "city": "Athens",
                    "country_code": "GR",
                    "currency": "EUR",
                }
            )
        )


# --- OpenAPI surface -----------------------------------------------------------------------------


def test_all_five_hotel_endpoints_are_registered() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    assert set(paths["/api/v1/hotels"]) == {"post", "get"}
    assert set(paths["/api/v1/hotels/{public_id}"]) == {"get", "patch", "delete"}


def test_create_documents_201_and_delete_documents_204() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    assert "201" in paths["/api/v1/hotels"]["post"]["responses"]
    assert "204" in paths["/api/v1/hotels/{public_id}"]["delete"]["responses"]


def test_error_responses_are_documented_with_the_shared_envelope() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]
    get_one = paths["/api/v1/hotels/{public_id}"]["get"]["responses"]

    assert "404" in get_one
    schema = get_one["404"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("ErrorResponse")


def test_the_hotel_resource_exposes_exactly_its_five_endpoints() -> None:
    """Scoped to the hotel resource itself.

    The portfolio-wide guard (is any unbuilt domain present?) lives in
    ``test_app_factory.py`` and is matched by path SEGMENT -- a substring check would flag
    the approved ``room-types`` for containing "room".
    """
    paths = create_app(Settings(environment="test")).openapi()["paths"]
    hotel_own = {
        path for path in paths if path == "/api/v1/hotels" or path == "/api/v1/hotels/{public_id}"
    }

    assert hotel_own == {"/api/v1/hotels", "/api/v1/hotels/{public_id}"}
    assert set(paths["/api/v1/hotels"]) == {"post", "get"}
    assert set(paths["/api/v1/hotels/{public_id}"]) == {"get", "patch", "delete"}
