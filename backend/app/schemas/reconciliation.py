"""The financial summary of one booking.

Stage 4.5.9. Every figure here is derived at read time from rows that are already
authoritative -- the nightly rates and the payment ledger. Nothing in this module is stored,
and no column backs any of it, which is the point: a persisted summary would be a second
source of truth that could disagree with the rows it summarises.

**Two totals are reported, and the difference between them is the finding.**
``accommodation_total`` is what the server can prove the stay is worth: the sum of its nightly
rates. ``declared_total`` is ``bookings.total_amount``, which arrives from the client and,
by approved decision 10, is deliberately NOT defined as that sum -- discounts, taxes and
packages legitimately break the equality. The schema has nowhere to record a tax, a fee or a
discount, so the server cannot explain a divergence; it can only report one. Reconciliation
therefore states both figures and whether they agree, rather than picking a winner or
pretending the difference is not there.
"""

from __future__ import annotations

import decimal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PaymentState

#: Money is reported at the scale the columns store it at: Numeric(14, 2).
Money = decimal.Decimal


class BookingReconciliation(BaseModel):
    """What a booking is worth, what has been paid, and what remains."""

    model_config = ConfigDict(from_attributes=True)

    booking_public_id: uuid.UUID
    currency: str

    #: SERVER-AUTHORITATIVE. The sum of this booking's nightly rates.
    accommodation_total: Money = Field(
        description="Sum of the booking's nightly rates. Derived by the server."
    )
    #: INFORMATIONAL. What the client contracted, echoed so a divergence is visible.
    declared_total: Money = Field(
        description="The contracted total supplied on the booking. Not authoritative."
    )
    #: False when the two disagree. The server cannot say why: nothing in the schema records
    #: taxes, fees or discounts.
    totals_agree: bool

    charged_total: Money
    refunded_total: Money
    #: ``charged_total - refunded_total``. May be negative if more was refunded than charged,
    #: which the per-charge refund cap makes unreachable through the API but which is reported
    #: honestly rather than clamped.
    net_paid: Money
    #: ``accommodation_total - net_paid``. Positive means still owed, negative means overpaid.
    outstanding_amount: Money

    payment_state: PaymentState


__all__ = ["BookingReconciliation"]
