"""Financial ledger business logic and unit-of-work boundaries.

**Payments are not the ledger, and this module does not pretend otherwise.** The frozen
schema declares no foreign key in either direction between ``payments`` and ``revenue`` or
``expenses`` -- verified live, zero constraints between them. Posting revenue therefore does
not create a payment, recording a payment does not create revenue, and nothing here reads or
reconciles ``bookings.total_amount``. A payment is money moving; a revenue line is money
earned; the schema keeps them apart and so does this.

**Lifecycle, decided from the schema rather than by convention:**

* *Categories* are mutable and deletable. They have no timestamps and no update trigger, but
  they do carry ``is_active``, and both ledger tables reference them ``ON DELETE RESTRICT``
  -- so a category in use cannot be removed, and retiring one means deactivating it.
* *Ledger lines are append-only*, for a structural reason rather than a policy one: neither
  table has a ``public_id`` and neither has any unique constraint beyond its primary key, so
  no client can name a row to edit or delete it. The schema's own correction mechanism is a
  compensating line -- ``amount`` carries no positivity CHECK on either table while
  ``tax_amount`` does, and that asymmetry is deliberate.

  The ``set_updated_at`` trigger on both tables is **not** evidence against this. It is on
  all twelve tables that have an ``updated_at`` column, including ``payments``, which is
  append-only. It is a blanket convention, checked rather than assumed.

**What this module deliberately does NOT do**, because the frozen schema does not say it:

* It does not require ``revenue.currency`` to match the hotel's currency. No constraint
  relates them, and a property may record a supplier invoice in another currency.
* It does not prevent revenue being posted against an ``is_room_revenue`` category. That flag
  exists so a reporting job can *exclude* such rows, not to forbid them.
* It does not relate ``revenue_date`` to the linked booking's stay dates.
* It invents no pricing. ``room_types.base_price`` is never read here.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    SQLSTATE_NOT_NULL_VIOLATION,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    NotFoundError,
    sqlstate_of,
)
from app.models.finance import Expense, ExpenseCategory, Revenue, RevenueCategory
from app.models.hotel import Hotel
from app.repositories.booking import BookingRepository
from app.repositories.finance import (
    ExpenseCategoryRepository,
    ExpenseRepository,
    RevenueCategoryRepository,
    RevenueRepository,
)
from app.schemas.common import Page
from app.schemas.finance import (
    ExpenseCategoryCreate,
    ExpenseCategoryResponse,
    ExpenseCategoryUpdate,
    ExpenseCreate,
    ExpenseResponse,
    RevenueCategoryCreate,
    RevenueCategoryResponse,
    RevenueCategoryUpdate,
    RevenueCreate,
    RevenueResponse,
)
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: Declared at module scope on purpose: inside these classes, ``list`` is a method name.
type RevenueRows = list[Revenue]
type ExpenseRows = list[Expense]
type RevenueResponses = list[RevenueResponse]
type ExpenseResponses = list[ExpenseResponse]


def _constraint_name(exc: IntegrityError) -> str | None:
    """The violated constraint's NAME from driver diagnostics, never its message.

    The message quotes the offending row, which for this domain means amounts, vendor names
    and invoice references.
    """
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None


def _log(kind: str, exc: IntegrityError) -> tuple[str | None, str | None]:
    """Record only the SQLSTATE and the constraint name, never ``exc_info``."""
    state = sqlstate_of(exc)
    constraint = _constraint_name(exc)
    logger.warning("%s integrity error (sqlstate=%s, constraint=%s)", kind, state, constraint)
    return state, constraint


# --- categories ------------------------------------------------------------------------------


class RevenueCategoryService:
    """The global revenue-stream vocabulary. Mutable; deletable only while unreferenced."""

    def __init__(self, session: Session, repository: RevenueCategoryRepository) -> None:
        self._session = session
        self._repository = repository

    def get(self, code: str) -> RevenueCategoryResponse:
        return RevenueCategoryResponse.model_validate(self._require(code))

    def list(
        self, *, page: int, page_size: int, is_active: bool | None = None
    ) -> Page[RevenueCategoryResponse]:
        total = self._repository.count(is_active=is_active)
        rows = self._repository.list_page(
            limit=page_size, offset=(page - 1) * page_size, is_active=is_active
        )
        return Page.build(
            items=[RevenueCategoryResponse.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def create(self, payload: RevenueCategoryCreate) -> RevenueCategoryResponse:
        """Create and commit. ``uq_revenue_categories_code`` decides duplication, not a
        prior lookup -- a check-then-insert would only add a race."""
        try:
            created = self._repository.add(RevenueCategory(**payload.model_dump()))
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        return RevenueCategoryResponse.model_validate(created)

    def update(self, code: str, payload: RevenueCategoryUpdate) -> RevenueCategoryResponse:
        """Apply a partial update and commit. ``code`` is not reachable from here."""
        category = self._require(code)
        changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
        if not changes:
            return RevenueCategoryResponse.model_validate(category)
        try:
            updated = self._repository.apply_changes(category, changes)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        return RevenueCategoryResponse.model_validate(updated)

    def delete(self, code: str) -> None:
        """Delete and commit, honouring the database's RESTRICT.

        A category with revenue lines cannot be removed; the schema says so and no cascade is
        invented. Deactivating it is the way to retire a stream without losing its history.
        """
        category = self._require(code)
        try:
            self._repository.delete(category)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            state = sqlstate_of(exc)
            _log("Revenue category", exc)
            if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
                raise ConflictError(
                    "This revenue category cannot be deleted because revenue entries still "
                    "reference it. Deactivate it instead."
                ) from exc
            raise self._translate(exc) from exc

    def _require(self, code: str) -> RevenueCategory:
        category = self._repository.get_by_code(code)
        if category is None:
            raise NotFoundError("Revenue category not found.")
        return category

    def _translate(self, exc: IntegrityError) -> Exception:
        state, _ = _log("Revenue category", exc)
        if state == SQLSTATE_UNIQUE_VIOLATION:
            return ConflictError("A revenue category with this code already exists.")
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a revenue category constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("This revenue category is still referenced by other records.")
        return ConflictError("The request conflicts with the current state of the database.")


class ExpenseCategoryService:
    """The global cost vocabulary. Same lifecycle as revenue categories, separate table and
    separate code space -- the same code may exist in both (verified live)."""

    def __init__(self, session: Session, repository: ExpenseCategoryRepository) -> None:
        self._session = session
        self._repository = repository

    def get(self, code: str) -> ExpenseCategoryResponse:
        return ExpenseCategoryResponse.model_validate(self._require(code))

    def list(
        self, *, page: int, page_size: int, is_active: bool | None = None
    ) -> Page[ExpenseCategoryResponse]:
        total = self._repository.count(is_active=is_active)
        rows = self._repository.list_page(
            limit=page_size, offset=(page - 1) * page_size, is_active=is_active
        )
        return Page.build(
            items=[ExpenseCategoryResponse.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def create(self, payload: ExpenseCategoryCreate) -> ExpenseCategoryResponse:
        try:
            created = self._repository.add(ExpenseCategory(**payload.model_dump()))
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        return ExpenseCategoryResponse.model_validate(created)

    def update(self, code: str, payload: ExpenseCategoryUpdate) -> ExpenseCategoryResponse:
        category = self._require(code)
        changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
        if not changes:
            return ExpenseCategoryResponse.model_validate(category)
        try:
            updated = self._repository.apply_changes(category, changes)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        return ExpenseCategoryResponse.model_validate(updated)

    def delete(self, code: str) -> None:
        """Delete and commit, honouring ``expenses.category_id``'s RESTRICT."""
        category = self._require(code)
        try:
            self._repository.delete(category)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            state = sqlstate_of(exc)
            _log("Expense category", exc)
            if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
                raise ConflictError(
                    "This expense category cannot be deleted because expenses still "
                    "reference it. Deactivate it instead."
                ) from exc
            raise self._translate(exc) from exc

    def _require(self, code: str) -> ExpenseCategory:
        category = self._repository.get_by_code(code)
        if category is None:
            raise NotFoundError("Expense category not found.")
        return category

    def _translate(self, exc: IntegrityError) -> Exception:
        state, _ = _log("Expense category", exc)
        if state == SQLSTATE_UNIQUE_VIOLATION:
            return ConflictError("An expense category with this code already exists.")
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate an expense category constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("This expense category is still referenced by other records.")
        return ConflictError("The request conflicts with the current state of the database.")


# --- ledger ------------------------------------------------------------------------------------


class RevenueService:
    """Revenue lines at one hotel. Append-only: post and read, never edit or remove."""

    def __init__(
        self,
        session: Session,
        repository: RevenueRepository,
        categories: RevenueCategoryRepository,
        bookings: BookingRepository,
        scope: HotelScopeResolver,
    ) -> None:
        self._session = session
        self._repository = repository
        self._categories = categories
        self._bookings = bookings
        self._scope = scope

    def list(
        self,
        hotel_public_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
        category_code: str | None = None,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
        booking_public_id: uuid.UUID | None = None,
    ) -> Page[RevenueResponse]:
        """One page of a hotel's revenue, newest first.

        An unknown category or booking in the filter is a 404 rather than an empty page: the
        caller asked about something that does not exist, which is different from a real
        filter that matched nothing.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        category_id = self._filter_category_id(category_code)
        booking_id = None
        if booking_public_id is not None:
            booking_id = self._require_booking_id(hotel, booking_public_id)

        total = self._repository.count_for_hotel(
            hotel.id,
            category_id=category_id,
            date_from=date_from,
            date_to=date_to,
            booking_id=booking_id,
        )
        rows = self._repository.list_page_for_hotel(
            hotel.id,
            limit=page_size,
            offset=(page - 1) * page_size,
            category_id=category_id,
            date_from=date_from,
            date_to=date_to,
            booking_id=booking_id,
        )
        return Page.build(
            items=self._to_responses(rows, hotel), total=total, page=page, page_size=page_size
        )

    def create(self, hotel_public_id: uuid.UUID, payload: RevenueCreate) -> RevenueResponse:
        """Post one revenue line and commit.

        The booking, when given, is resolved **through the hotel** before it is used, so a
        line can never reference another property's stay. The composite foreign key would
        refuse it anyway; resolving first turns a 409 into an honest 404.

        No payment is created, and ``bookings.total_amount`` is neither read nor adjusted.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        category = self._require_category(payload.category_code)

        booking_id = None
        if payload.booking_public_id is not None:
            booking_id = self._require_booking_id(hotel, payload.booking_public_id)

        fields = payload.model_dump(exclude={"category_code", "booking_public_id"})
        entry = Revenue(hotel_id=hotel.id, category_id=category.id, booking_id=booking_id, **fields)
        try:
            created = self._repository.add(entry)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._render(created, hotel, category.code, payload.booking_public_id)

    # --- internals ----------------------------------------------------------------------

    def _require_category(self, code: str) -> RevenueCategory:
        category = self._categories.get_by_code(code)
        if category is None:
            raise NotFoundError("Revenue category not found.")
        return category

    def _filter_category_id(self, code: str | None) -> int | None:
        return None if code is None else self._require_category(code).id

    def _require_booking_id(self, hotel: Hotel, booking_public_id: uuid.UUID) -> int:
        booking = self._bookings.get_by_hotel_and_public_id(hotel.id, booking_public_id)
        if booking is None:
            raise NotFoundError("Booking not found for this hotel.")
        return booking.id

    def _to_responses(self, rows: RevenueRows, hotel: Hotel) -> RevenueResponses:
        """Render a page, resolving categories and bookings in two batched queries."""
        codes = self._repository.category_codes_for([row.category_id for row in rows])
        bookings = self._repository.booking_public_ids_for(
            [row.booking_id for row in rows if row.booking_id is not None]
        )
        return [
            self._render(
                row,
                hotel,
                codes.get(row.category_id, ""),
                bookings.get(row.booking_id) if row.booking_id is not None else None,
            )
            for row in rows
        ]

    @staticmethod
    def _render(
        entry: Revenue, hotel: Hotel, category_code: str, booking_public_id: uuid.UUID | None
    ) -> RevenueResponse:
        """The one place a Revenue row becomes a response. No internal key is copied across,
        and the row carries no identifier of its own because the table provides none."""
        return RevenueResponse.model_validate(
            {
                "hotel_public_id": hotel.public_id,
                "category_code": category_code,
                "booking_public_id": booking_public_id,
                "revenue_date": entry.revenue_date,
                "amount": entry.amount,
                "tax_amount": entry.tax_amount,
                "currency": entry.currency,
                "description": entry.description,
                "reference": entry.reference,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
            }
        )

    def _translate(self, exc: IntegrityError) -> Exception:
        state, _ = _log("Revenue", exc)
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a revenue constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("The revenue entry refers to a record that does not exist here.")
        if state == SQLSTATE_UNIQUE_VIOLATION:
            return ConflictError("That value is already taken by another revenue entry.")
        return ConflictError("The request conflicts with the current state of the database.")


class ExpenseService:
    """Expense lines at one hotel. Append-only, and with no booking link -- ``expenses`` has
    no ``booking_id`` column at all. A cost is incurred by the property, not by a stay."""

    def __init__(
        self,
        session: Session,
        repository: ExpenseRepository,
        categories: ExpenseCategoryRepository,
        scope: HotelScopeResolver,
    ) -> None:
        self._session = session
        self._repository = repository
        self._categories = categories
        self._scope = scope

    def list(
        self,
        hotel_public_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
        category_code: str | None = None,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
    ) -> Page[ExpenseResponse]:
        """One page of a hotel's expenses, newest first."""
        hotel = self._scope.require_hotel(hotel_public_id)
        category_id = None if category_code is None else self._require_category(category_code).id

        total = self._repository.count_for_hotel(
            hotel.id, category_id=category_id, date_from=date_from, date_to=date_to
        )
        rows = self._repository.list_page_for_hotel(
            hotel.id,
            limit=page_size,
            offset=(page - 1) * page_size,
            category_id=category_id,
            date_from=date_from,
            date_to=date_to,
        )
        return Page.build(
            items=self._to_responses(rows, hotel), total=total, page=page, page_size=page_size
        )

    def create(self, hotel_public_id: uuid.UUID, payload: ExpenseCreate) -> ExpenseResponse:
        """Post one expense line and commit. No payment is created and no booking is touched."""
        hotel = self._scope.require_hotel(hotel_public_id)
        category = self._require_category(payload.category_code)

        fields = payload.model_dump(exclude={"category_code"})
        entry = Expense(hotel_id=hotel.id, category_id=category.id, **fields)
        try:
            created = self._repository.add(entry)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._render(created, hotel, category.code)

    # --- internals ----------------------------------------------------------------------

    def _require_category(self, code: str) -> ExpenseCategory:
        category = self._categories.get_by_code(code)
        if category is None:
            raise NotFoundError("Expense category not found.")
        return category

    def _to_responses(self, rows: ExpenseRows, hotel: Hotel) -> ExpenseResponses:
        codes = self._repository.category_codes_for([row.category_id for row in rows])
        return [self._render(row, hotel, codes.get(row.category_id, "")) for row in rows]

    @staticmethod
    def _render(entry: Expense, hotel: Hotel, category_code: str) -> ExpenseResponse:
        """The one place an Expense row becomes a response."""
        return ExpenseResponse.model_validate(
            {
                "hotel_public_id": hotel.public_id,
                "category_code": category_code,
                "expense_date": entry.expense_date,
                "amount": entry.amount,
                "tax_amount": entry.tax_amount,
                "currency": entry.currency,
                "description": entry.description,
                "vendor": entry.vendor,
                "invoice_reference": entry.invoice_reference,
                "is_recurring": entry.is_recurring,
                "recurrence_interval": entry.recurrence_interval,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
            }
        )

    def _translate(self, exc: IntegrityError) -> Exception:
        state, _ = _log("Expense", exc)
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate an expense constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("The expense refers to a record that does not exist here.")
        if state == SQLSTATE_NOT_NULL_VIOLATION:
            return ConflictError("A required value was missing from the expense.")
        return ConflictError("The request conflicts with the current state of the database.")


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ExpenseCategoryService",
    "ExpenseService",
    "RevenueCategoryService",
    "RevenueService",
]
