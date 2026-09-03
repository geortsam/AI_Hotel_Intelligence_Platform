"""add the users table for authentication

Strictly additive, and authorised for exactly this change: **one new table**. No existing
table, column, constraint, index, foreign key or trigger is altered, and no data is migrated.

Why a new table rather than extending an existing one. Stage 4.1's reconnaissance found no
column anywhere in the schema capable of holding a credential, and ``guests`` -- the only
table with an email -- cannot serve as an identity store:

* it is hotel-scoped (``hotel_id`` NOT NULL), so a guest belongs to exactly one property,
  while the person who signs in to manage a property is not a guest of it;
* its ``email`` is nullable and unique only per hotel, so the same address legitimately
  exists at several properties and most rows have none -- it cannot be a login identity;
* it is the most PII-dense table in the system and is returned by a listing endpoint, which
  is the last place password hashes should live.

**Deliberately absent: ``hotel_id``, any role, and any permission column.** Who a user *is*
and what a user may *do* are different questions, and the second belongs to Stage 4.2. Adding
a role column now would bake an authorization model into an authentication migration.

``ck_users_email_format`` is the format constraint ``guests.email`` notably lacks. The
pattern relies on PostgreSQL's advanced regular expressions, where ``\\s`` is supported and
``^``/``$`` anchor the whole string rather than each line -- both verified against the live
server before this migration was written, since every other CHECK in this schema uses plain
bracket expressions.

Revision ID: 0003_users_authentication
Revises: 0002_payments_public_id
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_users_authentication"
down_revision: str | None = "0002_payments_public_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE users (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            is_active BOOLEAN DEFAULT true NOT NULL,
            last_login_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_users PRIMARY KEY (id),
            CONSTRAINT uq_users_public_id UNIQUE (public_id),
            CONSTRAINT uq_users_email UNIQUE (email),
            CONSTRAINT ck_users_email_format
                CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$')
        )
        """
    )
    # The same trigger every other timestamped table uses. The function already exists from
    # 0001; this only attaches it.
    op.execute(
        "CREATE TRIGGER trg_users_set_updated_at BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    # The trigger goes with the table; dropping it separately would be redundant. The
    # set_updated_at FUNCTION is shared with twelve other tables and must survive.
    op.execute("DROP TABLE IF EXISTS users")
