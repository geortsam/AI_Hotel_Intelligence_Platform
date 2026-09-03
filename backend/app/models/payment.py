"""Payments, including refunds.

Refunds are modelled as rows with ``kind = 'refund'`` and a self-reference to the charge they
reverse, rather than as negative amounts. Negative amounts are simpler but lose the link to
the original charge, make partial refunds ambiguous, and force every query to handle sign.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, pk_column
from app.models.enums import PaymentKind, PaymentMethod, PaymentStatus

if TYPE_CHECKING:
    from app.models.booking import Booking


class Payment(TimestampMixin, Base):
    """Money actually moved against a booking."""

    __tablename__ = "payments"

    id: Mapped[int] = pk_column()
    # Added by migration 0002. Payments carry no natural key -- the only unique index is
    # partial and spans two nullable columns -- so without this a cash payment would have
    # no identifier at all, and a refund could not name the charge it reverses.
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    booking_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hotel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'charge'"))
    # Always positive. Direction is carried by `kind`, never by sign.
    amount: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    method: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    # Null until settled: a pending payment has no payment date.
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    provider: Mapped[str | None] = mapped_column(Text)
    transaction_reference: Mapped[str | None] = mapped_column(Text)
    refunded_payment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("payments.id", ondelete="RESTRICT")
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)

    # Reconciliation only. No PAN, no CVV, no expiry -- card data never enters this database.
    card_last_four: Mapped[str | None] = mapped_column(String(4))

    booking: Mapped[Booking] = relationship(
        back_populates="payments",
        primaryjoin="foreign(Payment.booking_id) == Booking.id",
    )
    # Adjacency list: `reverses` is the many-to-one side pointing at the original charge,
    # `refunds` is the collection of reversals hanging off it.
    reverses: Mapped[Payment | None] = relationship(
        back_populates="refunds", remote_side="Payment.id"
    )
    refunds: Mapped[list[Payment]] = relationship(back_populates="reverses")

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_payments_public_id"),
        ForeignKeyConstraint(
            ["booking_id", "hotel_id"],
            ["bookings.id", "bookings.hotel_id"],
            name="fk_payments_booking_id_hotel_id_bookings",
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(f"kind IN ({PaymentKind.sql_in_list()})", name="kind_valid"),
        CheckConstraint(f"method IN ({PaymentMethod.sql_in_list()})", name="method_valid"),
        CheckConstraint(f"status IN ({PaymentStatus.sql_in_list()})", name="status_valid"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        # A refund names the charge it reverses; a charge never does.
        CheckConstraint(
            "(kind = 'refund') = (refunded_payment_id IS NOT NULL)",
            name="refund_references_charge",
        ),
        CheckConstraint("status <> 'captured' OR paid_at IS NOT NULL", name="captured_has_paid_at"),
        CheckConstraint(
            "card_last_four IS NULL OR card_last_four ~ '^[0-9]{4}$'",
            name="card_last_four_format",
        ),
        # Idempotency: payment providers guarantee at-least-once webhook delivery, so a
        # duplicate callback must not create a second payment row.
        Index(
            "uq_payments_provider_transaction_reference",
            "provider",
            "transaction_reference",
            unique=True,
            postgresql_where=text("transaction_reference IS NOT NULL"),
        ),
        Index("ix_payments_booking_id", "booking_id"),
        Index(
            "ix_payments_hotel_id_paid_at",
            "hotel_id",
            "paid_at",
            postgresql_where=text("status = 'captured'"),
        ),
    )


__all__ = ["Payment"]
