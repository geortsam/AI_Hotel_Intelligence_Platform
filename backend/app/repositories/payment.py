"""Payment persistence.

**Every lookup is scoped by ``booking_id``**, which is itself resolved through the hotel. As
elsewhere, ``public_id`` is globally unique, so an unscoped lookup would work and would be
able to reach another property's financial records -- the method does not exist.

That matters more here than in any previous domain. ``refunded_payment_id`` is a
**single-column** foreign key with no hotel or booking in it: the database alone would let a
refund at hotel A point at a charge at hotel B. The scoped lookup below is what closes that
gap, and it is the reason there is no ``get_by_public_id(public_id)``.

There are no update or delete methods. Payments are append-only.

Nothing here commits.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.payment import Payment


class PaymentRepository:
    """Data access for payments, always scoped to one booking."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, payment: Payment) -> Payment:
        """Stage a posting and flush so the database assigns its keys and defaults.

        ``flush`` -- not ``commit`` -- so ``public_id`` is populated and any constraint
        violation surfaces here while the service still owns the transaction.
        """
        self._session.add(payment)
        self._session.flush()
        self._session.refresh(payment)
        return payment

    def get_in_booking(self, booking_id: int, public_id: uuid.UUID) -> Payment | None:
        """The scoped lookup. Both halves are required; there is no public-id-only variant.

        Used for reads *and* for resolving the parent of a refund, so a refund can only ever
        reverse a payment on the same booking.
        """
        return self._session.scalars(
            select(Payment).where(Payment.booking_id == booking_id, Payment.public_id == public_id)
        ).one_or_none()

    def count_for_booking(self, booking_id: int) -> int:
        """Total postings against one booking, for the pagination envelope."""
        return (
            self._session.scalar(
                select(func.count()).select_from(Payment).where(Payment.booking_id == booking_id)
            )
            or 0
        )

    def list_page_for_booking(self, booking_id: int, *, limit: int, offset: int) -> list[Payment]:
        """One page of a booking's postings, oldest first.

        Ascending by creation time, then by internal id: a ledger reads forwards, and several
        postings can share a timestamp. The id is used for ordering only and never leaves.
        """
        return list(
            self._session.scalars(
                select(Payment)
                .where(Payment.booking_id == booking_id)
                .order_by(Payment.created_at.asc(), Payment.id.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def public_ids_for(self, payment_ids: list[int]) -> dict[int, uuid.UUID]:
        """Map internal id -> public_id, for rendering a refund's parent reference.

        One query for the whole page rather than a lazy hop per refund.
        """
        if not payment_ids:
            return {}
        rows = (
            self._session.execute(
                select(Payment.id, Payment.public_id).where(Payment.id.in_(payment_ids))
            )
            .tuples()
            .all()
        )
        return dict(rows)


__all__ = ["PaymentRepository"]
