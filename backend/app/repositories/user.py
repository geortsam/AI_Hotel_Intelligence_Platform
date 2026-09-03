"""User persistence.

Queries and flushes; never commits, never hashes, never verifies. The repository stores
whatever digest it is handed and has no idea how it was produced -- keeping Argon2id in
``app.core.security`` means the algorithm can be changed without touching data access, and it
keeps a credential-handling routine out of the layer that talks to the database.

There is no ``hotel_id`` here and no membership lookup. Which hotels a user may reach is
Stage 4.2's question.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User


class UserRepository:
    """Data access for authentication identities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, user: User) -> User:
        """Stage a user and flush so the database assigns its keys and defaults.

        ``flush`` -- not ``commit`` -- so ``public_id`` and ``created_at`` are populated and
        the uniqueness constraint fires here, while the service still owns the transaction.
        """
        self._session.add(user)
        self._session.flush()
        self._session.refresh(user)
        return user

    def get_by_email(self, email: str) -> User | None:
        """Look up by login identity.

        ``uq_users_email`` is global, so this needs no other scope -- unlike every domain
        repository in this codebase, a user is not owned by a hotel.
        """
        return self._session.scalars(select(User).where(User.email == email)).one_or_none()

    def get_by_public_id(self, public_id: uuid.UUID) -> User | None:
        """Resolve the subject of a token.

        Read on every authenticated request, which is why it is a single indexed lookup on
        ``uq_users_public_id``.
        """
        return self._session.scalars(select(User).where(User.public_id == public_id)).one_or_none()

    def set_password(self, user: User, password_hash: str, changed_at: dt.datetime) -> User:
        """Store a new Argon2id digest and the moment it was set, then flush.

        Takes the DIGEST, never the plaintext: the password does not cross this boundary, so
        it cannot reach a query log, a driver error or a repository-level exception.

        Both columns are written together in one flush. They are two halves of one fact --
        "this is the credential, and this is when it became the credential" -- and a state
        where the hash changed but the timestamp did not would leave every token issued
        against the OLD password still valid. The service commits them as one unit.
        """
        user.password_hash = password_hash
        user.password_changed_at = changed_at
        self._session.flush()
        self._session.refresh(user)
        return user

    def record_login(self, user: User, when: dt.datetime) -> User:
        """Stamp a successful sign-in and flush.

        Separate from ``get_by_email`` so a *failed* attempt writes nothing at all: a login
        that touches the database on every guess is a denial-of-service surface.
        """
        user.last_login_at = when
        self._session.flush()
        self._session.refresh(user)
        return user


__all__ = ["UserRepository"]
