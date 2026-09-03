"""Authentication endpoints.

``/api/v1/auth/...``. Not hotel-scoped, and correctly so: a user is not owned by a property.
This is the first collection in the codebase outside the hotel hierarchy that is not a global
lookup table, and the reason is that identity precedes tenancy.

**These three routes are the only authenticated surface in the application.** Stage 4.1
establishes *who* a caller is; it does not protect the domain endpoints, and every hotel,
booking, payment and analytics route remains open exactly as before. Attaching
``CurrentUserDep`` to them is Stage 4.2's work, together with deciding which hotels a user
may reach.

HTTP concerns only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status

from app.api.deps import (
    AuthServiceDep,
    CurrentUserDep,
    change_password_rate_limit,
    login_rate_limit,
)
from app.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    RegistrationRequest,
    TokenResponse,
    UserResponse,
)
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/auth", tags=["authentication"])

RATE_LIMITED_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": ErrorResponse,
        "description": (
            "Too many requests from this client address. Carries `Retry-After` in seconds. "
            "The response is identical whatever was attempted and for whichever account."
        ),
    }
}

UNAUTHENTICATED_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": (
            "Authentication failed. The same response is returned whether the address is "
            "unknown, the password is wrong, or the account is disabled."
        ),
    }
}


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    description="The password is hashed with Argon2id before it reaches the database and is "
    "never returned, logged or stored in any other form.",
    responses={
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "An account with this email address already exists.",
        }
    },
)
def register(payload: RegistrationRequest, service: AuthServiceDep) -> UserResponse:
    """201 on success; 409 when the address is taken -- decided by ``uq_users_email``, not by
    a prior lookup."""
    return service.register(payload)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for an access token",
    description="Returns a short-lived HS256 bearer token. Present it as "
    "`Authorization: Bearer <token>`. Rate limited per client address; see the 429 response.",
    responses={**UNAUTHENTICATED_RESPONSE, **RATE_LIMITED_RESPONSE},
    # Stage 4.5.3. The brute-force surface of the whole application: the only endpoint that
    # accepts a password from an unauthenticated caller. Keyed by client address rather than
    # by email -- a per-account limit would say which addresses have accounts, and would let
    # anyone lock out a person they can name.
    dependencies=[Depends(login_rate_limit)],
)
def login(payload: LoginRequest, service: AuthServiceDep) -> TokenResponse:
    """One 401 for every rejection.

    An unknown address, a wrong password and a disabled account are externally identical --
    and cost the same to evaluate, so the timing does not distinguish them either.
    """
    return service.authenticate(payload.email, payload.password)


@router.post(
    "/change-password",
    response_model=TokenResponse,
    summary="Change the authenticated user's own password",
    description="Requires the current password as well as a valid token: a token alone is "
    "not authority to replace the credential it was minted from. **Every token issued before "
    "the change is revoked, including the one used to make the request** -- the replacement "
    "returned here is the caller's way to continue without signing in again.",
    responses={**UNAUTHENTICATED_RESPONSE, **RATE_LIMITED_RESPONSE},
    # Stage 4.5.3. Flood protection, NOT lockout: this route already demands a valid token
    # AND the current password, so it is not a guessing surface. What the limit bounds is how
    # often one source can make the server spend Argon2id's 64 MiB.
    dependencies=[Depends(change_password_rate_limit)],
)
def change_password(
    payload: PasswordChangeRequest, current_user: CurrentUserDep, service: AuthServiceDep
) -> TokenResponse:
    """Whose password changes is decided by the token, never by the body.

    Returns the same ``TokenResponse`` login returns -- the one token shape this API has, not
    a second one invented for this route. 200 rather than 201: a password change creates no
    resource, and the token is a credential rather than a thing that now exists at a URL.

    A wrong current password is the same 401 login gives, for the same reason: the only
    correct answer to a rejected credential is one that describes nothing about it.
    """
    return service.change_password(current_user, payload)


@router.get(
    "/me",
    response_model=UserResponse,
    summary="The authenticated identity",
    description="Requires a valid bearer token. Returns who the caller is -- not what they "
    "may access, which no endpoint decides yet.",
    responses=UNAUTHENTICATED_RESPONSE,
)
def read_current_user(current_user: CurrentUserDep) -> UserResponse:
    """The only endpoint that requires authentication in this stage.

    The user is re-read from the database on every request rather than reconstructed from
    token claims, so a disabled account stops working immediately.
    """
    return UserResponse(
        public_id=current_user.public_id,
        email=current_user.email,
        full_name=current_user.full_name,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
        last_login_at=current_user.last_login_at,
    )
