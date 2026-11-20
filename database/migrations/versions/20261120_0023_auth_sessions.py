"""add auth_sessions: the server side of a browser's cookie session

Authorised in the v2.0 roadmap pass (R1.1) for exactly one change: **one new table**,
``auth_sessions``. No existing table, column, constraint, index, foreign key or trigger is
altered, and no data is migrated. Migrations 0001-0022 are unchanged.

**Why.** Until R1.1 the browser held its bearer token in ``sessionStorage``, where any script on
the origin can read it (docs/architecture.md section 7.1). The browser now signs in through
``POST /api/v1/auth/session``, which keeps every credential in an ``HttpOnly`` cookie: a short
access token, and a **refresh token** that renews it. A refresh token must be revocable before
it expires -- by signing out, by a password change, by its own reuse -- so the server has to
remember which ones it issued. This table is that memory.

**One row per refresh token, never the token.** ``token_hash`` is the SHA-256 of the random
secret the cookie carries; a database read yields nothing a client could present. Every row of
one sign-in shares a ``family_id``: a refresh *rotates* -- marks its row ``used_at`` and inserts
the next -- and a token presented a second time is a stolen copy racing its owner, so the whole
family is revoked (``revoked_at``). The access token names the family (its ``sid`` claim), so a
revoked family stops its access tokens too, not only its refreshes.

**What it deliberately does not hold:** no ``public_id`` -- nothing addresses a session over
HTTP, and an identifier that exists is one something will eventually expose -- no address, no
user agent and no hotel. ``ON DELETE CASCADE`` from ``users``: a session is meaningless without
its account.

**The downgrade drops the table**, signing every browser out; nothing else depends on it.

Revision ID: 0023_auth_sessions
Revises: 0022_catalogue_versions
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_auth_sessions"
down_revision: str | None = "0022_catalogue_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auth_sessions (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            user_id BIGINT NOT NULL,
            family_id UUID NOT NULL,
            token_hash TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            used_at TIMESTAMP WITH TIME ZONE,
            revoked_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_auth_sessions PRIMARY KEY (id),
            CONSTRAINT fk_auth_sessions_user_id_users FOREIGN KEY (user_id)
                REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT uq_auth_sessions_token_hash UNIQUE (token_hash),
            CONSTRAINT ck_auth_sessions_token_hash_format CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_auth_sessions_expires_after_created CHECK (expires_at > created_at)
        )
        """
    )
    op.execute("CREATE INDEX ix_auth_sessions_user_id ON auth_sessions (user_id)")
    op.execute("CREATE INDEX ix_auth_sessions_family_id ON auth_sessions (family_id)")


def downgrade() -> None:
    op.execute("DROP TABLE auth_sessions")
