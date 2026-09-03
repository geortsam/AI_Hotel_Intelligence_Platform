"""add users.password_changed_at

Strictly additive, and authorised for exactly this change: **one new column on one table**.
No other table is touched, and no constraint, index or trigger is altered.

This is the server-side fact that lets a password change revoke the tokens issued before it.
An access token already carries ``iat``; comparing it against this column answers "was this
token minted before the credential it authenticates changed?" without adding a claim, a token
version, or a table of revoked identifiers.

**``updated_at`` could not be reused for this.** The shared ``set_updated_at`` trigger fires on
every UPDATE, and a successful login writes ``last_login_at`` -- so an ``iat < updated_at`` rule
would revoke every other device's token on each sign-in, and could revoke the token being
issued in that very request. A dedicated column is the only honest way to say "the password
changed at this moment" and nothing else.

**NOT NULL with DEFAULT now()**, so existing rows get a value without a backfill pass and the
application never has to reason about a null. The consequence is deliberate and worth stating:
every access token outstanding when this migration runs is invalidated, because every row's
``password_changed_at`` becomes the moment of deployment. That is a one-time forced re-login,
and it is the safe direction to fail.

**No index.** The column is read only as part of the single-row lookup that already resolves a
token by ``public_id``, which ``uq_users_public_id`` already serves; it is never a predicate,
never sorted on and never joined. An index here would cost writes and buy nothing.

Revision ID: 0006_users_password_changed_at
Revises: 0005_platform_administration
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_users_password_changed_at"
down_revision: str | None = "0005_platform_administration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN password_changed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL"
    )


def downgrade() -> None:
    # Only this column. `users` keeps its nine Stage 4.1 columns, its constraints and its
    # updated_at trigger; nothing else in the schema is aware this column existed.
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS password_changed_at")
