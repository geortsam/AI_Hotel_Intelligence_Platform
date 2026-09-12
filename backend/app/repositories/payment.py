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

import decimal
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import VOIDED_PAYMENT_STATUSES, PaymentKind
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

    def lock_for_refund(self, payment_id: int) -> Payment:
        """Re-read one payment under a row lock, refreshing it in place.

        ``SELECT ... FOR UPDATE`` on the parent charge. This is the row every refund against
        that charge contends on, so locking it serialises them: the second request blocks
        until the first commits and then re-reads, because READ COMMITTED re-evaluates a
        locked row after acquiring it.

        Without it two requests both read "100 charged, 0 refunded, 100 refundable" and both
        write a 100 refund -- 200 refunded from a 100 charge, with neither request having done
        anything wrong in isolation. The database is the only thing that can arbitrate that,
        which is why this is a row lock and not a Python lock.

        ``populate_existing`` matters: the instance is already in the identity map, and
        without it SQLAlchemy returns the attributes it loaded before the lock was held, so
        the amount and status the cap is computed from would be the stale ones.

        No commit. The caller's transaction owns the lock until it ends.
        """
        return self._session.scalars(
            select(Payment)
            .where(Payment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()

    def refunded_total(self, parent_payment_id: int) -> decimal.Decimal:
        """How much has already been refunded against one payment.

        Summed in the database rather than by walking the ORM ``refunds`` collection: the
        collection may be stale or unloaded, and this must read committed state. Rows whose
        status says no money moved are excluded -- see ``VOIDED_PAYMENT_STATUSES``.

        Call this only while :meth:`lock_for_refund` holds the parent, or the number is a
        snapshot that another transaction may already have invalidated.
        """
        total = self._session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.refunded_payment_id == parent_payment_id,
                Payment.status.not_in(VOIDED_PAYMENT_STATUSES),
            )
        )
        return decimal.Decimal(total or 0)

    def get_in_booking(self, booking_id: int, public_id: uuid.UUID) -> Payment | None:
        """The scoped lookup. Both halves are required; there is no public-id-only variant.

        Used for reads *and* for resolving the parent of a refund, so a refund can only ever
        reverse a payment on the same booking.
        """
        return self._session.scalars(
            select(Payment).where(Payment.booking_id == booking_id, Payment.public_id == public_id)
        ).one_or_none()

    def ledger_totals_for_booking(self, booking_id: int) -> tuple[decimal.Decimal, decimal.Decimal]:
        """``(charged, refunded)`` for one booking, in a single aggregate query.

        Both sides come back together with ``FILTER`` rather than as two round trips, and the
        rows are never loaded: a booking with a thousand postings costs the same one query as
        a booking with two.

        The status filter is :data:`VOIDED_PAYMENT_STATUSES`, exactly as
        :meth:`refunded_total` uses for the refund cap. That is deliberate -- reconciliation
        and refund integrity must agree about what counts as money, and they agree here by
        sharing the definition rather than by two lists that happen to match today.
        """
        counted = Payment.status.not_in(VOIDED_PAYMENT_STATUSES)
        row = self._session.execute(
            select(
                func.coalesce(
                    func.sum(Payment.amount).filter(
                        counted, Payment.kind == PaymentKind.CHARGE.value
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(Payment.amount).filter(
                        counted, Payment.kind == PaymentKind.REFUND.value
                    ),
                    0,
                ),
            ).where(Payment.booking_id == booking_id)
        ).one()
        return decimal.Decimal(row[0] or 0), decimal.Decimal(row[1] or 0)

    def currencies_for_booking(self, booking_id: int) -> set[str]:
        """The distinct currencies posted against one booking.

        Reconciliation sums amounts, and summing across currencies would produce a number
        that means nothing. This is how it finds out first. Voided rows are included on
        purpose: a failed USD charge on a EUR booking is still a data problem worth
        surfacing, and excluding it would hide the inconsistency rather than report it.
        """
        return set(
            self._session.scalars(
                select(Payment.currency).where(Payment.booking_id == booking_id).distinct()
            ).all()
        )

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
