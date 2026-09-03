"""The financial ledger: revenue and expenses, each with a category lookup.

Room revenue is NOT posted here (approved decision 21). ``booking_room_nights`` is the single
source of truth for it, and posting it in both places would double every room-revenue figure,
and with it ADR, RevPAR and total revenue. This ledger carries non-room streams -- food and
beverage, spa, parking, events.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, pk_column
from app.models.enums import RecurrenceInterval

if TYPE_CHECKING:
    from app.models.booking import Booking
    from app.models.hotel import Hotel


class RevenueCategory(Base):
    """Lookup rather than an enum: hotels add revenue streams, and requiring a schema
    migration to open a gift shop is the wrong trade."""

    __tablename__ = "revenue_categories"

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Gives the metrics job a data-driven way to EXCLUDE room-revenue rows from
    # `other_revenue`, rather than hard-coding a string comparison.
    is_room_revenue: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    entries: Mapped[list[Revenue]] = relationship(back_populates="category")

    __table_args__ = (UniqueConstraint("code", name="uq_revenue_categories_code"),)


class Revenue(TimestampMixin, Base):
    """A non-room earning event."""

    __tablename__ = "revenue"

    id: Mapped[int] = pk_column()
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("revenue_categories.id", ondelete="RESTRICT"), nullable=False
    )
    # Nullable by design: a non-resident eating in the restaurant generates revenue attached
    # to no booking. Forcing a link would either lose that revenue or invent fake bookings.
    booking_id: Mapped[int | None] = mapped_column(BigInteger)

    revenue_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    # Deliberately NOT constrained positive: refunds and corrections are posted as negative
    # lines, which is standard ledger practice and keeps the running total additive.
    amount: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_amount: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    description: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)

    hotel: Mapped[Hotel] = relationship(back_populates="revenue_entries")
    category: Mapped[RevenueCategory] = relationship(back_populates="entries")
    booking: Mapped[Booking | None] = relationship(
        back_populates="revenue_entries",
        primaryjoin="foreign(Revenue.booking_id) == Booking.id",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["booking_id", "hotel_id"],
            ["bookings.id", "bookings.hotel_id"],
            name="fk_revenue_booking_id_hotel_id_bookings",
            ondelete="SET NULL",
        ),
        CheckConstraint("tax_amount >= 0", name="tax_amount_non_negative"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        Index(
            "ix_revenue_hotel_id_revenue_date_category_id",
            "hotel_id",
            "revenue_date",
            "category_id",
        ),
        Index(
            "ix_revenue_booking_id",
            "booking_id",
            postgresql_where=text("booking_id IS NOT NULL"),
        ),
    )


class ExpenseCategory(Base):
    """Expense lookup, with the fixed/variable split profitability analysis needs."""

    __tablename__ = "expense_categories"

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_fixed_cost: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    entries: Mapped[list[Expense]] = relationship(back_populates="category")

    __table_args__ = (UniqueConstraint("code", name="uq_expense_categories_code"),)


class Expense(TimestampMixin, Base):
    """A cost incurred by a property."""

    __tablename__ = "expenses"

    id: Mapped[int] = pk_column()
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("expense_categories.id", ondelete="RESTRICT"), nullable=False
    )

    expense_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    # Negative permitted, for credit notes.
    amount: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_amount: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    description: Mapped[str | None] = mapped_column(Text)
    vendor: Mapped[str | None] = mapped_column(Text)
    invoice_reference: Mapped[str | None] = mapped_column(Text)

    is_recurring: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    recurrence_interval: Mapped[str | None] = mapped_column(Text)

    hotel: Mapped[Hotel] = relationship(back_populates="expenses")
    category: Mapped[ExpenseCategory] = relationship(back_populates="entries")

    __table_args__ = (
        CheckConstraint("tax_amount >= 0", name="tax_amount_non_negative"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint(
            "is_recurring = (recurrence_interval IS NOT NULL)", name="recurrence_consistent"
        ),
        CheckConstraint(
            "recurrence_interval IS NULL "
            f"OR recurrence_interval IN ({RecurrenceInterval.sql_in_list()})",
            name="recurrence_interval_valid",
        ),
        Index(
            "ix_expenses_hotel_id_expense_date_category_id",
            "hotel_id",
            "expense_date",
            "category_id",
        ),
    )


__all__ = ["Expense", "ExpenseCategory", "Revenue", "RevenueCategory"]
