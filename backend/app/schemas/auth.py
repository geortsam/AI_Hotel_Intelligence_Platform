"""Authentication API contracts.

**No schema here exposes ``password_hash``**, and a structural test asserts it across every
generated OpenAPI component. The plaintext ``password`` appears in exactly two request
models and in no response model.

``UserResponse`` is built from explicit fields rather than ``from_attributes`` over the ORM
row, so adding a column to ``users`` later cannot silently start returning it -- which is how
a credential ends up in a payload.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

#: Mirrors ck_users_email_format. Validated at the edge so a malformed address is a 422 that
#: names the field, rather than a 409 from a constraint the client cannot see.
EmailField = Annotated[
    str, Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
]
#: Twelve characters is the floor, not a policy. Length dominates every other password rule,
#: and composition requirements push people towards predictable substitutions. The upper
#: bound exists because Argon2id hashes whatever it is given and an unbounded body is a
#: denial-of-service parameter, not because long passwords are a problem.
PasswordField = Annotated[str, Field(min_length=12, max_length=256)]
NameField = Annotated[str, Field(min_length=1, max_length=200)]


class RegistrationRequest(BaseModel):
    """Payload for creating an account.

    ``is_active`` is absent: a client must not be able to create an account in any state but
    the default. So is anything hotel-shaped -- membership is Stage 4.2's.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: EmailField
    password: PasswordField
    full_name: NameField


class LoginRequest(BaseModel):
    """Payload for exchanging credentials for a token.

    ``password`` has no length floor here on purpose. Rejecting a short password at login
    would tell an attacker their guess was too short to be this account's password, and the
    only correct answer to any wrong credential is the same one.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: Annotated[str, Field(min_length=1, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class TokenResponse(BaseModel):
    """A freshly issued access token.

    ``token_type`` is the RFC 6750 bearer scheme, spelled as clients expect in the
    ``Authorization`` header. ``expires_in`` is seconds, so a client can schedule a refresh
    without parsing the token -- which it should not do, since the token is ours to change.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    """The authenticated identity.

    Field-by-field rather than ``from_attributes``: ``password_hash`` is not merely omitted
    here, it is unreachable, and a column added to ``users`` in a later stage cannot leak
    through by default.
    """

    public_id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    created_at: dt.datetime
    last_login_at: dt.datetime | None


class PasswordChangeRequest(BaseModel):
    """Payload for changing the authenticated caller's own password.

    **It names no account.** Whose password changes is decided by the bearer token, not by
    anything in this body -- ``extra="forbid"`` means a client that tries to add an ``email``,
    a ``public_id`` or a ``user_id`` gets a 422 rather than having it quietly ignored.

    ``current_password`` deliberately carries NO length floor, following ``LoginRequest``: it
    is a value being VERIFIED, and rejecting a short one would tell an attacker their guess
    was too short to be this account's password. ``new_password`` is a value being SET, so it
    gets the same ``PasswordField`` floor registration uses.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    current_password: Annotated[str, Field(min_length=1, max_length=256)]
    new_password: PasswordField


__all__ = [
    "EmailField",
    "LoginRequest",
    "NameField",
    "PasswordChangeRequest",
    "PasswordField",
    "RegistrationRequest",
    "TokenResponse",
    "UserResponse",
]
