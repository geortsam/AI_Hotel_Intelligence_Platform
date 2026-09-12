"""What a stay modification does to the money (Stage 4.5.24).

Stage 4.5.23 priced NEW bookings on the server and deliberately stopped there: changing the
price of a booking a guest has already agreed to is a financial act, not a calculation, and it
was left as a stated dependency. This is that dependency, and the first thing it does is refuse
to invent anything.

**The figures already existed.** Nothing here is a new accounting concept:

* the value of a stay is ``SUM(booking_room_nights.rate)`` -- the figure
  :class:`~app.services.reconciliation.ReconciliationService` already calls authoritative, and
  the only monetary number about a booking the server itself produces;
* what has been collected is ``charged - refunded`` over the payment ledger, on the same
  definition of what counts as money that the refund cap uses;
* the resulting position is already a vocabulary the platform reports --
  ``unpaid``/``partially_paid``/``paid``/``overpaid``.

So a repricing is the difference between two authoritative sums, read against a ledger that
already knows how to describe every outcome. No invoice, no receivable, no tax, no capture.

**A calculation is not a transaction.** This module computes what is owed or owed back and
says so. It creates no payment, reverses no charge, and calls no provider -- the platform does
not own one, and a workflow that quietly moved money because a date changed would be the
worst possible reading of "repricing". Settling the difference remains a separate, deliberate
act through the payment endpoints that already exist.

**The refundable figure is capped by what was actually collected.** A stay that gets cheaper by
200 on a booking that has paid 30 does not make 200 refundable; it makes 30 refundable and the
remaining 170 a smaller balance owed. Reporting the raw difference as refundable would invite
a refund the ledger cannot support, and the refund cap would then refuse it at the last
moment -- a worse place to find out.

**``bookings.total_amount`` is not touched, and that is a decision.** Approved decision 10
defines it as the CONTRACTED figure and expects discounts, taxes and packages to break its
equality with the nightly sum. Recomputing it from the nights would silently destroy whatever
commercial agreement those differences represent, and the schema records none of it, so the
server cannot reconstruct it. Reconciliation already reports both numbers side by side and
flags whether they agree; after a repricing it goes on doing exactly that.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass

#: The scale every reported figure is quantised to, matching ``Numeric(14, 2)``.
MONEY_SCALE = decimal.Decimal("0.01")

ZERO = decimal.Decimal("0.00")

#: No money moves either way: the stay is worth what it was worth.
ADJUSTMENT_NONE = "none"
#: The stay got dearer. The guest owes the difference; nothing has been charged.
ADJUSTMENT_AMOUNT_DUE = "amount_due"
#: The stay got cheaper AND money has been collected. Some of it is refundable; nothing has
#: been refunded.
ADJUSTMENT_REFUNDABLE = "refundable"

ADJUSTMENTS = (ADJUSTMENT_NONE, ADJUSTMENT_AMOUNT_DUE, ADJUSTMENT_REFUNDABLE)


@dataclass(frozen=True, slots=True)
class RepricingOutcome:
    """The financial consequence of a stay modification. A statement, not an instruction."""

    currency: str
    previous_total: decimal.Decimal
    new_total: decimal.Decimal
    #: ``new_total - previous_total``. Positive means dearer, negative means cheaper.
    difference: decimal.Decimal
    #: What the guest owes as a result of THIS change. Zero when the stay got cheaper.
    additional_amount_due: decimal.Decimal
    #: What could be refunded as a result of this change, capped by what was collected.
    refundable_amount: decimal.Decimal
    #: The booking's balance after the change: positive still owed, negative overpaid.
    outstanding_after: decimal.Decimal
    adjustment: str

    @property
    def is_adjusted(self) -> bool:
        return self.adjustment != ADJUSTMENT_NONE


class RepricingPolicy:
    """Turns two totals and a ledger position into a financial consequence.

    Pure, like :class:`~app.services.pricing.PricingPolicy` and for the same reason: the money
    question can then be asked in a unit test without a booking, a session or a hotel.
    """

    def outcome(
        self,
        *,
        currency: str,
        previous_total: decimal.Decimal,
        new_total: decimal.Decimal,
        net_paid: decimal.Decimal,
    ) -> RepricingOutcome:
        """The consequence of moving a booking from *previous_total* to *new_total*.

        *net_paid* is ``charged - refunded`` on the same definition the refund cap uses. It
        can legitimately be negative if more has been refunded than charged, which is why the
        refundable figure floors at zero rather than assuming it cannot.
        """
        previous = self._money(previous_total)
        new = self._money(new_total)
        difference = self._money(new - previous)

        due = difference if difference > ZERO else ZERO
        # Capped twice: by the size of the reduction, and by what there is to give back.
        # ``max(net_paid, ZERO)`` because a ledger already refunded past its charges has
        # nothing further to return, and a negative cap would produce a negative refund.
        refundable = (
            min(-difference, max(self._money(net_paid), ZERO)) if difference < ZERO else ZERO
        )

        if difference > ZERO:
            adjustment = ADJUSTMENT_AMOUNT_DUE
        elif difference < ZERO and refundable > ZERO:
            adjustment = ADJUSTMENT_REFUNDABLE
        else:
            # A reduction against a booking that has paid nothing moves no money: it lowers
            # the balance owed. Reporting it as "refundable" would name money that does not
            # exist to be returned.
            adjustment = ADJUSTMENT_NONE

        return RepricingOutcome(
            currency=currency,
            previous_total=previous,
            new_total=new,
            difference=difference,
            additional_amount_due=self._money(due),
            refundable_amount=self._money(refundable),
            outstanding_after=self._money(new - net_paid),
            adjustment=adjustment,
        )

    @staticmethod
    def _money(value: decimal.Decimal) -> decimal.Decimal:
        """Render at the scale the columns store, without changing the value."""
        return value.quantize(MONEY_SCALE)


__all__ = [
    "ADJUSTMENTS",
    "ADJUSTMENT_AMOUNT_DUE",
    "ADJUSTMENT_NONE",
    "ADJUSTMENT_REFUNDABLE",
    "MONEY_SCALE",
    "ZERO",
    "RepricingOutcome",
    "RepricingPolicy",
]
