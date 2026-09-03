"""Financial ledger persistence: revenue, expenses and the two category lookups.

Four repositories rather than one. The categories are **global** tables keyed by ``code``;
the ledger tables are **hotel-scoped** and keyed by nothing at all. Merging them would mean a
class whose every method had to ask which of two unrelated shapes it was dealing with.

**The ledger repositories take ``hotel_id`` on every query, and offer no single-row lookup.**
That is not caution, it is the schema: ``revenue`` and ``expenses`` have no ``public_id`` and
no unique constraint beyond the primary key, so there is no argument a ``get_one`` could
accept that names exactly one row.

The filters on the listing methods are the seam a later reporting stage reads through --
hotel, date range and category, which is precisely the column order of
``ix_revenue_hotel_id_revenue_date_category_id`` and its expenses counterpart. Nothing here
aggregates; that belongs to whatever consumes these queries.

Nothing here commits.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.finance import Expense, ExpenseCategory, Revenue, RevenueCategory


class RevenueCategoryRepository:
    """Data access for the global revenue-category lookup, keyed by ``code``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, category: RevenueCategory) -> RevenueCategory:
        self._session.add(category)
        self._session.flush()
        self._session.refresh(category)
        return category

    def apply_changes(self, category: RevenueCategory, changes: dict[str, Any]) -> RevenueCategory:
        """Apply a partial update to a managed instance and flush it."""
        for field, value in changes.items():
            setattr(category, field, value)
        self._session.flush()
        self._session.refresh(category)
        return category

    def delete(self, category: RevenueCategory) -> None:
        """Delete with a Core statement, so PostgreSQL's ON DELETE policy is authoritative.

        ``revenue.category_id`` is ON DELETE RESTRICT: a category with entries must not be
        removable. ``session.delete()`` would let the ORM try its own nullify cascade first
        and pre-empt that decision; a Core DELETE emits one statement and lets the database
        answer.
        """
        self._session.execute(sql_delete(RevenueCategory).where(RevenueCategory.id == category.id))
        self._session.flush()
        # The row is gone; detach the instance so the identity map cannot serve a ghost.
        self._session.expunge(category)

    def get_by_code(self, code: str) -> RevenueCategory | None:
        """The lookup is global by design -- the table has no ``hotel_id``."""
        return self._session.scalars(
            select(RevenueCategory).where(RevenueCategory.code == code)
        ).one_or_none()

    def count(self, *, is_active: bool | None = None) -> int:
        return (
            self._session.scalar(
                self._filtered(select(func.count()).select_from(RevenueCategory), is_active)
            )
            or 0
        )

    def list_page(
        self, *, limit: int, offset: int, is_active: bool | None = None
    ) -> list[RevenueCategory]:
        """Alphabetical by code: a lookup table is read as a catalogue, not a feed."""
        return list(
            self._session.scalars(
                self._filtered(select(RevenueCategory), is_active)
                .order_by(RevenueCategory.code.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    @staticmethod
    def _filtered(statement: Select[Any], is_active: bool | None) -> Select[Any]:
        if is_active is not None:
            statement = statement.where(RevenueCategory.is_active == is_active)
        return statement


class ExpenseCategoryRepository:
    """Data access for the global expense-category lookup, keyed by ``code``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, category: ExpenseCategory) -> ExpenseCategory:
        self._session.add(category)
        self._session.flush()
        self._session.refresh(category)
        return category

    def apply_changes(self, category: ExpenseCategory, changes: dict[str, Any]) -> ExpenseCategory:
        for field, value in changes.items():
            setattr(category, field, value)
        self._session.flush()
        self._session.refresh(category)
        return category

    def delete(self, category: ExpenseCategory) -> None:
        """Core DELETE, so ``expenses.category_id``'s RESTRICT is the database's decision."""
        self._session.execute(sql_delete(ExpenseCategory).where(ExpenseCategory.id == category.id))
        self._session.flush()
        self._session.expunge(category)

    def get_by_code(self, code: str) -> ExpenseCategory | None:
        return self._session.scalars(
            select(ExpenseCategory).where(ExpenseCategory.code == code)
        ).one_or_none()

    def count(self, *, is_active: bool | None = None) -> int:
        return (
            self._session.scalar(
                self._filtered(select(func.count()).select_from(ExpenseCategory), is_active)
            )
            or 0
        )

    def list_page(
        self, *, limit: int, offset: int, is_active: bool | None = None
    ) -> list[ExpenseCategory]:
        return list(
            self._session.scalars(
                self._filtered(select(ExpenseCategory), is_active)
                .order_by(ExpenseCategory.code.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    @staticmethod
    def _filtered(statement: Select[Any], is_active: bool | None) -> Select[Any]:
        if is_active is not None:
            statement = statement.where(ExpenseCategory.is_active == is_active)
        return statement


class RevenueRepository:
    """Data access for revenue lines, always scoped to one hotel.

    No update and no delete method: a ledger line cannot be named, so it cannot be edited or
    removed. A correction is a new line with a negative ``amount``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, entry: Revenue) -> Revenue:
        """Stage a line and flush so the database assigns its defaults."""
        self._session.add(entry)
        self._session.flush()
        self._session.refresh(entry)
        return entry

    def count_for_hotel(
        self,
        hotel_id: int,
        *,
        category_id: int | None = None,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
        booking_id: int | None = None,
    ) -> int:
        """Total lines under the same filters as the page, for the pagination envelope."""
        return (
            self._session.scalar(
                self._filtered(
                    select(func.count()).select_from(Revenue),
                    hotel_id,
                    category_id,
                    date_from,
                    date_to,
                    booking_id,
                )
            )
            or 0
        )

    def list_page_for_hotel(
        self,
        hotel_id: int,
        *,
        limit: int,
        offset: int,
        category_id: int | None = None,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
        booking_id: int | None = None,
    ) -> list[Revenue]:
        """One page of a hotel's revenue, newest first.

        ``id`` breaks ties: a date is not unique here -- the table has no unique constraint at
        all -- and without a total order a line can appear on two pages. It is used for
        ordering only and never leaves.
        """
        statement = self._filtered(
            select(Revenue), hotel_id, category_id, date_from, date_to, booking_id
        )
        return list(
            self._session.scalars(
                statement.order_by(Revenue.revenue_date.desc(), Revenue.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def category_codes_for(self, category_ids: list[int]) -> dict[int, str]:
        """Map internal category id -> code, for rendering a page in one extra query."""
        if not category_ids:
            return {}
        rows = (
            self._session.execute(
                select(RevenueCategory.id, RevenueCategory.code).where(
                    RevenueCategory.id.in_(category_ids)
                )
            )
            .tuples()
            .all()
        )
        return dict(rows)

    def booking_public_ids_for(self, booking_ids: list[int]) -> dict[int, Any]:
        """Map internal booking id -> public_id, for the rows that carry one."""
        if not booking_ids:
            return {}
        rows = (
            self._session.execute(
                select(Booking.id, Booking.public_id).where(Booking.id.in_(booking_ids))
            )
            .tuples()
            .all()
        )
        return dict(rows)

    @staticmethod
    def _filtered(
        statement: Select[Any],
        hotel_id: int,
        category_id: int | None,
        date_from: dt.date | None,
        date_to: dt.date | None,
        booking_id: int | None,
    ) -> Select[Any]:
        """Hotel scope plus the reporting filters, applied to count and page identically.

        Shared so the two can never disagree about what is being counted. ``hotel_id`` is
        applied here and is not optional. The filter order follows
        ``ix_revenue_hotel_id_revenue_date_category_id``.
        """
        statement = statement.where(Revenue.hotel_id == hotel_id)
        if date_from is not None:
            statement = statement.where(Revenue.revenue_date >= date_from)
        if date_to is not None:
            statement = statement.where(Revenue.revenue_date <= date_to)
        if category_id is not None:
            statement = statement.where(Revenue.category_id == category_id)
        if booking_id is not None:
            statement = statement.where(Revenue.booking_id == booking_id)
        return statement


class ExpenseRepository:
    """Data access for expense lines, always scoped to one hotel.

    As with revenue: no update, no delete, no single-row lookup. There is no key.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, entry: Expense) -> Expense:
        self._session.add(entry)
        self._session.flush()
        self._session.refresh(entry)
        return entry

    def count_for_hotel(
        self,
        hotel_id: int,
        *,
        category_id: int | None = None,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
    ) -> int:
        return (
            self._session.scalar(
                self._filtered(
                    select(func.count()).select_from(Expense),
                    hotel_id,
                    category_id,
                    date_from,
                    date_to,
                )
            )
            or 0
        )

    def list_page_for_hotel(
        self,
        hotel_id: int,
        *,
        limit: int,
        offset: int,
        category_id: int | None = None,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
    ) -> list[Expense]:
        """One page of a hotel's expenses, newest first, with ``id`` as the tiebreaker."""
        statement = self._filtered(select(Expense), hotel_id, category_id, date_from, date_to)
        return list(
            self._session.scalars(
                statement.order_by(Expense.expense_date.desc(), Expense.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def category_codes_for(self, category_ids: list[int]) -> dict[int, str]:
        """Map internal category id -> code, for rendering a page in one extra query."""
        if not category_ids:
            return {}
        rows = (
            self._session.execute(
                select(ExpenseCategory.id, ExpenseCategory.code).where(
                    ExpenseCategory.id.in_(category_ids)
                )
            )
            .tuples()
            .all()
        )
        return dict(rows)

    @staticmethod
    def _filtered(
        statement: Select[Any],
        hotel_id: int,
        category_id: int | None,
        date_from: dt.date | None,
        date_to: dt.date | None,
    ) -> Select[Any]:
        """Hotel scope plus the reporting filters, in the column order of
        ``ix_expenses_hotel_id_expense_date_category_id``."""
        statement = statement.where(Expense.hotel_id == hotel_id)
        if date_from is not None:
            statement = statement.where(Expense.expense_date >= date_from)
        if date_to is not None:
            statement = statement.where(Expense.expense_date <= date_to)
        if category_id is not None:
            statement = statement.where(Expense.category_id == category_id)
        return statement


__all__ = [
    "ExpenseCategoryRepository",
    "ExpenseRepository",
    "RevenueCategoryRepository",
    "RevenueRepository",
]
