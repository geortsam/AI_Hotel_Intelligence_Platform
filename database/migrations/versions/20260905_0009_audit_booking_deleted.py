"""allow the booking.deleted audit action

Authorised for exactly one change: **one CHECK constraint on one column**, widened by one
value. No table is created or dropped, no column is added or altered, no index changes, and --
this matters more than the rest -- ``trg_audit_events_append_only`` is not touched, disabled or
re-created. Migrations 0001-0008 are untouched.

**Why this migration exists at all.** ``ck_audit_events_action_valid`` is a closed list that
migration 0007 materialised in SQL. Stage 4.5.15 needed ``booking.deleted``, and the database
refused it outright::

    INSERT INTO audit_events (action, ...) VALUES ('booking.deleted', ...);
    -> CheckViolation, SQLSTATE 23514, ck_audit_events_action_valid

That refusal is the constraint doing its job: the vocabulary is closed precisely so a value the
application would not recognise cannot be stored by any path, including an out-of-band INSERT.
Widening it is therefore a schema change and not an application one, and it was raised for
authorisation before being written rather than slipped in beside the feature that needed it.

**Booking deletion was the last unaudited destructive operation in the domain.** Cancelling a
booking is a status change and has been recorded since Stage 4.5.12; DELETE removes the row,
its allocations and its priced nights outright, and until now left nothing at all behind. The
audit event is the only thing that survives to say the stay existed.

**The downgrade is conditionally safe, and that is worth stating plainly.** It re-adds the
eighteen-value constraint, which PostgreSQL validates against existing rows -- so it succeeds
only while no ``booking.deleted`` row exists. Once one does, this migration is effectively
one-way, because ``audit_events`` is append-only and the offending rows cannot be removed to
make room for the narrower constraint.

That is not a defect in this migration. It is what a closed vocabulary on an immutable table
means, and it will be true of every future action added to this list: the value can be added,
and it can be withdrawn only until it is used. The alternative -- dropping the CHECK so the
column accepts anything -- would trade a one-way migration for an audit trail that cannot say
what its own vocabulary is, which is a far worse bargain.

Revision ID: 0009_audit_booking_deleted
Revises: 0008_audit_retention_archive
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_audit_booking_deleted"
down_revision: str | None = "0008_audit_retention_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The eighteen values migration 0007 established. Repeated verbatim rather than read from the
#: enum: a migration describes the schema at a point in history, and one that imported today's
#: application code would silently change meaning the next time the enum did.
_ORIGINAL_ACTIONS = (
    "booking.created",
    "booking.status_changed",
    "booking.stay_modified",
    "payment.created",
    "payment.refund_created",
    "membership.created",
    "membership.role_changed",
    "membership.removed",
    "auth.password_changed",
    "amenity.created",
    "amenity.updated",
    "amenity.deleted",
    "revenue_category.created",
    "revenue_category.updated",
    "revenue_category.deleted",
    "expense_category.created",
    "expense_category.updated",
    "expense_category.deleted",
)

#: The nineteenth.
_ADDED_ACTION = "booking.deleted"


def _check(actions: tuple[str, ...]) -> str:
    values = ", ".join(f"'{action}'" for action in actions)
    return (
        "ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_action_valid "
        f"CHECK (action = ANY (ARRAY[{values}]))"
    )


def upgrade() -> None:
    # Dropped and re-added rather than edited: PostgreSQL has no ALTER CONSTRAINT for a CHECK
    # expression. Both statements run in one transaction -- Alembic wraps each migration -- so
    # there is no window in which the column is unconstrained.
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_action_valid")
    op.execute(_check((*_ORIGINAL_ACTIONS, _ADDED_ACTION)))


def downgrade() -> None:
    # Succeeds only while no `booking.deleted` row exists; see the module docstring. PostgreSQL
    # validates the narrower constraint against the table, so this fails loudly rather than
    # leaving rows the constraint forbids -- which is the right way for it to fail.
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_action_valid")
    op.execute(_check(_ORIGINAL_ACTIONS))
