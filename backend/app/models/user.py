"""Authentication identities.

A ``users`` row is a **principal** -- someone who signs in. It is deliberately not related to
``guests``, which is tenant data describing people who stay at a property: a guest is a
subject of the system, a user is an actor in it, and conflating them would tie a login to a
single hotel.

**No ``hotel_id``, no role, no permissions.** This table answers "who is this?" and nothing
else. "What may they reach?" is Stage 4.2's question, and answering it here would put an
authorization model inside an authentication table.

``password_hash`` holds an Argon2id digest and never leaves the repository layer -- no schema
in ``app.schemas.auth`` exposes it, and a structural test asserts that.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, pk_column


class User(TimestampMixin, Base):
    """Someone who can authenticate."""

    __tablename__ = "users"

    id: Mapped[int] = pk_column()
    #: The identity carried in a JWT's ``sub``. A sequential BIGINT there would be
    #: enumerable and would put an internal key in a token.
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    #: Globally unique, unlike ``guests.email`` which is unique only per hotel and nullable.
    #: A login identity has to be global.
    email: Mapped[str] = mapped_column(Text, nullable=False)
    #: An Argon2id digest, roughly 97 characters. Never plaintext, never returned.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: Lets an account be disabled without deleting it. It must NOT change what a failed
    #: login looks like from outside -- see AuthService.authenticate.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the password was last set (Stage 4.5.2). Compared against an access token's
    #: ``iat`` to revoke everything minted before a password change.
    #:
    #: NOT NULL, defaulted by the database, so there is no "unknown" state to reason about:
    #: an account always has a moment its credential was established, and at registration
    #: that moment is now.
    #:
    #: **Internal.** No response model exposes it -- not `/auth/me`, not registration -- and
    #: a structural test asserts that across every generated OpenAPI component. It is
    #: security machinery, and publishing when a password last changed tells an attacker
    #: which accounts are stale.
    password_changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_users_public_id"),
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint(
            r"email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'",
            name="email_format",
        ),
    )


__all__ = ["User"]
