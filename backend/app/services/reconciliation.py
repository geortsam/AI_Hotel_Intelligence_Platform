"""Reconciling a booking against its payment ledger.

Stage 4.5.9. Read-only: this service opens no transaction, writes nothing, and holds no lock.
It has no ``commit`` and no ``rollback`` for the same reason ``analytics`` and ``intelligence``
do not -- there is nothing here to undo.

**Where the authoritative number comes from, and why it is not the one the client sent.**
``bookings.total_amount`` arrives in the create payload. It is stored, echoed back, and read
by nothing that makes a financial decision -- the revenue service says so explicitly, and so
does the payment service. Treating it as the booking's worth would let a client assert what it
is owed. The server's own figure is the sum of ``booking_room_nights.rate``: one row per room
per night, complete by constraint trigger, non-negative by CHECK, and the table the model
calls the atomic financial unit of the platform. That sum is what payments are reconciled
against; the declared total is reported beside it so the divergence is visible.

**Why the totals may legitimately differ.** Approved decision 10 defines ``total_amount`` as
the contracted figure and expects discounts, taxes and packages to break the equality with the
nightly sum. Nothing in the schema records any of those, so the server cannot attribute a
difference to one -- it can only report that there is one. Inventing a tax model to close the
gap would be inventing financial semantics the data cannot support.

**Currency.** A booking carries exactly one currency and its nights carry none of their own,
so the accommodation sum is unambiguous. Payments carry their own, and a refund is already
pinned to its charge's currency by Stage 4.5.8 -- but a CHARGE in a currency other than the
booking's is still possible, so this service refuses to aggregate across currencies rather
than adding numbers that do not add. No conversion is performed and none is introduced.

**Concurrency.** The figures are a snapshot of one transaction and are described as nothing
more. A charge committed a moment later makes them stale, which is a property of any read and
not a defect to be locked away; the write-side invariants that actually protect money -- the
refund cap and its row lock -- are untouched by this stage.
"""

from __future__ import annotations

import decimal
import uuid

from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.enums import PaymentState
from app.repositories.booking import BookingRepository
from app.repositories.payment import PaymentRepository
from app.schemas.reconciliation import BookingReconciliation
from app.services.scope import HotelScopeResolver

#: The scale every reported figure is quantised to, matching Numeric(14, 2). Quantising the
#: results rather than the inputs keeps the arithmetic exact: Decimal addition of two-place
#: values is already exact, and this only fixes how a zero or a difference is rendered.
MONEY_SCALE = decimal.Decimal("0.01")

ZERO = decimal.Decimal("0.00")


class ReconciliationService:
    """Answers what a booking is worth and what has been paid against it."""

    def __init__(
        self,
        session: Session,
        bookings: BookingRepository,
        payments: PaymentRepository,
        scope: HotelScopeResolver,
    ) -> None:
        self._session = session
        self._bookings = bookings
        self._payments = payments
        self._scope = scope

    def booking_reconciliation(
        self, hotel_public_id: uuid.UUID, booking_public_id: uuid.UUID
    ) -> BookingReconciliation:
        """Reconcile one booking against its ledger.

        Resolution order is the project's usual one and matters here for the same reason it
        does everywhere: the hotel wall answers first, so a booking at another property is
        *not found* rather than the subject of a financial answer.

        Three queries, fixed: the booking, its nightly sum, its ledger totals. Not one per
        room, not one per night, not one per payment.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        booking = self._bookings.get_by_hotel_and_public_id(hotel.id, booking_public_id)
        if booking is None:
            raise NotFoundError("Booking not found for this hotel.")

        accommodation = self._bookings.accommodation_total(booking.id)
        charged, refunded = self._payments.ledger_totals_for_booking(booking.id)
        self._require_single_currency(booking.id, booking.currency)

        net_paid = charged - refunded
        outstanding = accommodation - net_paid

        return BookingReconciliation(
            booking_public_id=booking.public_id,
            currency=booking.currency,
            accommodation_total=self._money(accommodation),
            declared_total=self._money(booking.total_amount),
            totals_agree=accommodation == booking.total_amount,
            charged_total=self._money(charged),
            refunded_total=self._money(refunded),
            net_paid=self._money(net_paid),
            outstanding_amount=self._money(outstanding),
            payment_state=self._state(accommodation, net_paid),
        )

    # --- internals ----------------------------------------------------------------------

    def _require_single_currency(self, booking_id: int, currency: str) -> None:
        """Refuse to report a total that added different currencies together.

        The ledger sum is a plain SUM: if a booking carried a EUR charge and a USD charge it
        would produce a number that means nothing. Rather than convert -- this stage
        introduces no FX -- the reconciliation fails cleanly and says which booking it is
        about, naming only the currency codes the caller can already read on the payments.
        """
        currencies = self._payments.currencies_for_booking(booking_id)
        foreign = sorted(code for code in currencies if code != currency)
        if foreign:
            # Built with a loop rather than `str.join`: `app.services` is audited for query
            # verbs by exact identifier and `join` is one of them. The rule is deliberately
            # blunt, so this reads plainly instead of dodging it cleverly.
            listed = foreign[0]
            for code in foreign[1:]:
                listed = f"{listed}, {code}"
            raise ConflictError(
                "This booking cannot be reconciled: it has payments in "
                f"{listed} but the booking is in {currency}. "
                "No conversion is performed."
            )

    @staticmethod
    def _money(value: decimal.Decimal) -> decimal.Decimal:
        """Render at the scale the columns store, without changing the value."""
        return value.quantize(MONEY_SCALE)

    @staticmethod
    def _state(accommodation: decimal.Decimal, net_paid: decimal.Decimal) -> PaymentState:
        """The derived state.

        ``unpaid`` is reserved for nothing net having been received, so a booking whose
        payments have been fully refunded reads as unpaid again rather than as partially paid
        -- which is what the money says.

        A zero-value booking with nothing paid is ``paid``: there is nothing outstanding, and
        calling it unpaid would report a debt that does not exist.
        """
        if net_paid > accommodation:
            return PaymentState.OVERPAID
        if net_paid == accommodation:
            return PaymentState.PAID
        if net_paid <= ZERO:
            return PaymentState.UNPAID
        return PaymentState.PARTIALLY_PAID


__all__ = ["ReconciliationService"]
