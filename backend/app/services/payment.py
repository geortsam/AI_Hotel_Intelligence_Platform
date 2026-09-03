"""Payment business logic and unit-of-work boundaries.

Payments are **append-only**: this service posts charges and refunds and reads them back. It
has no update or delete method, and neither does the repository. A financial record is not
edited; a mistake is corrected by posting a reversal.

**What this service deliberately does NOT do**, because the frozen schema does not say it and
inventing financial rules was out of scope:

* It does not cap a refund at the charge's amount. The schema constrains only ``amount > 0``;
  nothing prevents refunding more than was taken. Reported as a finding, not silently fixed.
* It does not forbid refunding a refund. ``refunded_payment_id`` accepts any payment row.
* It does not post to the ``revenue`` ledger. That is a separate domain and a later stage.
* It does not reconcile against ``bookings.total_amount``.

**What it does enforce, and why that is not an invented rule**: a refund's parent must be a
payment *on the same booking*. ``refunded_payment_id`` is a single-column foreign key with no
hotel or booking in it, so the database alone would permit a refund at hotel A to reference a
charge at hotel B. Closing that is tenant isolation -- a standing project principle -- not a
financial policy.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    NotFoundError,
    sqlstate_of,
)
from app.models.booking import Booking
from app.models.hotel import Hotel
from app.models.payment import Payment
from app.repositories.booking import BookingRepository
from app.repositories.payment import PaymentRepository
from app.schemas.common import Page
from app.schemas.payment import ChargeCreate, PaymentResponse, RefundCreate
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: The partial unique index giving webhook idempotency: (provider, transaction_reference)
#: WHERE transaction_reference IS NOT NULL. Named so a conflict is reported precisely without
#: quoting the processor reference back.
IDEMPOTENCY_CONSTRAINT = "uq_payments_provider_transaction_reference"


def _constraint_name(exc: IntegrityError) -> str | None:
    """The violated constraint's NAME from driver diagnostics, never its message."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None


class PaymentService:
    """Domain operations on payments, always within one booking at one hotel."""

    def __init__(
        self,
        session: Session,
        repository: PaymentRepository,
        bookings: BookingRepository,
        scope: HotelScopeResolver,
    ) -> None:
        self._session = session
        self._repository = repository
        self._bookings = bookings
        self._scope = scope

    # --- reads --------------------------------------------------------------------------

    def get(
        self,
        hotel_public_id: uuid.UUID,
        booking_public_id: uuid.UUID,
        payment_public_id: uuid.UUID,
    ) -> PaymentResponse:
        """Return one posting on this booking, or raise 404."""
        hotel, booking = self._require_booking(hotel_public_id, booking_public_id)
        payment = self._require_payment(booking, payment_public_id)
        return self._to_response(payment, hotel, booking)

    def list(
        self,
        hotel_public_id: uuid.UUID,
        booking_public_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
    ) -> Page[PaymentResponse]:
        """One page of this booking's postings, oldest first."""
        hotel, booking = self._require_booking(hotel_public_id, booking_public_id)
        total = self._repository.count_for_booking(booking.id)
        rows = self._repository.list_page_for_booking(
            booking.id, limit=page_size, offset=(page - 1) * page_size
        )
        return Page.build(
            items=[self._to_response(row, hotel, booking) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    # --- writes -------------------------------------------------------------------------

    def create_charge(
        self,
        hotel_public_id: uuid.UUID,
        booking_public_id: uuid.UUID,
        payload: ChargeCreate,
    ) -> PaymentResponse:
        """Post a charge against a booking and commit.

        ``refunded_payment_id`` stays null: ``ck_payments_refund_references_charge`` is a
        biconditional, so a charge must not carry one.
        """
        hotel, booking = self._require_booking(hotel_public_id, booking_public_id)

        payment = Payment(
            booking_id=booking.id,
            hotel_id=hotel.id,
            kind="charge",
            refunded_payment_id=None,
            **payload.model_dump(),
        )
        return self._persist(payment, hotel, booking)

    def create_refund(
        self,
        hotel_public_id: uuid.UUID,
        booking_public_id: uuid.UUID,
        payload: RefundCreate,
    ) -> PaymentResponse:
        """Post a refund against an existing payment on the same booking, and commit.

        The parent is resolved through the booking, so a refund can only ever reverse a
        payment the caller already had access to -- the single-column foreign key cannot
        make that guarantee on its own.
        """
        hotel, booking = self._require_booking(hotel_public_id, booking_public_id)

        parent = self._repository.get_in_booking(booking.id, payload.refunds_public_id)
        if parent is None:
            # Scoped lookup: a payment on another booking or hotel is *not found*, which is
            # also what stops this endpoint being used to probe for one.
            raise NotFoundError("The payment being refunded was not found on this booking.")

        fields = payload.model_dump(exclude={"refunds_public_id"})
        payment = Payment(
            booking_id=booking.id,
            hotel_id=hotel.id,
            kind="refund",
            refunded_payment_id=parent.id,
            **fields,
        )
        return self._persist(payment, hotel, booking)

    # --- internals ----------------------------------------------------------------------

    def _persist(self, payment: Payment, hotel: Hotel, booking: Booking) -> PaymentResponse:
        """Write one posting and commit, translating any integrity failure."""
        try:
            created = self._repository.add(payment)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._to_response(created, hotel, booking)

    def _require_booking(
        self, hotel_public_id: uuid.UUID, booking_public_id: uuid.UUID
    ) -> tuple[Hotel, Booking]:
        """Resolve hotel then booking, in that order.

        Order matters for the error a client sees: an unknown hotel reports the hotel, so a
        caller can tell which part of the path is wrong.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        booking = self._bookings.get_by_hotel_and_public_id(hotel.id, booking_public_id)
        if booking is None:
            raise NotFoundError("Booking not found for this hotel.")
        return hotel, booking

    def _require_payment(self, booking: Booking, payment_public_id: uuid.UUID) -> Payment:
        """Resolve a payment **within this booking**, or raise 404."""
        payment = self._repository.get_in_booking(booking.id, payment_public_id)
        if payment is None:
            raise NotFoundError("Payment not found for this booking.")
        return payment

    def _to_response(self, payment: Payment, hotel: Hotel, booking: Booking) -> PaymentResponse:
        """Build the response, translating the internal parent id into its public form."""
        refunds_public_id = None
        if payment.refunded_payment_id is not None:
            refunds_public_id = self._repository.public_ids_for([payment.refunded_payment_id]).get(
                payment.refunded_payment_id
            )

        return PaymentResponse.model_validate(
            {
                "hotel_public_id": hotel.public_id,
                "booking_public_id": booking.public_id,
                "public_id": payment.public_id,
                "kind": payment.kind,
                "amount": payment.amount,
                "currency": payment.currency,
                "method": payment.method,
                "status": payment.status,
                "paid_at": payment.paid_at,
                "provider": payment.provider,
                "transaction_reference": payment.transaction_reference,
                "refunds_public_id": refunds_public_id,
                "failure_reason": payment.failure_reason,
                "card_last_four": payment.card_last_four,
                "created_at": payment.created_at,
                "updated_at": payment.updated_at,
            }
        )

    def _translate(self, exc: IntegrityError) -> Exception:
        """Turn a database integrity error into a domain error, leaking nothing.

        No ``exc_info``: a payment's driver message carries the amount, the processor
        reference and the card fragment. The SQLSTATE and constraint name decide what to say.
        """
        state = sqlstate_of(exc)
        constraint = _constraint_name(exc)
        logger.warning("Payment integrity error (sqlstate=%s, constraint=%s)", state, constraint)

        if state == SQLSTATE_UNIQUE_VIOLATION:
            if constraint == IDEMPOTENCY_CONSTRAINT:
                # The reference is deliberately not quoted back; the client already sent it.
                return ConflictError(
                    "A payment with this provider transaction reference has already been recorded."
                )
            return ConflictError("That value is already taken by another payment.")
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a payment constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("This payment is still referenced by other records.")
        return ConflictError("The request conflicts with the current state of the database.")


__all__ = ["DEFAULT_PAGE_SIZE", "IDEMPOTENCY_CONSTRAINT", "MAX_PAGE_SIZE", "PaymentService"]
