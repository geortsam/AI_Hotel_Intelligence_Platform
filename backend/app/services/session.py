"""Cookie sessions for the browser: sign in, refresh, sign out (R1.1).

Until R1.1 the browser kept its bearer token in ``sessionStorage``, where any script on the
origin can read it. A cookie session keeps every credential out of reach of script:

* an **access token** -- the same short HS256 JWT a bearer login returns, plus a ``sid`` naming
  this session -- in an ``HttpOnly`` cookie;
* a **refresh token** -- 256 random bits, stored server-side only as a SHA-256 digest in
  ``auth_sessions`` -- in an ``HttpOnly`` cookie scoped to the session routes;
* a **CSRF token**, derived from the ``sid`` (``app.core.security.csrf_token``), in a cookie the
  page *can* read and must echo in ``X-CSRF-Token`` on every request that changes something.

**Rotation, and reuse detection.** A refresh consumes its token -- ``used_at`` -- and issues the
next in the same family. A consumed token presented again means two parties hold it: the
owner and a thief racing them. Which is which cannot be told, so the whole family is revoked
and both must sign in again.

**What ends a session.** Signing out (the family is revoked); a replayed refresh token (the
family is revoked); a password change (every family of the account, in the password's own
transaction -- ``AuthService.change_password``); a deactivated account; or twelve idle hours
(``SESSION_REFRESH_TTL_HOURS``). Each ends the session's access tokens too, because every
cookie-authenticated request asks whether its ``sid`` is still live.

This service knows nothing about cookies or HTTP; ``app.api.session_cookies`` does.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import CsrfError, InvalidTokenError
from app.core.security import csrf_matches, csrf_token, hash_refresh_secret, new_refresh_secret
from app.models.user import User
from app.repositories.auth_session import AuthSessionRepository
from app.schemas.auth import UserResponse
from app.services.auth import AuthService


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """What a sign-in or a refresh hands the HTTP layer to put in cookies."""

    user: UserResponse
    access_token: str
    access_expires_in: int
    refresh_secret: str
    refresh_expires_in: int
    csrf_token: str


class SessionService:
    """The lifecycle of a browser's cookie session."""

    def __init__(
        self,
        session: Session,
        sessions: AuthSessionRepository,
        auth: AuthService,
        settings: Settings,
    ) -> None:
        self._session = session
        self._sessions = sessions
        self._auth = auth
        self._settings = settings

    @property
    def _refresh_ttl(self) -> dt.timedelta:
        return dt.timedelta(hours=self._settings.session_refresh_ttl_hours)

    def sign_in(self, email: str, password: str) -> IssuedSession:
        """Verify the credentials exactly as the bearer login does, then start a session."""
        user = self._auth.verify_credentials(email, password)
        return self.start(user)

    def start(self, user: User) -> IssuedSession:
        """Open a new session family for *user* and commit it."""
        now = dt.datetime.now(dt.UTC)
        family = uuid.uuid4()
        secret = new_refresh_secret()
        self._sessions.delete_expired_for_user(user.id, now)
        self._sessions.add(
            user_id=user.id,
            family_id=family,
            token_hash=hash_refresh_secret(secret),
            expires_at=now + self._refresh_ttl,
        )
        self._session.commit()
        return self._issued(user, family, secret)

    def refresh(self, secret: str | None, presented_csrf: str | None) -> IssuedSession:
        """Exchange a refresh token for the next, and a fresh access token; or raise.

        401 for a token that is unknown, revoked, expired, replayed, or whose account is
        disabled or has changed its password since; 403 for a missing or wrong CSRF token --
        checked before anything is consumed, so a forged request cannot burn a session.
        """
        if not secret:
            raise InvalidTokenError
        row = self._sessions.lock_by_hash(hash_refresh_secret(secret))
        if row is None:
            self._session.rollback()
            raise InvalidTokenError
        if not csrf_matches(self._settings, row.family_id, presented_csrf):
            self._session.rollback()
            raise CsrfError

        now = dt.datetime.now(dt.UTC)
        if row.revoked_at is not None or row.expires_at <= now:
            self._session.rollback()
            raise InvalidTokenError
        if row.used_at is not None:
            # A replay. The owner and a thief both hold this token; end the session for both.
            self._sessions.revoke_family(row.family_id, now)
            self._session.commit()
            raise InvalidTokenError

        user = self._session.get(User, row.user_id)
        if user is None or not user.is_active or row.created_at <= user.password_changed_at:
            self._sessions.revoke_family(row.family_id, now)
            self._session.commit()
            raise InvalidTokenError

        row.used_at = now
        secret_next = new_refresh_secret()
        self._sessions.add(
            user_id=user.id,
            family_id=row.family_id,
            token_hash=hash_refresh_secret(secret_next),
            expires_at=now + self._refresh_ttl,
        )
        self._session.commit()
        return self._issued(user, row.family_id, secret_next)

    def sign_out(self, secret: str | None, presented_csrf: str | None) -> None:
        """End the session the refresh cookie names, if any. Idempotent.

        No cookie, or one naming nothing, is simply nothing to end. A cookie that names a session
        needs its CSRF token, so another site cannot sign a user out by riding the cookie.
        """
        if not secret:
            return
        row = self._sessions.lock_by_hash(hash_refresh_secret(secret))
        if row is None:
            self._session.rollback()
            return
        if not csrf_matches(self._settings, row.family_id, presented_csrf):
            self._session.rollback()
            raise CsrfError
        self._sessions.revoke_family(row.family_id, dt.datetime.now(dt.UTC))
        self._session.commit()

    def _issued(self, user: User, family: uuid.UUID, secret: str) -> IssuedSession:
        access = self._auth.issue_session_token(user, family)
        return IssuedSession(
            user=self._auth.to_response(user),
            access_token=access.access_token,
            access_expires_in=access.expires_in,
            refresh_secret=secret,
            refresh_expires_in=int(self._refresh_ttl.total_seconds()),
            csrf_token=csrf_token(self._settings, family),
        )


__all__ = ["IssuedSession", "SessionService"]
