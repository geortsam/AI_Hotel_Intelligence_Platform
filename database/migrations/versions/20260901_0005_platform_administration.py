"""add the platform_admins table

Strictly additive, and authorised for exactly this change: **one new table**. Nothing on
``users``, ``user_hotels``, ``hotels`` or any other table is altered.

Stage 4.2 closed the three global catalogues -- ``amenities``, ``revenue_categories``,
``expense_categories`` -- to every caller, because a row belonging to no hotel cannot be
governed by a per-hotel grant. This table is the identity that can govern them.

**It is deliberately NOT a column on ``users``.** ``users`` answers "who is this?" and has
been kept free of authorization since Stage 4.1; a ``role`` column there would make the
authentication table the place two different authorization models meet. It is equally NOT a
row in ``user_hotels``: that table's ``hotel_id`` is NOT NULL, so expressing "authority over
no particular hotel" there would need a nullable tenant key and a partial unique index, and
every membership query would then have to remember to exclude the rows that are not
memberships. Two tables, two questions, no null-means-something-else.

**A platform administrator is not a member of anything.** Nothing here grants access to
hotel-scoped data: ``HotelAccessPolicy`` is untouched by this migration and by Stage 4.3, so
an administrator with no ``user_hotels`` row still gets the same 404 wall as any other
non-member. The absence of a join between this table and ``hotels`` is the point.

**Role is CHECK-constrained TEXT**, matching the ten vocabularies already in this schema.
There is exactly one value today. The constraint is not ceremony: Stage 4.3 exposes no API
for granting platform administration, so the grant path in practice is an out-of-band INSERT,
and the CHECK is what stops that path inventing ``superadmin`` or ``root`` -- roles the
application would never recognise but would happily store.

**``user_id`` is the primary key**, with no surrogate. A user holds at most one platform
role, so a second row could only ever restate or contradict the first.

**ON DELETE CASCADE**: a platform grant without a user is meaningless and is not business
data, exactly as with ``user_hotels.user_id``.

**Deliberately absent**: an ``is_active`` flag (revoking is deleting the row), a
``granted_by`` column (Stage 4.3 has no grant API, so there is no actor to record and a
column that is always NULL teaches a reader nothing), and a ``public_id`` -- no endpoint
addresses a platform grant, so there is nothing to name.

Revision ID: 0005_platform_administration
Revises: 0004_user_hotel_membership
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_platform_administration"
down_revision: str | None = "0004_user_hotel_membership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE platform_admins (
            user_id BIGINT NOT NULL,
            role TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_platform_admins PRIMARY KEY (user_id),
            CONSTRAINT fk_platform_admins_user_id_users
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT ck_platform_admins_role_valid
                CHECK (role = ANY (ARRAY['platform_admin']))
        )
        """
    )
    # The shared function from 0001, reused rather than copied -- as users and user_hotels do.
    op.execute(
        "CREATE TRIGGER trg_platform_admins_set_updated_at BEFORE UPDATE ON platform_admins "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    # The trigger and the constraints go with the table; `users` is untouched either way.
    op.execute("DROP TABLE IF EXISTS platform_admins")
