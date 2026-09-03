"""Guest layering, schemas and PII handling.

Needs no database. The PII claims are asserted structurally here -- that no service logs a
driver exception, that the conflict message quotes no address -- and behaviourally against
live PostgreSQL in ``tests/integration/test_guests_api.py``.
"""

from __future__ import annotations

import datetime as dt
import inspect
import uuid
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api import deps
from app.api.v1.endpoints import guests as guests_router
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import create_app
from app.models.guest import Guest
from app.repositories.guest import GuestRepository
from app.schemas.guest import GuestCreate, GuestResponse, GuestUpdate
from app.services.guest import EMAIL_UNIQUE_CONSTRAINT, GuestService
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy


def base_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"first_name": "Ada", "last_name": "Lovelace"}
    body.update(overrides)
    return body


# --- 26-28. layer separation --------------------------------------------------------------


def test_router_contains_no_sqlalchemy_query_expressions() -> None:
    source = inspect.getsource(guests_router)

    for forbidden in ["select(", "session.", "Session", "sqlalchemy", "commit"]:
        assert forbidden not in source, f"router references {forbidden!r}"


def test_service_contains_no_sqlalchemy_query_expressions() -> None:
    source = inspect.getsource(inspect.getmodule(GuestService))  # type: ignore[arg-type]

    for forbidden in ["select(", "text(", "func.count", "order_by"]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_repository_never_commits() -> None:
    source = inspect.getsource(inspect.getmodule(GuestRepository))  # type: ignore[arg-type]

    assert "commit()" not in source


def test_service_owns_commit_and_rollback() -> None:
    source = inspect.getsource(inspect.getmodule(GuestService))  # type: ignore[arg-type]

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


def test_repository_uses_the_core_delete_technique() -> None:
    """Both FK policies referencing guests -- RESTRICT from bookings, SET NULL from
    reviews -- must be PostgreSQL's decision, not the ORM's."""
    source = inspect.getsource(inspect.getmodule(GuestRepository))  # type: ignore[arg-type]

    assert "sql_delete" in source
    assert "self._session.delete(" not in source


# --- 29. every repository query is hotel-scoped ----------------------------------------------


def test_repository_offers_no_unscoped_lookup() -> None:
    """public_id is globally unique, so an unscoped lookup would "work" -- and would be able
    to reach another property's guest. The method must not exist to be called by mistake."""
    methods = {name for name in dir(GuestRepository) if not name.startswith("_")}

    assert "get_by_hotel_and_public_id" in methods
    for forbidden in ["get_by_public_id", "get_by_id", "get_by_email", "list_page", "count"]:
        assert forbidden not in methods, f"unscoped lookup {forbidden!r} exists"


def test_every_repository_query_takes_a_hotel_id() -> None:
    for method_name in [
        "get_by_hotel_and_public_id",
        "count_for_hotel",
        "list_page_for_hotel",
    ]:
        parameters = inspect.signature(getattr(GuestRepository, method_name)).parameters
        assert "hotel_id" in parameters, method_name


def test_service_resolves_the_hotel_through_the_shared_resolver() -> None:
    source = inspect.getsource(inspect.getmodule(GuestService))  # type: ignore[arg-type]

    assert "self._scope.require_hotel(" in source
    # The lookup itself belongs to the resolver, not copied into this service.
    assert "get_by_public_id(hotel_public_id)" not in source


def test_dependency_assembles_the_service_over_the_shared_resolver() -> None:
    class _Session:
        pass

    stub: Any = _Session()
    scope = deps.get_scope_resolver(stub, AllowAllPolicy())
    service = deps.get_guest_service(stub, scope)

    assert isinstance(service, GuestService)
    assert isinstance(service._repository, GuestRepository)
    assert isinstance(service._scope, HotelScopeResolver)
    assert service._session is stub


# --- 7. no application-level uniqueness check ---------------------------------------------------


def test_create_does_not_pre_check_the_email() -> None:
    """The partial unique index is the sole authority. A SELECT-then-INSERT would be racy
    and redundant."""
    source = inspect.getsource(GuestService.create)

    assert "email_exists" not in source
    assert "exists(" not in source


def test_the_email_constraint_name_matches_the_frozen_schema() -> None:
    """The service reports a precise conflict by constraint NAME rather than by parsing the
    driver message, which would contain the address."""
    index_names = {index.name for index in Guest.metadata.tables["guests"].indexes}

    assert EMAIL_UNIQUE_CONSTRAINT in index_names


def test_the_email_uniqueness_is_partial_and_per_hotel() -> None:
    """Guests with no email are unconstrained; two at one hotel cannot share an address."""
    guests_table = Guest.metadata.tables["guests"]
    index = next(i for i in guests_table.indexes if i.name == EMAIL_UNIQUE_CONSTRAINT)

    assert index.unique
    assert [column.name for column in index.columns] == ["hotel_id", "email"]
    assert index.dialect_options["postgresql"]["where"] is not None


# --- PII: nothing sensitive is logged or echoed ---------------------------------------------------


def test_the_service_never_logs_a_driver_exception() -> None:
    """PostgreSQL renders a unique violation as
    ``DETAIL: Key (hotel_id, email)=(1, someone@example.com) already exists.``
    Logging it would write a guest's address into application logs."""
    source = inspect.getsource(inspect.getmodule(GuestService))  # type: ignore[arg-type]

    # The KEYWORD, not the word: the module docstring legitimately explains why exc_info is
    # avoided, and matching prose would fail on the explanation itself.
    assert "exc_info=" not in source


def test_the_conflict_message_does_not_interpolate_a_value() -> None:
    """Unlike the hotel and room-type services, which quote the offending slug or code, this
    one must not quote the email."""
    source = inspect.getsource(GuestService._translate)

    assert "email address." in source
    # No f-string interpolation of user data into the client-facing message.
    assert "{payload" not in source
    assert "{guest" not in source


def test_the_not_found_message_names_no_guest() -> None:
    """A 404 must not confirm that a person exists at another property."""
    source = inspect.getsource(GuestService._require_guest)

    assert '"Guest not found for this hotel."' in source


# --- 10, 30. forbidden and internal fields -------------------------------------------------------


@pytest.mark.parametrize("field", ["id", "hotel_id", "public_id", "created_at", "updated_at"])
def test_create_rejects_internal_and_server_assigned_fields(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        GuestCreate.model_validate(base_payload(**{field: 1}))


@pytest.mark.parametrize("field", ["id", "hotel_id", "public_id"])
def test_update_rejects_internal_fields(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        GuestUpdate.model_validate({field: 1})


def test_update_forbids_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        GuestUpdate.model_validate({"nmae": "typo"})


def test_response_exposes_no_internal_keys() -> None:
    fields = set(GuestResponse.model_fields)

    for forbidden in ["id", "hotel_id"]:
        assert forbidden not in fields
    assert {"public_id", "hotel_public_id"} <= fields


def test_response_carries_only_columns_the_table_actually_has() -> None:
    """Nothing is fabricated: no address, no passport, no document fields."""
    table_columns = set(Guest.metadata.tables["guests"].columns.keys())
    response_fields = set(GuestResponse.model_fields) - {"hotel_public_id"}

    assert response_fields <= table_columns


def test_no_identification_fields_were_invented() -> None:
    fields = set(GuestCreate.model_fields) | set(GuestResponse.model_fields)
    forbidden = {
        "passport_number",
        "national_id",
        "id_document",
        "id_number",
        "ssn",
        "address",
        "address_line1",
    }

    assert fields.isdisjoint(forbidden)


# --- 8-9. schema validation ---------------------------------------------------------------------


def test_create_requires_both_names() -> None:
    with pytest.raises(PydanticValidationError) as excinfo:
        GuestCreate.model_validate({})

    assert {"first_name", "last_name"} <= {e["loc"][0] for e in excinfo.value.errors()}


def test_everything_except_the_names_is_optional() -> None:
    guest = GuestCreate.model_validate(base_payload())

    assert guest.email is None
    assert guest.phone is None
    assert guest.date_of_birth is None
    assert guest.marketing_opt_in is False  # consent defaults to no


def test_country_code_is_upper_cased_to_satisfy_the_check_constraint() -> None:
    assert GuestCreate.model_validate(base_payload(country_code="gb")).country_code == "GB"


def test_preferred_language_is_lower_cased() -> None:
    parsed = GuestCreate.model_validate(base_payload(preferred_language="EN"))

    assert parsed.preferred_language == "en"


@pytest.mark.parametrize("code", ["GBR", "G", "12", ""])
def test_invalid_country_codes_are_rejected_at_the_edge(code: str) -> None:
    with pytest.raises(PydanticValidationError):
        GuestCreate.model_validate(base_payload(country_code=code))


def test_date_of_birth_accepts_an_iso_date() -> None:
    parsed = GuestCreate.model_validate(base_payload(date_of_birth="1815-12-10"))

    assert parsed.date_of_birth == dt.date(1815, 12, 10)


def test_update_distinguishes_omitted_from_explicit_null() -> None:
    omitted = GuestUpdate.model_validate({"first_name": "Augusta"})
    explicit = GuestUpdate.model_validate({"first_name": "Augusta", "phone": None})

    assert omitted.model_dump(exclude_unset=True) == {"first_name": "Augusta"}
    assert explicit.model_dump(exclude_unset=True) == {
        "first_name": "Augusta",
        "phone": None,
    }


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


def test_missing_hotel_is_reported_before_any_guest_work() -> None:
    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    service = GuestService(_StubSession(), object(), scope)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        service.get(uuid.uuid4(), uuid.uuid4())


# --- OpenAPI surface -----------------------------------------------------------------------------


def test_guest_routes_are_nested_under_the_hotel() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    collection = "/api/v1/hotels/{hotel_public_id}/guests"
    assert set(paths[collection]) == {"post", "get"}
    assert set(paths[collection + "/{guest_public_id}"]) == {"get", "patch", "delete"}


def test_no_flat_guest_route_exists() -> None:
    """A portfolio-wide guest collection would contradict the hotel-scoped identity."""
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any(p.startswith("/api/v1/guests") for p in paths)
