"""Data access for ``auth_sessions``, the server side of cookie sessions (R1.1).

Knows SQL and nothing about cookies, CSRF or HTTP. Every write is a flush; the service owns the
transaction.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, exists, select, update
from sqlalchemy.orm import Session

from app.models.auth_session import AuthSession


class AuthSessionRepository:
    """The refresh tokens a browser session is made of."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self, *, user_id: int, family_id: uuid.UUID, token_hash: str, expires_at: dt.datetime
    ) -> AuthSession:
        row = AuthSession(
            user_id=user_id, family_id=family_id, token_hash=token_hash, expires_at=expires_at
        )
        self._session.add(row)
        self._session.flush()
        return row

    def lock_by_hash(self, token_hash: str) -> AuthSession | None:
        """The row a presented refresh secret names, locked ``FOR UPDATE``.

        Locked because a refresh is a read-then-write: two requests presenting the same secret
        must not both see it unused and both rotate it. The second waits, then sees ``used_at``
        and is treated as the replay it is.
        """
        return self._session.scalars(
            select(AuthSession).where(AuthSession.token_hash == token_hash).with_for_update()
        ).one_or_none()

    def family_is_live(self, family_id: uuid.UUID, now: dt.datetime) -> bool:
        """Whether this sign-in still has an unrevoked, unexpired, unused refresh token.

        The test every cookie-authenticated request makes of its access token's ``sid``: a
        signed-out, replayed or password-changed family has none, so its access tokens stop
        before they expire.
        """
        return bool(
            self._session.scalar(
                select(
                    exists().where(
                        AuthSession.family_id == family_id,
                        AuthSession.revoked_at.is_(None),
                        AuthSession.used_at.is_(None),
                        AuthSession.expires_at > now,
                    )
                )
            )
        )

    def revoke_family(self, family_id: uuid.UUID, now: dt.datetime) -> int:
        """End one sign-in: every token of the family not already revoked. Returns how many."""
        result = self._session.execute(
            update(AuthSession)
            .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        self._session.flush()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def revoke_all_for_user(self, user_id: int, now: dt.datetime) -> int:
        """End every sign-in of one account -- a password change. Returns how many tokens."""
        result = self._session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        self._session.flush()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def delete_expired_for_user(self, user_id: int, now: dt.datetime) -> int:
        """Remove one account's rows that can never be presented again.

        Run when the account signs in, so the table holds, per account, only what could still
        matter: rows already past their expiry are deleted, revoked or not. A revoked row that
        has not expired is kept, because a replay of it must still be recognised as one.
        """
        result = self._session.execute(
            delete(AuthSession).where(AuthSession.user_id == user_id, AuthSession.expires_at <= now)
        )
        self._session.flush()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


__all__ = ["AuthSessionRepository"]
