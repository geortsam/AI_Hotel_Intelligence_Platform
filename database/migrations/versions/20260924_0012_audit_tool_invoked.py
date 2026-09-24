"""allow the tool.invoked audit action and the tool resource type

Authorised as the smallest extension of the existing audit vocabulary that Stage 7.6 needs:
**two CHECK constraints on two columns, each widened by one value.** No table is created or
dropped, no column is added or altered, no index changes, and ``trg_audit_events_append_only`` is
not touched, disabled or re-created. Migrations 0001-0011 are untouched. The archive table has no
vocabulary CHECK (migration 0008 explains why) and needs nothing.

**Why this migration exists at all.** Stage 7.6 requires every copilot tool invocation to be
recorded "in the existing append-only audit trail", using "the existing audit vocabulary", and
permits "the smallest possible explicit extension" where that vocabulary is insufficient. It is
insufficient: none of the nineteen actions describes a tool being called — every one of them is
a committed change to a booking, a payment, a membership, a user or a catalogue entry — and none
of the seven resource types is a tool. Recording a tool call under ``booking.created`` or any
other existing value would be a false record. Both constraints are closed lists, so the database
refuses the new values with SQLSTATE 23514 until they are widened, exactly as it refused
``booking.deleted`` until 0009.

The alternative the roadmap sketched — a separate ``copilot_tool_invocations`` table — was
rejected: it would be the "second audit system" the Stage 7.6 brief forbids, and it would sit
outside the append-only trigger and the retention machinery the existing trail already has.

**The downgrade is conditionally safe, as 0009's is.** It re-adds the narrower constraints, which
PostgreSQL validates against existing rows, so it succeeds only while no ``tool.invoked`` row
exists. Once one does, ``audit_events`` being append-only makes this migration one-way. That is
what a closed vocabulary on an immutable table means; it is stated rather than discovered.

Revision ID: 0012_audit_tool_invoked
Revises: 0011_demand_prediction_public_id
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_audit_tool_invoked"
down_revision: str | None = "0011_demand_prediction_public_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The nineteen actions 0007 and 0009 established, repeated verbatim rather than read from the
#: enum: a migration describes the schema at a point in history, and one that imported today's
#: application code would silently change meaning the next time the enum did.
_PREVIOUS_ACTIONS = (
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
    "booking.deleted",
)

#: The twentieth.
_ADDED_ACTION = "tool.invoked"

#: The seven resource types 0007 established.
_PREVIOUS_RESOURCE_TYPES = (
    "booking",
    "payment",
    "membership",
    "user",
    "amenity",
    "revenue_category",
    "expense_category",
)

#: The eighth.
_ADDED_RESOURCE_TYPE = "tool"


def _check(constraint: str, column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return (
        f"ALTER TABLE audit_events ADD CONSTRAINT {constraint} "
        f"CHECK ({column} = ANY (ARRAY[{listed}]))"
    )


def upgrade() -> None:
    # Dropped and re-added rather than edited: PostgreSQL has no ALTER CONSTRAINT for a CHECK
    # expression. Alembic wraps the migration in one transaction, so there is no window in which
    # either column is unconstrained.
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_action_valid")
    op.execute(
        _check(
            "ck_audit_events_action_valid",
            "action",
            (*_PREVIOUS_ACTIONS, _ADDED_ACTION),
        )
    )
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_resource_type_valid")
    op.execute(
        _check(
            "ck_audit_events_resource_type_valid",
            "resource_type",
            (*_PREVIOUS_RESOURCE_TYPES, _ADDED_RESOURCE_TYPE),
        )
    )


def downgrade() -> None:
    # Succeeds only while no `tool.invoked` row exists; see the module docstring.
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_resource_type_valid")
    op.execute(
        _check("ck_audit_events_resource_type_valid", "resource_type", _PREVIOUS_RESOURCE_TYPES)
    )
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_action_valid")
    op.execute(_check("ck_audit_events_action_valid", "action", _PREVIOUS_ACTIONS))
