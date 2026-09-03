"""Booking layering, schemas and the constraint-authority rules.

The structural claims here are the ones that matter most in this domain: that no
availability check was written in application code, that allocations have no sub-resource
routes, and that the status vocabulary and inventory-holding set come from the frozen schema.
Behaviour against live PostgreSQL is in ``tests/integration/test_bookings_api.py``.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import uuid
from typing import Any, get_args

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api import deps
from app.api.v1.endpoints import bookings as bookings_router
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import create_app
from app.models.enums import INVENTORY_HOLDING_STATUSES, BookingSource, BookingStatus
from app.repositories.booking import BookingRepository
from app.schemas.booking import (
    BookingCreate,
    BookingResponse,
    BookingStatusLiteral,
    BookingUpdate,
)
from app.services.booking import (
    OVERLAP_CONSTRAINT,
    SQLSTATE_EXCLUSION_VIOLATION,
    BookingService,
)
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy

CHECK_IN = dt.date(2026, 9, 1)
CHECK_OUT = dt.date(2026, 9, 4)


def nights(*rates: str) -> list[dict[str, object]]:
    return [
        {"stay_date": str(CHECK_IN + dt.timedelta(days=offset)), "rate": rate}
        for offset, rate in enumerate(rates)
    ]


def base_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "guest_public_id": str(uuid.uuid4()),
        "reference": "BK-0001",
        "check_in_date": str(CHECK_IN),
        "check_out_date": str(CHECK_OUT),
        "total_amount": "360.00",
        "currency": "EUR",
        "rooms": [{"room_number": "101", "nights": nights("120.00", "120.00", "120.00")}],
    }
    body.update(overrides)
    return body


# --- layer separation ---------------------------------------------------------------------


def code_of(module: object) -> str:
    """Module source with every docstring removed.

    Layering tests scan for forbidden constructs, and a docstring that *explains* why
    ``commit()`` or ``select()`` is absent would otherwise trip the very check it documents.
    That has now happened three times in this project; stripping docstrings fixes the class
    of bug rather than the instance.
    """
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
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


def test_router_contains_no_sqlalchemy_query_expressions() -> None:
    source = code_of(bookings_router)

    for forbidden in ["select(", "session.", "Session", "sqlalchemy", "commit"]:
        assert forbidden not in source, f"router references {forbidden!r}"


def test_service_contains_no_sqlalchemy_query_expressions() -> None:
    source = code_of(inspect.getmodule(BookingService))

    for forbidden in ["select(", "text(", "func.count", "order_by", "selectinload"]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_repository_never_commits() -> None:
    assert "commit()" not in code_of(inspect.getmodule(BookingRepository))


def test_service_owns_commit_and_rollback() -> None:
    source = code_of(inspect.getmodule(BookingService))

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


def test_repository_uses_the_core_delete_technique() -> None:
    source = code_of(inspect.getmodule(BookingRepository))

    assert "sql_delete" in source
    assert "self._session.delete(" not in source


# --- the exclusion constraint is the sole authority --------------------------------------------


def test_no_application_level_availability_check_exists() -> None:
    """The GiST exclusion constraint decides. A query-then-insert would be racy, and this is
    the single most important correctness property in the schema."""
    service_source = code_of(inspect.getmodule(BookingService))
    repo_source = code_of(inspect.getmodule(BookingRepository))

    for forbidden in ["is_available", "overlaps", "daterange", "check_availability"]:
        assert forbidden not in service_source, f"service references {forbidden!r}"
        assert forbidden not in repo_source, f"repository references {forbidden!r}"


def test_the_overlap_constraint_name_matches_the_frozen_schema() -> None:
    from app.models.booking import BookingRoom

    table = BookingRoom.metadata.tables["booking_rooms"]
    names = {constraint.name for constraint in table.constraints}

    assert OVERLAP_CONSTRAINT in names


def test_the_service_recognises_the_exclusion_sqlstate() -> None:
    """23P01 is raised only by an EXCLUDE constraint."""
    assert SQLSTATE_EXCLUSION_VIOLATION == "23P01"
    source = inspect.getsource(BookingService._translate)
    assert "SQLSTATE_EXCLUSION_VIOLATION" in source


def test_the_service_never_logs_a_driver_exception() -> None:
    """A booking's driver message carries guest, room and date values."""
    assert "exc_info=" not in code_of(inspect.getmodule(BookingService))


# --- status vocabulary comes from the frozen schema ----------------------------------------------


def test_status_literal_matches_the_database_check_constraint_exactly() -> None:
    assert set(get_args(BookingStatusLiteral)) == set(BookingStatus.values())


def test_source_literal_matches_the_database_check_constraint_exactly() -> None:
    from app.schemas.booking import BookingSourceLiteral

    assert set(get_args(BookingSourceLiteral)) == set(BookingSource.values())


@pytest.mark.parametrize("status", ["booked", "active", "CONFIRMED", ""])
def test_invented_statuses_are_rejected(status: str) -> None:
    with pytest.raises(PydanticValidationError):
        BookingCreate.model_validate(base_payload(status=status))


def test_only_confirmed_and_checked_in_hold_inventory() -> None:
    """Restates the approved decision the exclusion constraint encodes."""
    assert INVENTORY_HOLDING_STATUSES == ("confirmed", "checked_in")


def test_status_defaults_to_pending() -> None:
    assert BookingCreate.model_validate(base_payload()).status == "pending"


# --- the deferred trigger shapes the API ---------------------------------------------------------


def test_nights_must_cover_the_stay_exactly() -> None:
    """Mirrors the deferred trigger at the edge so the client gets a field-level message.
    The trigger stays the authority -- an integration test drives it directly."""
    with pytest.raises(PydanticValidationError, match="must price exactly the nights"):
        BookingCreate.model_validate(
            base_payload(rooms=[{"room_number": "101", "nights": nights("120.00", "120.00")}])
        )


def test_extra_nights_outside_the_stay_are_rejected() -> None:
    payload = base_payload()
    payload["rooms"] = [
        {
            "room_number": "101",
            "nights": [
                *nights("120.00", "120.00", "120.00"),
                {"stay_date": str(CHECK_OUT), "rate": "120.00"},
            ],
        }
    ]

    with pytest.raises(PydanticValidationError, match="unexpected"):
        BookingCreate.model_validate(payload)


def test_the_checkout_day_is_not_a_night() -> None:
    """Half-open [check_in, check_out), consistent with the whole schema."""
    parsed = BookingCreate.model_validate(base_payload())

    assert [n.stay_date for n in parsed.rooms[0].nights] == [
        CHECK_IN,
        CHECK_IN + dt.timedelta(days=1),
        CHECK_IN + dt.timedelta(days=2),
    ]


def test_duplicate_stay_dates_in_one_room_are_rejected() -> None:
    payload = base_payload()
    payload["rooms"] = [
        {
            "room_number": "101",
            "nights": [
                {"stay_date": str(CHECK_IN), "rate": "120.00"},
                {"stay_date": str(CHECK_IN), "rate": "130.00"},
                {"stay_date": str(CHECK_IN + dt.timedelta(days=1)), "rate": "120.00"},
            ],
        }
    ]

    with pytest.raises(PydanticValidationError, match="only once"):
        BookingCreate.model_validate(payload)


def test_the_same_room_cannot_be_allocated_twice_to_one_booking() -> None:
    """Mirrors uq_booking_rooms_booking_id_room_id."""
    payload = base_payload()
    payload["rooms"] = [
        {"room_number": "101", "nights": nights("120.00", "120.00", "120.00")},
        {"room_number": "101", "nights": nights("120.00", "120.00", "120.00")},
    ]

    with pytest.raises(PydanticValidationError, match="only once per booking"):
        BookingCreate.model_validate(payload)


def test_at_least_one_room_is_required() -> None:
    with pytest.raises(PydanticValidationError):
        BookingCreate.model_validate(base_payload(rooms=[]))


def test_stay_dates_must_be_ordered() -> None:
    with pytest.raises(PydanticValidationError, match="check_out_date must be after"):
        BookingCreate.model_validate(
            base_payload(check_in_date=str(CHECK_OUT), check_out_date=str(CHECK_IN))
        )


# --- update is deliberately narrow ---------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["check_in_date", "check_out_date", "reference", "guest_public_id", "rooms"]
)
def test_update_cannot_change_identity_dates_or_allocation(field: str) -> None:
    """Re-dating cascades to the night rows without changing their COUNT, which the deferred
    trigger rejects. Re-rooming is atomic for the same reason as creation."""
    assert field not in BookingUpdate.model_fields


def test_update_does_not_accept_cancelled_at() -> None:
    """Derived from status: ck_bookings_cancellation_consistent is a biconditional."""
    assert "cancelled_at" not in BookingUpdate.model_fields


def test_update_forbids_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        BookingUpdate.model_validate({"nmae": "typo"})


def test_cancellation_timestamp_is_derived_from_status() -> None:
    assert BookingService._cancelled_at_for("cancelled") is not None
    assert BookingService._cancelled_at_for("confirmed") is None
    # An existing timestamp is preserved rather than moved on every touch.
    original = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert BookingService._cancelled_at_for("cancelled", existing=original) is original
    assert BookingService._cancelled_at_for("confirmed", existing=original) is None


# --- internal ids never surface ------------------------------------------------------------------


def test_response_exposes_no_internal_keys() -> None:
    fields = set(BookingResponse.model_fields)

    for forbidden in ["id", "hotel_id", "guest_id", "room_id", "booking_id"]:
        assert forbidden not in fields
    assert {"public_id", "hotel_public_id", "guest_public_id"} <= fields


def test_allocations_are_identified_by_room_number_not_id() -> None:
    from app.schemas.booking import BookingRoomResponse

    fields = set(BookingRoomResponse.model_fields)
    assert "room_number" in fields
    for forbidden in ["id", "room_id", "booking_id", "hotel_id"]:
        assert forbidden not in fields


@pytest.mark.parametrize("field", ["id", "hotel_id", "guest_id", "public_id"])
def test_create_rejects_internal_identifiers(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        BookingCreate.model_validate(base_payload(**{field: 1}))


def test_allocation_input_rejects_a_room_id() -> None:
    payload = base_payload()
    payload["rooms"] = [
        {"room_id": 1, "room_number": "101", "nights": nights("120.00", "120.00", "120.00")}
    ]

    with pytest.raises(PydanticValidationError):
        BookingCreate.model_validate(payload)


# --- money and dates -----------------------------------------------------------------------------


def test_money_fields_are_decimal_not_float() -> None:
    import decimal

    parsed = BookingCreate.model_validate(base_payload())

    assert isinstance(parsed.total_amount, decimal.Decimal)
    assert isinstance(parsed.rooms[0].nights[0].rate, decimal.Decimal)


def test_negative_money_is_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        BookingCreate.model_validate(base_payload(total_amount="-1.00"))


def test_business_dates_are_dates_not_timestamps() -> None:
    parsed = BookingCreate.model_validate(base_payload())

    assert isinstance(parsed.check_in_date, dt.date)
    assert not isinstance(parsed.check_in_date, dt.datetime)


# --- wiring --------------------------------------------------------------------------------------


def test_dependency_assembles_the_service_with_its_collaborators() -> None:
    from app.repositories.guest import GuestRepository

    class _Session:
        pass

    stub: Any = _Session()
    scope = deps.get_scope_resolver(stub, AllowAllPolicy())
    service = deps.get_booking_service(stub, scope)

    assert isinstance(service, BookingService)
    assert isinstance(service._repository, BookingRepository)
    # The guest repository is reused rather than duplicated: a booking resolves its guest
    # within the same hotel.
    assert isinstance(service._guests, GuestRepository)
    assert isinstance(service._scope, HotelScopeResolver)
    assert service._session is stub


def test_repository_offers_no_unscoped_lookup() -> None:
    methods = {name for name in dir(BookingRepository) if not name.startswith("_")}

    assert "get_by_hotel_and_public_id" in methods
    for forbidden in ["get_by_public_id", "get_by_id", "get_by_reference", "list_page"]:
        assert forbidden not in methods, f"unscoped lookup {forbidden!r} exists"


def test_every_repository_read_takes_a_hotel_id() -> None:
    for method_name in [
        "get_by_hotel_and_public_id",
        "count_for_hotel",
        "list_page_for_hotel",
        "get_room_in_hotel",
    ]:
        parameters = inspect.signature(getattr(BookingRepository, method_name)).parameters
        assert "hotel_id" in parameters, method_name


class _NoHotels:
    def get_by_public_id(self, _: uuid.UUID) -> None:
        return None


class _NoRoomTypes:
    def get_by_hotel_and_code(self, _hotel_id: int, _code: str) -> None:
        return None


class _StubSession:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def test_missing_hotel_is_reported_before_any_booking_work() -> None:
    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    service = BookingService(_StubSession(), object(), object(), scope)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        service.get(uuid.uuid4(), uuid.uuid4())


# --- OpenAPI surface -----------------------------------------------------------------------------


def test_booking_routes_are_nested_under_the_hotel() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    collection = "/api/v1/hotels/{hotel_public_id}/bookings"
    assert set(paths[collection]) == {"post", "get"}
    assert set(paths[collection + "/{booking_public_id}"]) == {"get", "patch", "delete"}


def test_no_flat_booking_route_exists() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any(p.startswith("/api/v1/bookings") for p in paths)
