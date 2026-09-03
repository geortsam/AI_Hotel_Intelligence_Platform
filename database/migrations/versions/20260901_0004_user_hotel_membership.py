"""add the user_hotels membership table

Strictly additive, and authorised for exactly this change: **one new table**. Nothing on
``users``, ``hotels`` or any other table is altered.

This is the join that Stage 4.1 deliberately left out. ``users`` answers "who is this?";
``user_hotels`` answers "which properties, and in what capacity?". Keeping them apart is what
lets one person hold different roles at different hotels, and what stops a login identity
being owned by a tenant.

**Role is CHECK-constrained TEXT**, matching the nine vocabularies already in this schema
(approved decision 3). A native enum makes removing a value painful, and a separate role
table would buy nothing while roles carry no attributes of their own.

**The two ON DELETE policies differ on purpose.** Deleting a user CASCADEs their memberships
away -- a membership without a user is meaningless and is not business data. Deleting a hotel
RESTRICTs, matching the seven other RESTRICT policies pointing at ``hotels``: a property with
staff attached should fail loudly rather than quietly shed them.

**Deliberately absent**: an ``is_active`` flag (revoking access is deleting the row; two ways
to express one thing invites a bug where only one is checked), an ownership column
(``owner`` is a role), any platform-administrator concept, and a ``public_id`` -- Stage 4.2
exposes no endpoint that addresses a membership, so there is nothing to name yet.

Revision ID: 0004_user_hotel_membership
Revises: 0003_users_authentication
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_user_hotel_membership"
down_revision: str | None = "0003_users_authentication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE user_hotels (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            user_id BIGINT NOT NULL,
            hotel_id BIGINT NOT NULL,
            role TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_user_hotels PRIMARY KEY (id),
            CONSTRAINT uq_user_hotels_user_id_hotel_id UNIQUE (user_id, hotel_id),
            CONSTRAINT fk_user_hotels_user_id_users
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT fk_user_hotels_hotel_id_hotels
                FOREIGN KEY (hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT,
            CONSTRAINT ck_user_hotels_role_valid
                CHECK (role = ANY (ARRAY['viewer', 'staff', 'manager', 'owner']))
        )
        """
    )
    # UNIQUE (user_id, hotel_id) already indexes user_id as its leading column, so "which
    # hotels does this user belong to?" -- the query every authenticated request makes -- is
    # covered. This index serves the other direction: "who belongs to this hotel?", which a
    # future membership-management screen needs and which the unique index cannot answer.
    op.execute("CREATE INDEX ix_user_hotels_hotel_id ON user_hotels (hotel_id)")
    op.execute(
        "CREATE TRIGGER trg_user_hotels_set_updated_at BEFORE UPDATE ON user_hotels "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    # The trigger and the index go with the table. The shared set_updated_at FUNCTION is used
    # by thirteen other tables and must survive.
    op.execute("DROP TABLE IF EXISTS user_hotels")
