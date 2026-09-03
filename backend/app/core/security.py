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
#: Short by design. A stolen access token is only useful while it lives, and there is no
#: revocation list in this stage -- the expiry *is* the revocation mechanism.
ACCESS_TOKEN_TTL: Final = dt.timedelta(minutes=30)

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
) -> str:
    """Sign a short-lived access token for *subject*.

    The claims are the minimum that authentication needs:

    ``sub``  the user's ``public_id`` -- never the internal BIGINT, never the email
    ``iat``  issued-at
    ``exp``  expiry
    ``jti``  a unique token id, so a future revocation list has something to name
    ``typ``  ``access``

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
    return jwt.encode(payload, require_secret(settings), algorithm=ALGORITHM)


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """The two claims the application reads back out of a verified token.

    ``issued_at`` is returned alongside the subject from Stage 4.5.2 so the service can ask
    whether the token predates the credential it authenticates. It is a claim that was
    already there; nothing was added to the token to support revocation.
    """

    subject: uuid.UUID
    issued_at: int


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
        return AccessTokenClaims(
            subject=uuid.UUID(str(claims["sub"])),
            issued_at=int(claims["iat"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenError from exc


__all__ = [
    "ACCESS_TOKEN_TTL",
    "ALGORITHM",
    "TOKEN_TYPE_ACCESS",
    "AccessTokenClaims",
    "SecretNotConfiguredError",
    "TokenError",
    "create_access_token",
    "decode_access_token",
    "hash_password",
    "require_secret",
    "verify_password",
]
