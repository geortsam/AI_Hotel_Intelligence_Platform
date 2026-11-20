"""give the three global catalogues an updated_at, maintained by the shared trigger

Authorised in the v2.0 roadmap pass (R3.3) for exactly one change per table: ``amenities``,
``revenue_categories`` and ``expense_categories`` each gain an ``updated_at timestamptz NOT NULL
DEFAULT now()`` column and a ``trg_<table>_set_updated_at`` trigger running the
``set_updated_at()`` function 0001 installed -- reused, not copied. No other column, constraint,
index or trigger changes; ``created_at`` is not added, because nothing needs it. Migrations
0001-0021 are unchanged.

**Why.** Issue H6 made the PATCH of every form-backed hotel resource conditional on an optional
``If-Match`` naming the ``updated_at`` the client loaded. The catalogues had no such column, so
their PATCHes stayed last-write-wins: two platform administrators editing the same field of
the same entry left the later one in place, silently. With a version the catalogue PATCHes take
the same precondition and refuse a stale one with 412 ``STALE_UPDATE``.

**Existing rows** receive the time this migration ran: the column's default fills them as it is
added. That instant is not when the row was last written -- nothing recorded that -- but it is a
valid version, which is all ``If-Match`` needs: the next write moves it.

**The downgrade is safe**: it drops the three triggers and then the three columns, losing only
the versions themselves.

Revision ID: 0022_catalogue_versions
Revises: 0021_audit_platform_grants
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022_catalogue_versions"
down_revision: str | None = "0021_audit_platform_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The three global catalogues, which belong to no hotel.
_TABLES = ("amenities", "revenue_categories", "expense_categories")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_set_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_set_updated_at ON {table}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN updated_at")
