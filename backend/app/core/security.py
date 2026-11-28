"""Password hashing and access tokens.

Pure cryptographic utility: no database, no HTTP, no domain rules. It turns a password into a
digest and a user identity into a signed token, and back again. Everything about *who* may do
*what* lives above it.

**Passwords.** Argon2id, via ``argon2-cffi``'s ``PasswordHasher`` defaults (t=3, m=64 MiB,
p=4) -- the Password Hashing Competition winner and the algorithm the OWASP cheat sheet
recommends first. Each hash carries its own random salt, so hashing the same password twice
produces different digests; that is a property the tests assert rather than an implementation
detail.

**Tokens.** HS256 JWTs, short-lived. The decode path pins ``algorithms=["HS256"]``
explicitly: without that pin a token whose header says ``{"alg": "none"}`` -- or ``HS256``
where the server expected RS256 -- can be made to verify. Algorithm confusion is the classic
JWT vulnerability and the pin is the entire defence.

**The signing secret comes from configuration and has no fallback.** A hard-coded default is
worse than a missing one: it fails silently, in production, with a key that is in the
repository. :func:`require_secret` raises instead.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings

#: The only algorithm this application will sign or accept.
ALGORITHM: Final = "HS256"
#: Marks an access token, so a future refresh token cannot be replayed as one.
TOKEN_TYPE_ACCESS: Final = "access"
#: Short by design. A stolen access token is only useful while it lives. A bearer token is
#: revoked by a password change; a cookie session's token also by ending its session (R1.1).
ACCESS_TOKEN_TTL: Final = dt.timedelta(minutes=30)
#: R1.1. The entropy of a refresh secret, in bytes: 256 bits, so it cannot be guessed and is
#: stored only as a SHA-256 digest -- a slow hash buys nothing against a random secret.
REFRESH_SECRET_BYTES: Final = 32
#: The HMAC domain of a CSRF token, so the same key can never yield a CSRF token that is also
#: some other value this application computes.
_CSRF_DOMAIN: Final = b"ahip.csrf.v1:"

#: One hasher, reused. Constructing a PasswordHasher per call is pure overhead, and its
#: defaults are the tuned Argon2id parameters we want.
_hasher = PasswordHasher()


class SecretNotConfiguredError(RuntimeError):
    """The signing secret is missing.

    Deliberately not an :class:`~app.core.errors.AppError`: this is a deployment fault, not
    something a client did, and it must never be rendered into an API response.
    """


class TokenError(Exception):
    """A token could not be trusted.

    Carries no detail about *why*. The caller turns every instance into one identical 401,
    so an attacker cannot distinguish "expired" from "bad signature" from "malformed".
    """


def hash_password(password: str) -> str:
    """Return an Argon2id digest of *password*.

    The digest embeds the algorithm, its parameters and a fresh random salt, so it is
    self-describing and the same input never produces the same output twice.
    """
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check *password* against *password_hash*, returning False rather than raising.

    Every failure mode collapses to False on purpose -- a mismatch, a corrupted digest and a
    hash produced by some other algorithm are all simply "no". Distinguishing them would hand
    an attacker information about the stored value.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def require_secret(settings: Settings) -> str:
    """The signing secret, or a refusal to run without one.

    There is no fallback and there must never be one: a default secret in source control
    means every deployment that forgets to configure one shares a key an attacker can read.
    Failing loudly at the first token operation is the safe behaviour.
    """
    secret = settings.secret_key
    if not secret or not secret.strip():
        raise SecretNotConfiguredError(
            "SECRET_KEY is not configured. Authentication cannot sign or verify tokens "
            "without it, and this application will not fall back to a built-in default."
        )
    return secret


def create_access_token(
    settings: Settings,
    subject: uuid.UUID,
    *,
    now: dt.datetime | None = None,
    expires_in: dt.timedelta = ACCESS_TOKEN_TTL,
    session_id: uuid.UUID | None = None,
) -> str:
    """Sign a short-lived access token for *subject*.

    The claims are the minimum that authentication needs:

    ``sub``  the user's ``public_id`` -- never the internal BIGINT, never the email
    ``iat``  issued-at
    ``exp``  expiry
    ``jti``  a unique token id, so a future revocation list has something to name
    ``typ``  ``access``
    ``sid``  only for a cookie session (R1.1): the ``auth_sessions`` family it belongs to, so
             ending the session ends the token too, and the key its CSRF token derives from

    Nothing else. No email, no name, no permissions, no hotel scope, no mutable state. A JWT
    is signed but **not encrypted** -- anyone holding one can read every claim -- and a claim
    that duplicates application state is stale the moment the state changes.
    """
    issued_at = now or dt.datetime.now(dt.UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + expires_in).timestamp()),
        "jti": str(uuid.uuid4()),
        "typ": TOKEN_TYPE_ACCESS,
    }
    if session_id is not None:
        payload["sid"] = str(session_id)
    return jwt.encode(payload, require_secret(settings), algorithm=ALGORITHM)


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """The two claims the application reads back out of a verified token.

    ``issued_at`` is returned alongside the subject from Stage 4.5.2 so the service can ask
    whether the token predates the credential it authenticates. It is a claim that was
    already there; nothing was added to the token to support revocation.

    ``session_id`` is the ``sid`` of a cookie session's token (R1.1), ``None`` for a bearer
    token from ``POST /auth/login``.
    """

    subject: uuid.UUID
    issued_at: int
    session_id: uuid.UUID | None = None


def decode_access_token(settings: Settings, token: str) -> AccessTokenClaims:
    """Verify *token* and return the claims the application reads, or raise
    :class:`TokenError`.

    ``algorithms=[ALGORITHM]`` is the load-bearing argument. PyJWT will otherwise trust the
    token's own ``alg`` header, which lets an attacker downgrade to ``none`` or swap a
    signature scheme. Expiry is verified by PyJWT; ``sub`` and ``typ`` are checked here
    because a missing subject or a non-access token must not authenticate anyone.

    No ``leeway``: every token this application issues carries a truthful ``iat`` that has
    already happened, so tolerating a future one would only widen what a forged token could
    claim. See ``AuthService._issue_token`` for how that is guaranteed.
    """
    try:
        claims = jwt.decode(
            token,
            require_secret(settings),
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        # Every reason -- expired, bad signature, malformed, wrong algorithm -- becomes the
        # same failure. The distinction is useful only to an attacker.
        raise TokenError from exc

    if claims.get("typ") != TOKEN_TYPE_ACCESS:
        raise TokenError

    try:
        sid = claims.get("sid")
        return AccessTokenClaims(
            subject=uuid.UUID(str(claims["sub"])),
            issued_at=int(claims["iat"]),
            session_id=None if sid is None else uuid.UUID(str(sid)),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenError from exc


# --- cookie sessions (R1.1) ----------------------------------------------------------------------


def new_refresh_secret() -> str:
    """A fresh refresh secret: 256 random bits, URL-safe, for the ``HttpOnly`` refresh cookie."""
    return secrets.token_urlsafe(REFRESH_SECRET_BYTES)


def hash_refresh_secret(secret: str) -> str:
    """The SHA-256, lower-case hex, that ``auth_sessions.token_hash`` stores for *secret*."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def csrf_token(settings: Settings, session_id: uuid.UUID) -> str:
    """The CSRF token of one cookie session: an HMAC of its ``sid`` under the signing secret.

    Derived, not stored: the server recomputes it from the access token's ``sid`` and needs no
    lookup, and another site can neither read it (the cookie is same-site, and the response
    carrying it is same-origin) nor compute it (it has no key).
    """
    key = require_secret(settings).encode("utf-8")
    return hmac.new(key, _CSRF_DOMAIN + str(session_id).encode("ascii"), "sha256").hexdigest()


def csrf_matches(settings: Settings, session_id: uuid.UUID, presented: str | None) -> bool:
    """Whether *presented* is this session's CSRF token, compared in constant time."""
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), csrf_token(settings, session_id).encode())


__all__ = [
    "ACCESS_TOKEN_TTL",
    "ALGORITHM",
    "REFRESH_SECRET_BYTES",
    "TOKEN_TYPE_ACCESS",
    "AccessTokenClaims",
    "SecretNotConfiguredError",
    "TokenError",
    "create_access_token",
    "csrf_matches",
    "csrf_token",
    "decode_access_token",
    "hash_password",
    "hash_refresh_secret",
    "new_refresh_secret",
    "require_secret",
    "verify_password",
]
