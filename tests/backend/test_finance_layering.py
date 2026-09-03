"""Financial ledger layering, schemas, and the four separate resources.

The two things this file exists to pin down: that the ledger has no identifier and therefore
no way to be edited, and that revenue, expenses and their two category vocabularies stay four
distinct resources rather than collapsing into one generic transaction.
"""

from __future__ import annotations

import ast
import datetime as dt
import decimal
import inspect
from typing import Any, get_args

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api import deps
from app.api.v1.endpoints import expense_categories as expense_categories_router
from app.api.v1.endpoints import expenses as expenses_router
from app.api.v1.endpoints import revenue as revenue_router
from app.api.v1.endpoints import revenue_categories as revenue_categories_router
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import create_app
from app.models.enums import RecurrenceInterval
from app.models.finance import Expense, ExpenseCategory, Revenue, RevenueCategory
from app.repositories.finance import (
    ExpenseCategoryRepository,
    ExpenseRepository,
    RevenueCategoryRepository,
    RevenueRepository,
)
from app.schemas.finance import (
    ExpenseCategoryCreate,
    ExpenseCategoryResponse,
    ExpenseCategoryUpdate,
    ExpenseCreate,
    ExpenseResponse,
    RecurrenceIntervalLiteral,
    RevenueCategoryCreate,
    RevenueCategoryResponse,
    RevenueCategoryUpdate,
    RevenueCreate,
    RevenueResponse,
)
from app.services.finance import (
    ExpenseCategoryService,
    ExpenseService,
    RevenueCategoryService,
    RevenueService,
)
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy

#: The ledger's own two collections. Matched EXACTLY rather than by path suffix: from
#: Stage 3B.11 the intelligence domain also has a path ending "/revenue", and a suffix
#: test would silently start asserting things about a forecast instead.
LEDGER_COLLECTIONS = {
    "/api/v1/hotels/{hotel_public_id}/revenue",
    "/api/v1/hotels/{hotel_public_id}/expenses",
}

LEDGER_REPOSITORIES = (RevenueRepository, ExpenseRepository)
LEDGER_SERVICES = (RevenueService, ExpenseService)
CATEGORY_REPOSITORIES = (RevenueCategoryRepository, ExpenseCategoryRepository)
CATEGORY_SERVICES = (RevenueCategoryService, ExpenseCategoryService)
ROUTERS = (
    revenue_router,
    expenses_router,
    revenue_categories_router,
    expense_categories_router,
)


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


def revenue_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "category_code": "FB",
        "revenue_date": "2026-09-02",
        "amount": "120.00",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


def expense_payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "category_code": "UTILITIES",
        "expense_date": "2026-09-02",
        "amount": "80.00",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


# --- layer separation ------------------------------------------------------------------------


@pytest.mark.parametrize("module", ROUTERS)
def test_routers_contain_no_sqlalchemy_query_expressions(module: object) -> None:
    source = code_of(module)

    for forbidden in ["select(", "session.", "Session", "sqlalchemy", "commit"]:
        assert forbidden not in source, f"router references {forbidden!r}"


def test_service_module_contains_no_sqlalchemy_query_expressions() -> None:
    source = code_of(inspect.getmodule(RevenueService))

    for forbidden in ["select(", "text(", "func.count", "order_by", ".where("]:
        assert forbidden not in source, f"service module references {forbidden!r}"


def test_repository_module_never_commits() -> None:
    assert "commit()" not in code_of(inspect.getmodule(RevenueRepository))


def test_repository_module_holds_no_domain_rules() -> None:
    source = code_of(inspect.getmodule(RevenueRepository))

    for forbidden in ["raise ", "ConflictError", "NotFoundError", "sqlstate"]:
        assert forbidden not in source, f"repository module contains {forbidden!r}"


@pytest.mark.parametrize("service", [*LEDGER_SERVICES, *CATEGORY_SERVICES])
def test_every_service_owns_its_commit(service: type) -> None:
    source = "".join(
        inspect.getsource(getattr(service, name))
        for name in dir(service)
        if not name.startswith("_") and callable(getattr(service, name))
    )

    assert "self._session.commit()" in source


def test_the_service_module_owns_rollback() -> None:
    assert "self._session.rollback()" in code_of(inspect.getmodule(RevenueService))


def test_no_service_defines_its_own_sqlstate_constants() -> None:
    """The vocabulary is shared in app.core.errors and must not be re-declared per domain."""
    source = code_of(inspect.getmodule(RevenueService))

    assert '"23505"' not in source
    assert '"23514"' not in source
    assert '"23503"' not in source
    assert "SQLSTATE_UNIQUE_VIOLATION" in source  # imported, not defined


# --- four resources, not one -------------------------------------------------------------------


def test_revenue_and_expenses_are_distinct_schemas() -> None:
    """A single 'financial transaction' schema could only be their union with everything
    optional, which would describe neither table."""
    assert RevenueCreate is not ExpenseCreate
    assert set(RevenueCreate.model_fields) != set(ExpenseCreate.model_fields)


def test_only_revenue_carries_a_booking_link() -> None:
    """expenses has no booking_id column at all: a cost is incurred by the property."""
    assert "booking_public_id" in RevenueCreate.model_fields
    assert "booking_public_id" not in ExpenseCreate.model_fields
    assert "booking_id" in Revenue.metadata.tables["revenue"].columns
    assert "booking_id" not in Expense.metadata.tables["expenses"].columns


def test_only_expenses_carry_recurrence_and_vendor() -> None:
    for field in ("is_recurring", "recurrence_interval", "vendor", "invoice_reference"):
        assert field in ExpenseCreate.model_fields
        assert field not in RevenueCreate.model_fields


def test_the_two_category_flags_are_different_columns() -> None:
    assert "is_room_revenue" in RevenueCategoryCreate.model_fields
    assert "is_fixed_cost" in ExpenseCategoryCreate.model_fields
    assert "is_fixed_cost" not in RevenueCategoryCreate.model_fields
    assert "is_room_revenue" not in ExpenseCategoryCreate.model_fields


def test_the_two_category_tables_have_independent_unique_constraints() -> None:
    """The same code may exist in both; they are separate vocabularies, not one."""
    for model, name in (
        (RevenueCategory, "uq_revenue_categories_code"),
        (ExpenseCategory, "uq_expense_categories_code"),
    ):
        table = model.metadata.tables[model.__tablename__]
        names = {c.name for c in table.constraints}
        assert name in names


# --- the identifier decision ----------------------------------------------------------------------


@pytest.mark.parametrize("table", ["revenue", "expenses"])
def test_the_ledger_tables_really_have_no_public_id(table: str) -> None:
    """The premise of every decision in this domain."""
    assert "public_id" not in Revenue.metadata.tables[table].columns


@pytest.mark.parametrize("table", ["revenue", "expenses"])
def test_the_ledger_tables_have_no_unique_constraint_beyond_the_primary_key(table: str) -> None:
    """Two byte-identical lines coexist, so nothing can name one row."""
    meta = Revenue.metadata.tables[table]
    unique_constraints = {
        c.name for c in meta.constraints if c.__class__.__name__ == "UniqueConstraint"
    }
    unique_indexes = {i.name for i in meta.indexes if i.unique}

    assert unique_constraints == set()
    assert unique_indexes == set()


@pytest.mark.parametrize("cls", [*LEDGER_REPOSITORIES, *LEDGER_SERVICES])
def test_no_ledger_layer_offers_a_single_row_lookup(cls: type) -> None:
    methods = {name for name in dir(cls) if not name.startswith("_")}

    for forbidden in ["get", "get_by_id", "get_by_public_id", "get_one"]:
        assert forbidden not in methods, f"{cls.__name__} exposes {forbidden!r}"


@pytest.mark.parametrize("cls", [*LEDGER_REPOSITORIES, *LEDGER_SERVICES])
def test_no_ledger_layer_can_update_or_delete(cls: type) -> None:
    """Append-only: a correction is a compensating line, which is why `amount` has no
    positivity CHECK while `tax_amount` does."""
    methods = {name for name in dir(cls) if not name.startswith("_")}

    for forbidden in ["update", "apply_changes", "delete", "remove"]:
        assert forbidden not in methods, f"{cls.__name__} exposes {forbidden!r}"


def test_the_ledger_service_surfaces_are_exactly_post_and_list() -> None:
    for service in LEDGER_SERVICES:
        methods = {name for name in dir(service) if not name.startswith("_")}
        assert methods == {"list", "create"}, service.__name__


def test_no_update_schema_exists_for_a_ledger_line() -> None:
    import app.schemas.finance as finance_schemas

    assert not hasattr(finance_schemas, "RevenueUpdate")
    assert not hasattr(finance_schemas, "ExpenseUpdate")


def test_no_delete_statement_exists_for_a_ledger_row() -> None:
    """The two Core deletes in the repository module belong to the CATEGORY repositories."""
    source = code_of(inspect.getmodule(RevenueRepository))

    assert "sql_delete(Revenue)" not in source
    assert "sql_delete(Expense)" not in source
    assert "sql_delete(RevenueCategory)" in source
    assert "sql_delete(ExpenseCategory)" in source


# --- categories are addressable, and mutable ------------------------------------------------------


@pytest.mark.parametrize("service", CATEGORY_SERVICES)
def test_the_category_service_surface_is_full_crud(service: type) -> None:
    """Different lifecycle from a ledger line: a category has a natural key."""
    methods = {name for name in dir(service) if not name.startswith("_")}

    assert methods == {"get", "list", "create", "update", "delete"}


@pytest.mark.parametrize("schema", [RevenueCategoryUpdate, ExpenseCategoryUpdate])
def test_a_categorys_code_is_not_editable(schema: type) -> None:
    """It is the URL identity; renaming it would break every link pointing at it."""
    assert "code" not in schema.model_fields  # type: ignore[attr-defined]
    with pytest.raises(PydanticValidationError):
        schema.model_validate({"code": "NEW"})  # type: ignore[attr-defined]


@pytest.mark.parametrize("schema", [RevenueCategoryUpdate, ExpenseCategoryUpdate])
def test_category_updates_are_partial(schema: type) -> None:
    parsed = schema.model_validate({"is_active": False})  # type: ignore[attr-defined]

    assert parsed.model_dump(exclude_unset=True) == {"is_active": False}


@pytest.mark.parametrize("repository", CATEGORY_REPOSITORIES)
def test_category_deletion_uses_a_core_statement(repository: type) -> None:
    """session.delete() would let the ORM nullify first and pre-empt the RESTRICT."""
    source = inspect.getsource(repository.delete)  # type: ignore[attr-defined]

    assert "sql_delete" in source
    assert "self._session.delete(" not in source
    assert "expunge" in source


# --- categories are global ------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["revenue_categories", "expense_categories"])
def test_the_category_tables_have_no_hotel_column(table: str) -> None:
    """One vocabulary for the whole installation, exactly like amenities."""
    assert "hotel_id" not in RevenueCategory.metadata.tables[table].columns


@pytest.mark.parametrize(
    "schema", [RevenueCategoryCreate, ExpenseCategoryCreate, RevenueCategoryResponse]
)
def test_no_category_payload_mentions_a_hotel(schema: type) -> None:
    assert not any(
        "hotel" in field
        for field in schema.model_fields  # type: ignore[attr-defined]
    )


@pytest.mark.parametrize("service", CATEGORY_SERVICES)
def test_the_category_services_take_no_scope_resolver(service: type) -> None:
    """A hotel scope on a global table would be meaningless, so it is not even available."""
    parameters = inspect.signature(service).parameters

    assert "scope" not in parameters
    assert set(parameters) == {"session", "repository"}


# --- ledger scoping -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repository", "methods"),
    [
        (RevenueRepository, ["count_for_hotel", "list_page_for_hotel"]),
        (ExpenseRepository, ["count_for_hotel", "list_page_for_hotel"]),
    ],
)
def test_every_ledger_query_takes_a_hotel_id(repository: type, methods: list[str]) -> None:
    for name in methods:
        parameters = inspect.signature(getattr(repository, name)).parameters
        assert "hotel_id" in parameters, f"{repository.__name__}.{name}"


@pytest.mark.parametrize("repository", LEDGER_REPOSITORIES)
def test_count_and_page_share_one_filter_helper(repository: type) -> None:
    """A filter applied to the page but not the count gives a total that contradicts the
    rows."""
    source = code_of(repository)

    assert source.count("self._filtered(") == 2
    assert source.count("_filtered") == 3  # two calls plus the definition


def test_the_booking_is_resolved_through_the_hotel() -> None:
    """The composite FK would refuse a cross-hotel booking anyway; resolving hotel-first
    turns that 409 into an honest 404."""
    source = code_of(RevenueService)

    assert "get_by_hotel_and_public_id(hotel.id" in source


# --- payments and the ledger are unrelated --------------------------------------------------------


def test_the_ledger_never_touches_payments() -> None:
    """The schema declares no foreign key in either direction (verified live)."""
    for module in (inspect.getmodule(RevenueService), inspect.getmodule(RevenueRepository)):
        source = code_of(module)
        assert "Payment" not in source
        assert "payment" not in source.lower().replace("payments are not", "")


def test_the_ledger_never_reads_the_bookings_total() -> None:
    source = code_of(inspect.getmodule(RevenueService))

    assert "total_amount" not in source


def test_the_ledger_invents_no_pricing() -> None:
    """room_types.base_price is a room type's list price, not a ledger input."""
    for module in (inspect.getmodule(RevenueService), inspect.getmodule(RevenueRepository)):
        source = code_of(module)
        assert "base_price" not in source
        assert "RoomType" not in source


def test_no_rate_or_rate_plan_exists_on_either_ledger_table() -> None:
    """Those columns live on booking_room_nights. Nothing here reproduces them."""
    for table in ("revenue", "expenses"):
        columns = set(Revenue.metadata.tables[table].columns.keys())
        assert "rate" not in columns
        assert "rate_plan_code" not in columns

    for schema in (RevenueCreate, ExpenseCreate, RevenueResponse, ExpenseResponse):
        assert "rate" not in schema.model_fields
        assert "rate_plan_code" not in schema.model_fields


# --- amounts: the sign asymmetry is the correction mechanism --------------------------------------


@pytest.mark.parametrize("schema", [RevenueCreate, ExpenseCreate])
@pytest.mark.parametrize("amount", ["-50.00", "0", "0.00", "120.00", "-0.01"])
def test_amount_accepts_any_sign(schema: type, amount: str) -> None:
    """Neither table constrains `amount`. A negative line is how a ledger is corrected."""
    payload = revenue_payload if schema is RevenueCreate else expense_payload
    parsed = schema.model_validate(payload(amount=amount))  # type: ignore[attr-defined]

    assert parsed.amount == decimal.Decimal(amount)


@pytest.mark.parametrize("schema", [RevenueCreate, ExpenseCreate])
def test_tax_amount_must_be_non_negative(schema: type) -> None:
    """ck_revenue_tax_amount_non_negative / ck_expenses_tax_amount_non_negative -- the
    asymmetry with `amount` is the point."""
    payload = revenue_payload if schema is RevenueCreate else expense_payload

    with pytest.raises(PydanticValidationError):
        schema.model_validate(payload(tax_amount="-0.01"))  # type: ignore[attr-defined]
    assert (
        schema.model_validate(payload(tax_amount="0")).tax_amount == 0  # type: ignore[attr-defined]
    )


@pytest.mark.parametrize("schema", [RevenueCreate, ExpenseCreate])
def test_tax_amount_defaults_to_zero(schema: type) -> None:
    payload = revenue_payload if schema is RevenueCreate else expense_payload

    assert schema.model_validate(payload()).tax_amount == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize("schema", [RevenueCreate, ExpenseCreate])
def test_amount_is_decimal_not_float(schema: type) -> None:
    payload = revenue_payload if schema is RevenueCreate else expense_payload
    parsed = schema.model_validate(payload(amount="120.05"))  # type: ignore[attr-defined]

    assert isinstance(parsed.amount, decimal.Decimal)
    assert parsed.amount == decimal.Decimal("120.05")


@pytest.mark.parametrize("schema", [RevenueCreate, ExpenseCreate])
def test_three_decimal_places_are_rejected_not_rounded(schema: type) -> None:
    """NUMERIC(14,2) would silently round; quietly altering money is worse than refusing."""
    payload = revenue_payload if schema is RevenueCreate else expense_payload

    with pytest.raises(PydanticValidationError):
        schema.model_validate(payload(amount="120.055"))  # type: ignore[attr-defined]


# --- currency IS present on both tables -----------------------------------------------------------


@pytest.mark.parametrize("table", ["revenue", "expenses"])
def test_currency_exists_and_is_not_null(table: str) -> None:
    """Contrary to a common recollection of the Stage 2 findings, both ledger tables carry
    currency. It is booking_room_nights that omits it."""
    column = Revenue.metadata.tables[table].columns["currency"]

    assert not column.nullable
    assert column.server_default is None


@pytest.mark.parametrize("schema", [RevenueCreate, ExpenseCreate])
def test_currency_is_required(schema: type) -> None:
    """NOT NULL with no default, and not defaulted to the hotel's currency: no constraint
    relates the two."""
    payload = revenue_payload if schema is RevenueCreate else expense_payload
    body = payload()
    del body["currency"]

    with pytest.raises(PydanticValidationError):
        schema.model_validate(body)  # type: ignore[attr-defined]


@pytest.mark.parametrize("schema", [RevenueCreate, ExpenseCreate])
@pytest.mark.parametrize("currency", ["EU", "EURO", "E1R", "", "12345"])
def test_malformed_currency_is_rejected(schema: type, currency: str) -> None:
    payload = revenue_payload if schema is RevenueCreate else expense_payload

    with pytest.raises(PydanticValidationError):
        schema.model_validate(payload(currency=currency))  # type: ignore[attr-defined]


@pytest.mark.parametrize("schema", [RevenueCreate, ExpenseCreate])
def test_lowercase_currency_is_upcased(schema: type) -> None:
    """The CHECK is case-sensitive, so `eur` would otherwise be a 409 rather than a value."""
    payload = revenue_payload if schema is RevenueCreate else expense_payload

    assert schema.model_validate(payload(currency="eur")).currency == "EUR"  # type: ignore[attr-defined]


# --- expense recurrence is a biconditional --------------------------------------------------------


def test_recurrence_literal_matches_the_check_constraint() -> None:
    assert set(get_args(RecurrenceIntervalLiteral)) == set(RecurrenceInterval.values())


@pytest.mark.parametrize(
    ("is_recurring", "interval"), [(False, None), (True, "monthly"), (True, "annual")]
)
def test_consistent_recurrence_is_accepted(is_recurring: bool, interval: str | None) -> None:
    parsed = ExpenseCreate.model_validate(
        expense_payload(is_recurring=is_recurring, recurrence_interval=interval)
    )

    assert parsed.is_recurring is is_recurring


@pytest.mark.parametrize(
    ("is_recurring", "interval"), [(True, None), (False, "monthly"), (False, "annual")]
)
def test_inconsistent_recurrence_is_rejected(is_recurring: bool, interval: str | None) -> None:
    """Mirrors ck_expenses_recurrence_consistent, caught at the edge so the message names
    the fields rather than an invisible constraint."""
    with pytest.raises(PydanticValidationError, match="is_recurring"):
        ExpenseCreate.model_validate(
            expense_payload(is_recurring=is_recurring, recurrence_interval=interval)
        )


@pytest.mark.parametrize("interval", ["weekly", "daily", "MONTHLY", ""])
def test_undeclared_recurrence_intervals_are_rejected(interval: str) -> None:
    with pytest.raises(PydanticValidationError):
        ExpenseCreate.model_validate(
            expense_payload(is_recurring=True, recurrence_interval=interval)
        )


# --- what a client may not send -------------------------------------------------------------------


@pytest.mark.parametrize("schema", [RevenueCreate, ExpenseCreate])
@pytest.mark.parametrize("field", ["id", "hotel_id", "category_id", "booking_id", "public_id"])
def test_ledger_creates_reject_internal_identifiers(schema: type, field: str) -> None:
    payload = revenue_payload if schema is RevenueCreate else expense_payload

    with pytest.raises(PydanticValidationError):
        schema.model_validate(payload(**{field: 1}))  # type: ignore[attr-defined]


def test_expenses_cannot_be_attached_to_a_booking() -> None:
    """There is no such column; accepting the field would promise something impossible."""
    with pytest.raises(PydanticValidationError):
        ExpenseCreate.model_validate(
            expense_payload(booking_public_id="11111111-1111-1111-1111-111111111111")
        )


def test_revenue_booking_is_optional() -> None:
    """revenue.booking_id is nullable, and that nullability is load-bearing: a non-resident
    eating in the restaurant belongs to no stay."""
    assert Revenue.metadata.tables["revenue"].columns["booking_id"].nullable
    assert RevenueCreate.model_validate(revenue_payload()).booking_public_id is None


# --- responses ------------------------------------------------------------------------------------


@pytest.mark.parametrize("schema", [RevenueResponse, ExpenseResponse])
def test_ledger_responses_expose_no_internal_keys(schema: type) -> None:
    fields = set(schema.model_fields)  # type: ignore[attr-defined]

    for forbidden in ["id", "hotel_id", "category_id", "booking_id"]:
        assert forbidden not in fields
    assert "hotel_public_id" in fields
    assert "category_code" in fields


@pytest.mark.parametrize("schema", [RevenueCategoryResponse, ExpenseCategoryResponse])
def test_category_responses_carry_no_timestamps(schema: type) -> None:
    """Neither category table uses the timestamp mixin, so none are invented."""
    fields = set(schema.model_fields)  # type: ignore[attr-defined]

    assert "created_at" not in fields
    assert "updated_at" not in fields
    assert "id" not in fields


@pytest.mark.parametrize("schema", [RevenueResponse, ExpenseResponse])
def test_ledger_responses_do_carry_timestamps(schema: type) -> None:
    """Both ledger tables DO use the mixin, and both have the update trigger."""
    fields = set(schema.model_fields)  # type: ignore[attr-defined]

    assert {"created_at", "updated_at"} <= fields


def test_a_ledger_response_carries_no_identifier_of_its_own() -> None:
    """Not an oversight: the table provides none. Asserted so a future edit that quietly
    exposes `id` fails here."""
    for schema in (RevenueResponse, ExpenseResponse):
        assert not any(
            field.endswith("public_id")
            and not field.startswith("hotel")
            and not field.startswith("booking")
            for field in schema.model_fields
        )


# --- error and financial-data hygiene -------------------------------------------------------------


def test_the_services_never_log_a_driver_exception() -> None:
    """The driver message quotes the row: amounts, vendor names, invoice references."""
    assert "exc_info=" not in code_of(inspect.getmodule(RevenueService))


def test_only_the_sqlstate_and_constraint_name_are_logged() -> None:
    source = code_of(inspect.getmodule(RevenueService))

    assert "sqlstate=%s, constraint=%s" in source
    for leaked in ["entry.amount", "payload.amount", "vendor", "invoice_reference", "str(exc)"]:
        assert f"logger.warning({leaked}" not in source


def test_conflict_messages_interpolate_nothing() -> None:
    for service in (*LEDGER_SERVICES, *CATEGORY_SERVICES):
        source = inspect.getsource(service._translate)
        assert "{payload" not in source
        assert "{constraint}" not in source
        assert "str(exc)" not in source


# --- service behaviour without a database ---------------------------------------------------------


class _NoHotels:
    def get_by_public_id(self, _: object) -> None:
        return None


class _NoRoomTypes:
    def get_by_hotel_and_code(self, _hotel_id: int, _code: str) -> None:
        return None


class _NoCategories:
    def get_by_code(self, _: str) -> None:
        return None


class _StubSession:
    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def test_missing_hotel_is_reported_before_any_ledger_work() -> None:
    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    service = RevenueService(_StubSession(), object(), _NoCategories(), object(), scope)  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        service.list(uuid_zero(), page=1, page_size=20)


def test_missing_category_is_reported_by_name() -> None:
    service = RevenueCategoryService(_StubSession(), _NoCategories())  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Revenue category not found\."):
        service.get("NOPE")


def test_missing_expense_category_is_reported_by_its_own_name() -> None:
    service = ExpenseCategoryService(_StubSession(), _NoCategories())  # type: ignore[arg-type]

    with pytest.raises(NotFoundError, match=r"Expense category not found\."):
        service.get("NOPE")


def uuid_zero() -> Any:
    import uuid

    return uuid.UUID(int=0)


def test_dependencies_assemble_the_four_services() -> None:
    from app.repositories.booking import BookingRepository

    class _Session:
        pass

    stub: Any = _Session()
    scope = deps.get_scope_resolver(stub, AllowAllPolicy())

    revenue = deps.get_revenue_service(stub, scope)
    expenses = deps.get_expense_service(stub, scope)
    revenue_categories = deps.get_revenue_category_service(stub)
    expense_categories = deps.get_expense_category_service(stub)

    assert isinstance(revenue._repository, RevenueRepository)
    assert isinstance(revenue._categories, RevenueCategoryRepository)
    # The booking repository is reused rather than duplicated.
    assert isinstance(revenue._bookings, BookingRepository)
    assert isinstance(expenses._repository, ExpenseRepository)
    assert isinstance(expenses._categories, ExpenseCategoryRepository)
    assert isinstance(revenue_categories._repository, RevenueCategoryRepository)
    assert isinstance(expense_categories._repository, ExpenseCategoryRepository)


def test_the_expense_service_has_no_booking_repository() -> None:
    """It would have nothing to do: expenses carry no booking column."""
    parameters = inspect.signature(ExpenseService.__init__).parameters

    assert "bookings" not in parameters


def test_dates_are_dates_not_timestamps() -> None:
    assert RevenueCreate.model_validate(revenue_payload()).revenue_date == dt.date(2026, 9, 2)
    assert ExpenseCreate.model_validate(expense_payload()).expense_date == dt.date(2026, 9, 2)


# --- OpenAPI surface ------------------------------------------------------------------------------


def test_the_url_hierarchy_matches_the_scoping_of_each_table() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    # Categories: global tables, so global collections.
    assert set(paths["/api/v1/revenue-categories"]) == {"get", "post"}
    assert set(paths["/api/v1/revenue-categories/{code}"]) == {"get", "patch", "delete"}
    assert set(paths["/api/v1/expense-categories"]) == {"get", "post"}
    assert set(paths["/api/v1/expense-categories/{code}"]) == {"get", "patch", "delete"}
    # Ledger: hotel-scoped, append-only, no single-entry URL.
    assert set(paths["/api/v1/hotels/{hotel_public_id}/revenue"]) == {"get", "post"}
    assert set(paths["/api/v1/hotels/{hotel_public_id}/expenses"]) == {"get", "post"}


def test_no_single_ledger_entry_url_exists() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    for path in paths:
        assert not path.endswith("/revenue/{revenue_id}")
        assert not path.endswith("/expenses/{expense_id}")
        assert "{revenue_public_id}" not in path
        assert "{expense_public_id}" not in path


def test_the_ledger_exposes_no_mutating_verbs_beyond_post() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    for path in LEDGER_COLLECTIONS:
        assert set(paths[path]) == {"get", "post"}, path


def test_no_generic_financial_transaction_route_exists() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    for banned in ("transactions", "ledger", "finance", "financial"):
        assert not any(banned in path for path in paths), banned


def test_the_categories_are_not_nested_under_a_hotel() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert not any("/hotels/" in p and "categories" in p for p in paths)


def test_the_ledger_itself_exposes_no_aggregation_route() -> None:
    """Stage 3B.9's ledger endpoints return rows, never totals. Aggregation arrived in
    3B.10 as a SEPARATE read-only analytics domain, under its own path segment -- so the
    ledger collections themselves must still expose nothing of the kind."""
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])
    assert paths >= LEDGER_COLLECTIONS
    for banned in ("reports", "summary", "totals", "dashboard"):
        assert not any(banned in path for path in paths), banned
