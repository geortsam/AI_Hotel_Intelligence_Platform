"""null only the optional reference when a review's or a revenue line's parent is deleted

Authorised for exactly this: **three foreign keys recreated**. No table, column, index, trigger or
other constraint is created, altered or dropped, and no row is written.

**The defect (Issue H3).** Migration 0001 declared three composite foreign keys ``ON DELETE SET
NULL``:

* ``fk_revenue_booking_id_hotel_id_bookings``  ``revenue(booking_id, hotel_id) -> bookings``
* ``fk_reviews_booking_id_hotel_id_bookings``  ``reviews(booking_id, hotel_id) -> bookings``
* ``fk_reviews_guest_id_hotel_id_guests``      ``reviews(guest_id, hotel_id) -> guests``

Without a column list PostgreSQL sets EVERY referencing column to NULL, and ``hotel_id`` is NOT
NULL on both tables. So the policy could never fire: the parent's delete failed with 23502 on
``hotel_id`` -- an undeclared RESTRICT. The declared intent, keep the review or the revenue line
and forget the stay or the author, never happened.

**The correction.** Each key is recreated with a column list (PostgreSQL 15+):
``ON DELETE SET NULL (booking_id)`` or ``ON DELETE SET NULL (guest_id)``. Deleting the parent now
nulls only the optional reference; ``hotel_id`` -- the tenant -- is never touched. Everything
else about each key is as 0001 declared it: same name, same columns and referenced columns,
``MATCH SIMPLE``, ``ON UPDATE NO ACTION``, not deferrable. Under ``MATCH SIMPLE`` a row whose
reference is NULL is not checked, which is exactly how an external review and walk-in revenue
were already stored; a detached row is that same, already valid, shape.

What still blocks: ``payments`` RESTRICTs a booking's deletion and ``bookings`` RESTRICTs a
guest's. Neither is touched here.

No backfill: only the constraint definitions change, and every existing row satisfies both the
old and the new definition. ``downgrade`` restores 0001's exact definitions.

Revision ID: 0018_composite_set_null_columns
Revises: 0017_llm_invocation_retention
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_composite_set_null_columns"
down_revision: str | None = "0017_llm_invocation_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, constraint, referencing column, referenced table, ON DELETE after 0018). The
#: referencing key is always ``(<column>, hotel_id)`` and the referenced key ``(id, hotel_id)``.
KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "revenue",
        "fk_revenue_booking_id_hotel_id_bookings",
        "booking_id",
        "bookings",
        "SET NULL (booking_id)",
    ),
    (
        "reviews",
        "fk_reviews_booking_id_hotel_id_bookings",
        "booking_id",
        "bookings",
        "SET NULL (booking_id)",
    ),
    (
        "reviews",
        "fk_reviews_guest_id_hotel_id_guests",
        "guest_id",
        "guests",
        "SET NULL (guest_id)",
    ),
)

#: What 0001 declared for all three: no column list, so every referencing column was nulled.
BEFORE = "SET NULL"


def _recreate(*, upgrading: bool) -> None:
    """Drop and re-add each key with its 0018 action, or with 0001's when downgrading."""
    for table, name, column, parent, after in KEYS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} "
            f"FOREIGN KEY ({column}, hotel_id) REFERENCES {parent} (id, hotel_id) "
            f"ON DELETE {after if upgrading else BEFORE}"
        )


def upgrade() -> None:
    _recreate(upgrading=True)


def downgrade() -> None:
    _recreate(upgrading=False)
