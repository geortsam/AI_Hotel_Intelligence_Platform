"""The server side of a browser's cookie session (R1.1, migration 0023).

One row per **refresh token**, never the token: ``token_hash`` is the SHA-256 of the random
secret the ``HttpOnly`` refresh cookie carries, so reading the table yields nothing a client
could present. Every row of one sign-in shares a ``family_id``. A refresh *rotates* -- it marks
its row ``used_at`` and inserts the next -- and a token presented a second time is a stolen copy
racing its owner, so the whole family is revoked. The access token names the family in its
``sid`` claim, so revoking a family stops its access tokens as well as its refreshes.

**No ``public_id``, no address, no user agent, no hotel.** Nothing addresses a session over
HTTP, and an identifier that exists is one something will eventually expose. ``ON DELETE
CASCADE`` from ``users``: a session means nothing without its account.

Not a ``TimestampMixin`` table: a row is written once and then only stamped -- ``used_at`` or
``revoked_at`` -- and those stamps *are* its history, so an ``updated_at`` would say nothing.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_column


class AuthSession(Base):
    """One refresh token of one sign-in."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Shared by every rotation of one sign-in; the access token's ``sid``.
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: SHA-256, lower-case hex, of the refresh secret. Never the secret.
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When this token was exchanged for the next. A second exchange is a replay.
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: When this token's family was ended: signed out, replayed, or a password change.
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_auth_sessions_user_id", "user_id"),
        Index("ix_auth_sessions_family_id", "family_id"),
    )
