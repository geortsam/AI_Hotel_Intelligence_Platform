"""allow the platform_admin.granted and platform_admin.revoked audit actions

Authorised in the v2.0 roadmap pass (R1.3a) for exactly one change: **one CHECK constraint on
one column**, ``ck_audit_events_action_valid``, widened by two values, as 0009, 0012 and 0014
widened it. No table, column, index or trigger changes; ``ck_audit_events_resource_type_valid``
is untouched, because both events are about a ``user``, a type 0007 already allows; and
``trg_audit_events_append_only`` is not touched. Migrations 0001-0020 are unchanged.

**Why.** A platform-administrator grant was an out-of-band INSERT into ``platform_admins``
(0005 exposes no API for it, deliberately), and the first-run runbook said plainly that the
grant was not audited: no application code ran, so no event could be written. R1.3a adds an
operator command, ``python -m app.jobs.platform_admin``, which performs the same grant -- and
its revocation -- inside the application, and records each in the same transaction as the
change. The database refused both values until this migration widened the list.

**Still no API.** Who may grant or revoke through the API is an open decision (R1.3b), so the
command is reachable only by whoever can run code against the database, which is the trust
boundary the out-of-band INSERT already had. The events carry no actor (``actor_user_id`` NULL,
"no authenticated user caused it") and no hotel: they are platform-scoped, like the catalogue
events, and appear in ``/api/v1/platform/audit-events``.

**The downgrade is conditionally safe**, like 0009's, 0012's and 0014's: it succeeds only while
no audit event uses either value, because ``audit_events`` is append-only.

Revision ID: 0021_audit_platform_grants
Revises: 0020_guest_email_rules
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_audit_platform_grants"
down_revision: str | None = "0020_guest_email_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The twenty-three actions 0007, 0009, 0012 and 0014 established, verbatim. Repeated rather
#: than read from the enum: a migration describes the schema at a point in history.
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
    "tool.invoked",
    "document.created",
    "document.version_created",
    "document.withdrawn",
)
_ADDED_ACTIONS = ("platform_admin.granted", "platform_admin.revoked")


def _check(actions: tuple[str, ...]) -> str:
    values = ", ".join(f"'{action}'" for action in actions)
    return (
        "ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_action_valid "
        f"CHECK (action = ANY (ARRAY[{values}]))"
    )


def upgrade() -> None:
    # Dropped and re-added in one transaction: PostgreSQL has no ALTER CONSTRAINT for a CHECK
    # expression, and Alembic wraps the migration, so the column is never unconstrained.
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_action_valid")
    op.execute(_check((*_PREVIOUS_ACTIONS, *_ADDED_ACTIONS)))


def downgrade() -> None:
    # Succeeds only while no event uses either value; see the module docstring.
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_action_valid")
    op.execute(_check(_PREVIOUS_ACTIONS))
