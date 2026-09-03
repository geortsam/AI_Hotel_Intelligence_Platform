"""Payment layering, schemas and the append-only guarantee.

The append-only claim is asserted structurally here -- the service and repository must have
no update or delete method at all, not merely no route -- and behaviourally against live
PostgreSQL in ``tests/integration/test_payments_api.py``.
"""

from __future__ import annotations

import ast
import datetime as dt
import decimal
import inspect
import uuid
from typing import Any, get_args

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import UniqueConstraint

from app.api import deps
from app.api.v1.endpoints import payments as payments_router
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import create_app
from app.models.enums import PaymentKind, PaymentMethod, PaymentStatus
from app.models.payment import Payment
from app.repositories.payment import PaymentRepository
from app.schemas.payment import (
    ChargeCreate,
    PaymentKindLiteral,
    PaymentMethodLiteral,
    PaymentResponse,
    PaymentStatusLiteral,
    RefundCreate,
)
from app.services.payment import IDEMPOTENCY_CONSTRAINT, PaymentService
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy


def code_of(module: object) -> str:
    """Module source with docstrings removed, so prose explaining an absent construct cannot
    trip a scan for that construct."""
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


def charge_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"amount": "100.00", "currency": "EUR", "method": "card"}
    body.update(overrides)
    return body


# --- layer separation -----------------------------------------------------------------------


def test_router_contains_no_sqlalchemy_query_expressions() -> None:
    source = code_of(payments_router)

    for forbidden in ["select(", "session.", "Session", "sqlalchemy", "commit"]:
        assert forbidden not in source, f"router references {forbidden!r}"


def test_service_contains_no_sqlalchemy_query_expressions() -> None:
    source = code_of(inspect.getmodule(PaymentService))

    for forbidden in ["select(", "text(", "func.count", "order_by"]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_repository_never_commits() -> None:
    assert "commit()" not in code_of(inspect.getmodule(PaymentRepository))


def test_service_owns_commit_and_rollback() -> None:
    source = code_of(inspect.getmodule(PaymentService))

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


# --- append-only, enforced at every layer ------------------------------------------------------


def test_the_repository_has_no_update_or_delete_method() -> None:
    """Not merely absent from the router: absent from the layer that could perform it."""
    methods = {name for name in dir(PaymentRepository) if not name.startswith("_")}

    for forbidden in ["update", "apply_changes", "delete", "remove"]:
        assert forbidden not in methods, f"repository exposes {forbidden!r}"


def test_the_service_has_no_update_or_delete_method() -> None:
    methods = {name for name in dir(PaymentService) if not name.startswith("_")}

    assert methods == {"get", "list", "create_charge", "create_refund"}


def test_no_delete_statement_exists_in_the_payment_layers() -> None:
    for module in (inspect.getmodule(PaymentRepository), inspect.getmodule(PaymentService)):
        source = code_of(module)
        assert "sql_delete" not in source
        assert ".delete(" not in source


def test_no_update_schema_exists() -> None:
    """A PaymentUpdate would be the first step towards editing financial history."""
    import app.schemas.payment as payment_schemas

    assert not hasattr(payment_schemas, "PaymentUpdate")


# --- scoping --------------------------------------------------------------------------------------


def test_repository_offers_no_unscoped_lookup() -> None:
    """public_id is globally unique, so an unscoped lookup would reach another booking's
    financial records. It must not exist -- especially here, because it is also what
    resolves a refund's parent."""
    methods = {name for name in dir(PaymentRepository) if not name.startswith("_")}

    assert "get_in_booking" in methods
    for forbidden in ["get_by_public_id", "get_by_id", "list_page", "count"]:
        assert forbidden not in methods, f"unscoped lookup {forbidden!r} exists"


def test_every_repository_query_takes_a_booking_id() -> None:
    for method_name in ["get_in_booking", "count_for_booking", "list_page_for_booking"]:
        parameters = inspect.signature(getattr(PaymentRepository, method_name)).parameters
        assert "booking_id" in parameters, method_name


def test_the_refund_parent_is_resolved_through_the_scoped_lookup() -> None:
    """refunded_payment_id is a SINGLE-column FK with no hotel or booking in it, so the
    database alone would allow a cross-hotel reference. The service closes that."""
    source = code_of(PaymentService)

    assert "get_in_booking(booking.id, payload.refunds_public_id)" in source


def test_dependency_assembles_the_service_with_its_collaborators() -> None:
    from app.repositories.booking import BookingRepository

    class _Session:
        pass

    stub: Any = _Session()
    scope = deps.get_scope_resolver(stub, AllowAllPolicy())
    service = deps.get_payment_service(stub, scope)

    assert isinstance(service, PaymentService)
    assert isinstance(service._repository, PaymentRepository)
    # The booking repository is reused rather than duplicated.
    assert isinstance(service._bookings, BookingRepository)
    assert isinstance(service._scope, HotelScopeResolver)
    assert service._session is stub


# --- vocabularies come from the frozen schema -----------------------------------------------------


def test_kind_literal_matches_the_check_constraint() -> None:
    assert set(get_args(PaymentKindLiteral)) == set(PaymentKind.values())


def test_method_literal_matches_the_check_constraint() -> None:
    assert set(get_args(PaymentMethodLiteral)) == set(PaymentMethod.values())


def test_status_literal_matches_the_check_constraint() -> None:
    assert set(get_args(PaymentStatusLiteral)) == set(PaymentStatus.values())


@pytest.mark.parametrize("method", ["paypal", "crypto", "CARD", ""])
def test_invented_methods_are_rejected(method: str) -> None:
    with pytest.raises(PydanticValidationError):
        ChargeCreate.model_validate(charge_payload(method=method))


# --- the biconditional CHECK constraints are mirrored ---------------------------------------------


def test_a_charge_payload_cannot_name_a_parent() -> None:
    """ck_payments_refund_references_charge is a biconditional. Separate schemas make the
    invalid combination unrepresentable rather than merely rejected."""
    assert "refunds_public_id" not in ChargeCreate.model_fields
    with pytest.raises(PydanticValidationError):
        ChargeCreate.model_validate(charge_payload(refunds_public_id=str(uuid.uuid4())))


def test_a_refund_payload_requires_a_parent() -> None:
    with pytest.raises(PydanticValidationError) as excinfo:
        RefundCreate.model_validate(charge_payload())

    assert "refunds_public_id" in {e["loc"][0] for e in excinfo.value.errors()}


def test_neither_payload_accepts_a_kind() -> None:
    """The endpoint decides kind; letting a client set it would create a way to violate the
    biconditional."""
    for schema in (ChargeCreate, RefundCreate):
        assert "kind" not in schema.model_fields


def test_captured_requires_a_settlement_time() -> None:
    """Mirrors ck_payments_captured_has_paid_at."""
    with pytest.raises(PydanticValidationError, match="paid_at is required"):
        ChargeCreate.model_validate(charge_payload(status="captured"))

    accepted = ChargeCreate.model_validate(
        charge_payload(status="captured", paid_at="2026-09-01T12:00:00Z")
    )
    assert accepted.paid_at is not None


def test_amount_must_be_positive_for_both_kinds() -> None:
    """ck_payments_amount_positive: direction lives in `kind`, never in the sign."""
    for schema in (ChargeCreate, RefundCreate):
        with pytest.raises(PydanticValidationError):
            schema.model_validate(
                charge_payload(amount="-1.00", refunds_public_id=str(uuid.uuid4()))
            )
        with pytest.raises(PydanticValidationError):
            schema.model_validate(charge_payload(amount="0", refunds_public_id=str(uuid.uuid4())))


@pytest.mark.parametrize("value", ["12345", "abc", "12a4", ""])
def test_card_last_four_format_is_enforced(value: str) -> None:
    """Mirrors ck_payments_card_last_four_format."""
    with pytest.raises(PydanticValidationError):
        ChargeCreate.model_validate(charge_payload(card_last_four=value))


def test_no_full_card_data_can_be_submitted() -> None:
    """The table stores a four-digit fragment and nothing else; the schema must not accept
    more, whatever a caller sends."""
    for field in ["card_number", "pan", "cvv", "cvc", "card_expiry", "cardholder_name"]:
        assert field not in ChargeCreate.model_fields
        with pytest.raises(PydanticValidationError):
            ChargeCreate.model_validate(charge_payload(**{field: "4111111111111111"}))


# --- identifiers ----------------------------------------------------------------------------------


def test_response_exposes_public_ids_and_no_internal_keys() -> None:
    fields = set(PaymentResponse.model_fields)

    for forbidden in ["id", "booking_id", "hotel_id", "refunded_payment_id"]:
        assert forbidden not in fields
    assert {"public_id", "booking_public_id", "hotel_public_id", "refunds_public_id"} <= fields


@pytest.mark.parametrize("field", ["id", "public_id", "booking_id", "hotel_id"])
def test_create_rejects_internal_identifiers(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        ChargeCreate.model_validate(charge_payload(**{field: 1}))


def test_the_model_carries_public_id_from_migration_0002() -> None:
    table = Payment.metadata.tables["payments"]

    assert "public_id" in table.columns
    assert not table.columns["public_id"].nullable
    unique = {
        tuple(c.name for c in con.columns)
        for con in table.constraints
        if isinstance(con, UniqueConstraint)
    }
    assert ("public_id",) in unique


def test_the_pre_existing_partial_index_is_untouched_by_0002() -> None:
    table = Payment.metadata.tables["payments"]
    index = next(i for i in table.indexes if i.name == IDEMPOTENCY_CONSTRAINT)

    assert index.unique
    assert [c.name for c in index.columns] == ["provider", "transaction_reference"]
    assert index.dialect_options["postgresql"]["where"] is not None


# --- error hygiene --------------------------------------------------------------------------------


def test_the_service_never_logs_a_driver_exception() -> None:
    """A payment's driver message carries the amount, the processor reference and the card
    fragment."""
    assert "exc_info=" not in code_of(inspect.getmodule(PaymentService))


def test_the_idempotency_conflict_does_not_echo_the_reference() -> None:
    source = inspect.getsource(PaymentService._translate)

    assert "already been recorded" in source
    assert "{payload" not in source
    assert "{constraint}" not in source


# --- service behaviour without a database ---------------------------------------------------------


class _NoHotels:
    def get_by_public_id(self, _: uuid.UUID) -> None:
        return None


class _NoRoomTypes:
    def get_by_hotel_and_code(self, _hotel_id: int, _code: str) -> None:
        return None


class _StubSession:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def test_missing_hotel_is_reported_before_any_payment_work() -> None:
    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    service = PaymentService(_StubSession(), object(), object(), scope)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        service.get(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())


def test_money_is_decimal_not_float() -> None:
    parsed = ChargeCreate.model_validate(charge_payload(amount="100.05"))

    assert isinstance(parsed.amount, decimal.Decimal)
    assert parsed.amount == decimal.Decimal("100.05")


def test_paid_at_is_a_timestamp_not_a_date() -> None:
    parsed = ChargeCreate.model_validate(
        charge_payload(status="captured", paid_at="2026-09-01T12:00:00Z")
    )

    assert isinstance(parsed.paid_at, dt.datetime)


# --- OpenAPI surface ------------------------------------------------------------------------------


def test_payment_routes_are_nested_under_the_booking() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]
    base = "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/payments"

    assert set(paths[base]) == {"post", "get"}
    assert set(paths[base + "/refunds"]) == {"post"}
    assert set(paths[base + "/{payment_public_id}"]) == {"get"}


def test_no_flat_payment_route_exists() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any(p.startswith("/api/v1/payments") for p in paths)
