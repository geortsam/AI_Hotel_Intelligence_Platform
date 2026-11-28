"""Authentication workflow: register, authenticate, resolve a token's subject.

**This service establishes *who* someone is. It never decides what they may reach.** No
method takes a hotel, reads a role or consults a membership, and a structural test asserts
that -- authorization is Stage 4.2's, and the cheapest way to get it wrong is to start
sprinkling it through the layer below it.

**One indistinguishable failure.** An unknown email, a wrong password and a disabled account
all raise the same :class:`AuthenticationError` with the same message. Anything else turns
the login endpoint into an oracle: a different response for an unknown address enumerates
which of a leaked address list have accounts here, and a different response for a disabled
account tells an attacker they have found a real one.

**The password is verified even when the user does not exist.** Skipping the Argon2id
computation on a miss makes the response measurably faster, which is the same oracle by
another route. The dummy verification below costs what a real one costs.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    GENERIC_CONFLICT_MESSAGE,
    AuthenticationError,
    ConflictError,
    InvalidTokenError,
    constraint_name_of,
    internal_fault,
    is_audit_integrity_failure,
    sqlstate_of,
)
from app.core.security import (
    ACCESS_TOKEN_TTL,
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.models.enums import AuditAction, AuditResourceType
from app.models.user import User
from app.repositories.auth_session import AuthSessionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    PasswordChangeRequest,
    RegistrationRequest,
    TokenResponse,
    UserResponse,
)
from app.services.audit import AuditTrail

logger = logging.getLogger(__name__)

#: A real Argon2id digest of a value nobody can supply, used to spend the same time verifying
#: a password for an address that does not exist as for one that does. Computed once at
#: import; its plaintext is discarded immediately and appears nowhere.
_DUMMY_HASH = hash_password(uuid.uuid4().hex + uuid.uuid4().hex)

#: uq_users_email. Named so a duplicate registration can be reported precisely without
#: quoting the address back or naming the constraint in the response.
EMAIL_UNIQUE_CONSTRAINT = "uq_users_email"


class AuthService:
    """Registration, sign-in and token-subject resolution."""

    def __init__(
        self,
        session: Session,
        repository: UserRepository,
        settings: Settings,
        audit: AuditTrail,
        sessions: AuthSessionRepository,
    ) -> None:
        self._session = session
        self._repository = repository
        self._settings = settings
        # R1.1. Read on every request whose token names a cookie session (`sid`), and written
        # by a password change, which ends every sign-in of the account.
        self._sessions = sessions
        # Stage 4.5.12. Handed in WITHOUT an actor: this service is what resolves a token, so
        # the authenticated user cannot also be one of its dependencies. `change_password`
        # binds the caller it was given; see `AuditTrail.for_actor`.
        self._audit = audit

    # --- registration -------------------------------------------------------------------

    def register(self, payload: RegistrationRequest) -> UserResponse:
        """Create an account and commit.

        The password is hashed before the row is built, so the plaintext never reaches the
        ORM, the session, a query log or a driver error. No prior "is this email taken?"
        lookup: ``uq_users_email`` decides, which is both race-free and one round trip.
        """
        user = User(
            email=payload.email.lower(),
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
            # One second before now, rather than the column's `now()` default.
            #
            # The revocation rule kills every token whose `iat` falls in or before the
            # boundary's second, and `iat` has only second resolution -- so a boundary of
            # "now" would revoke the token issued by logging in moments later, making a new
            # account unusable until the clock ticked over. Backdating is safe precisely
            # here and nowhere else: an account being created has no outstanding tokens, so
            # a boundary in the past can revoke nothing.
            password_changed_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        try:
            created = self._repository.add(user)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self.to_response(created)

    # --- sign-in ------------------------------------------------------------------------

    def authenticate(self, email: str, password: str) -> TokenResponse:
        """Exchange credentials for a bearer access token, or raise the one failure."""
        return self._issue_token(self.verify_credentials(email, password))

    def verify_credentials(self, email: str, password: str) -> User:
        """The account these credentials sign in to, its login recorded and committed -- or the
        one failure. Shared by the bearer login and the cookie session (R1.1).

        Three rejections share a single path -- unknown address, wrong password, disabled
        account -- and each does the same amount of work, so neither the response nor the
        time taken distinguishes them.
        """
        user = self._repository.get_by_email(email.strip().lower())

        if user is None:
            # Spend the cost of a real verification against a throwaway digest, then fail.
            # Returning early here would make a miss noticeably faster than a hit.
            verify_password(password, _DUMMY_HASH)
            raise AuthenticationError

        if not verify_password(password, user.password_hash):
            raise AuthenticationError

        if not user.is_active:
            # Deliberately indistinguishable from a wrong password. A distinct response
            # would confirm the account exists.
            raise AuthenticationError

        try:
            self._repository.record_login(user, dt.datetime.now(dt.UTC))
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return user

    def change_password(self, user: User, payload: PasswordChangeRequest) -> TokenResponse:
        """Replace the authenticated caller's password, and commit.

        *user* comes from the bearer token by way of ``CurrentUserDep``; this method takes no
        identifier and therefore cannot be pointed at somebody else's account.

        **The current password is verified first, and its failure is login's failure.** A
        valid token is not sufficient authority to change the credential that token was
        minted from -- otherwise a stolen token becomes a permanent account takeover rather
        than a thirty-minute one.

        Neither password is logged, and neither reaches the repository: only the digest does.

        **Every token issued before this moment stops working**, including the one this
        request was made with: ``password_changed_at`` moves, and `resolve_token` refuses
        anything minted at or before that second. The replacement returned here is the
        caller's only way to keep going, which is why it is returned at all.
        """
        if not verify_password(payload.current_password, user.password_hash):
            raise AuthenticationError

        if not user.is_active:
            # Unreachable through the route -- `resolve_token` already refuses a disabled
            # account with a 401. Kept so the service is correct on its own terms, and so
            # that a disabled caller could never be told apart from a wrong password here.
            raise AuthenticationError

        # Hashed before the transaction opens, as `register` does: Argon2id is the expensive
        # part of this request and it has no business happening inside an open write.
        digest = hash_password(payload.new_password)
        try:
            changed_at = dt.datetime.now(dt.UTC)
            self._repository.set_password(user, digest, changed_at)
            # R1.1. Every cookie session of the account ends with it, in the same transaction:
            # a refresh token must not outlive the password it was signed in with, any more
            # than an access token does.
            self._sessions.revoke_all_for_user(user.id, changed_at)
            # Stage 4.5.12. The event carries no hotel: a password belongs to the ACCOUNT,
            # and a user may belong to no property or to several -- attributing the change to
            # one of them would be an invention. It carries no details at all, either. There
            # is nothing safe to say about a password change beyond that it happened, and
            # every field a reader might want here (old digest, new digest, strength, length)
            # is a fact about a credential that must not be written down.
            self._audit.for_actor(user).record(
                AuditAction.AUTH_PASSWORD_CHANGED,
                AuditResourceType.USER,
                str(user.public_id),
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        # AFTER the commit, never before. A token minted first and then handed back from a
        # transaction that rolled back would authenticate a password state that does not
        # exist -- and would do it with a token the caller believes is the new one.
        return self._issue_token(user)

    # --- request authentication ---------------------------------------------------------

    def resolve_token(self, token: str) -> User:
        """Turn a bearer token into the user it names, or raise.

        Read-only: authenticating a request must not write. The user is re-read on every
        request rather than trusted from the token, so an account disabled a minute ago stops
        working now instead of when its token happens to expire -- which is the whole reason
        the token carries no mutable state.
        """
        user, _ = self._resolve_claims(token)
        return user

    def resolve_session_token(self, token: str) -> tuple[User, uuid.UUID]:
        """The user and session a **cookie** access token names (R1.1), or raise.

        A cookie must carry a session's token: one without a ``sid`` -- a bearer token planted
        in the cookie -- is refused, because it has no CSRF token to check against.
        """
        user, session_id = self._resolve_claims(token)
        if session_id is None:
            raise InvalidTokenError
        return user, session_id

    def _resolve_claims(self, token: str) -> tuple[User, uuid.UUID | None]:
        """The user a token names, and its session if it has one; every check applied."""
        try:
            claims = decode_access_token(self._settings, token)
        except TokenError as exc:
            raise InvalidTokenError from exc

        user = self._repository.get_by_public_id(claims.subject)
        if user is None or not user.is_active:
            # A token for a deleted or disabled account is simply not authenticated.
            raise InvalidTokenError

        # Stage 4.5.2. `password_changed_at` arrived with the user above -- revocation costs
        # no extra query, which is what makes it affordable on every single request.
        #
        # `<=` rather than `<`, and against the FLOORED second: `iat` has one-second
        # resolution, so a token minted at 12:00:00.9 and a password changed at 12:00:00.1
        # are indistinguishable by it. Rejecting the whole second closes that window in the
        # safe direction, at the cost of also rejecting a token minted just before the
        # change within the same second -- a fresh login, not a security failure.
        if claims.issued_at <= int(user.password_changed_at.timestamp()):
            # Indistinguishable from every other token failure: which one it was is useful
            # only to somebody probing, and "your password changed" is a fact about the
            # account that an expired-token response must not confirm.
            raise InvalidTokenError

        # R1.1. A session's token lives only as long as its session: signed out, replayed or
        # expired, the family has no live refresh token and the access token stops with it.
        if claims.session_id is not None and not self._sessions.family_is_live(
            claims.session_id, dt.datetime.now(dt.UTC)
        ):
            raise InvalidTokenError

        return user, claims.session_id

    def issue_session_token(self, user: User, session_id: uuid.UUID) -> TokenResponse:
        """An access token for one cookie session, named by its ``sid`` (R1.1)."""
        return self._issue_token(user, session_id=session_id)

    # --- internals ----------------------------------------------------------------------

    def _issue_token(self, user: User, *, session_id: uuid.UUID | None = None) -> TokenResponse:
        """Mint an access token this application will actually accept, waiting if it must.

        The revocation rule rejects ``iat <= floor(password_changed_at)`` -- the whole second
        in which the password changed. A token minted inside that second would be born
        revoked, which is what the replacement returned by a password change would otherwise
        be.

        So when the clock has not yet left that second, this waits for it to. The wait is
        bounded by one second, happens only on the request that just changed a password, and
        buys two things worth more than it costs:

        * ``iat`` stays truthful -- no token is stamped with a time that has not happened, so
          ``decode_access_token`` needs no leeway and a forged future ``iat`` stays invalid;
        * the rule stays strictly stronger than ``iat < floor(...)``. An earlier draft pushed
          ``iat`` forward instead of waiting, and that quietly reopened the hole: a token
          stamped one second ahead survived a password change later in the same second, which
          is precisely the case ``<=`` exists to catch.

        For every ordinary request -- any account whose password changed more than a second
        ago -- the condition is false and this does nothing at all.
        """
        boundary = int(user.password_changed_at.timestamp()) + 1
        remaining = boundary - dt.datetime.now(dt.UTC).timestamp()
        if remaining > 0:
            time.sleep(remaining)

        return TokenResponse(
            access_token=create_access_token(self._settings, user.public_id, session_id=session_id),
            expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
        )

    @staticmethod
    def to_response(user: User) -> UserResponse:
        """The one place a User row becomes a response. ``password_hash`` is not copied, and
        the response model has no field that could hold it."""
        return UserResponse(
            public_id=user.public_id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )

    def _translate(self, exc: IntegrityError) -> Exception:
        """Turn an integrity error into a domain error, leaking nothing.

        No ``exc_info``: the driver's rendering of a failed user insert contains the email
        address and, on some errors, the whole row. The SQLSTATE and the constraint name are
        all that is recorded, and neither reaches the client.
        """
        state = sqlstate_of(exc)
        constraint = constraint_name_of(exc)
        logger.warning("User integrity error (sqlstate=%s, constraint=%s)", state, constraint)

        if is_audit_integrity_failure(exc):
            # Stage 4.5.16. The audit layer failed, not this domain -- so nothing below may
            # claim it. Before this guard, a booking deletion whose audit INSERT failed told
            # the client that payments still referenced the booking, which was false and
            # unactionable.
            return internal_fault(exc)

        if constraint == EMAIL_UNIQUE_CONSTRAINT:
            # The address is not quoted back. A registration form can say "this address is
            # already registered" because the person typing it already knows it; the API
            # says the same without repeating the value into logs or proxies.
            return ConflictError("An account with this email address already exists.")
        return ConflictError(GENERIC_CONFLICT_MESSAGE)


__all__ = ["EMAIL_UNIQUE_CONSTRAINT", "AuthService"]
