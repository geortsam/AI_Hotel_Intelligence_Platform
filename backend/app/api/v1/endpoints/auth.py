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

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import (
    AuthServiceDep,
    CurrentUserDep,
    SessionServiceDep,
    change_password_rate_limit,
    login_rate_limit,
)
from app.api.session_cookies import (
    ACCESS_COOKIE,
    CSRF_HEADER,
    REFRESH_COOKIE,
    clear_session_cookies,
    set_session_cookies,
)
from app.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    RegistrationRequest,
    SessionResponse,
    TokenResponse,
    UserResponse,
)
from app.schemas.common import ErrorResponse
from app.services.session import IssuedSession

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
    payload: PasswordChangeRequest,
    current_user: CurrentUserDep,
    service: AuthServiceDep,
    sessions: SessionServiceDep,
    request: Request,
    response: Response,
) -> TokenResponse:
    """Whose password changes is decided by the token, never by the body.

    Returns the same ``TokenResponse`` login returns -- the one token shape this API has, not
    a second one invented for this route. 200 rather than 201: a password change creates no
    resource, and the token is a credential rather than a thing that now exists at a URL.

    A wrong current password is the same 401 login gives, for the same reason: the only
    correct answer to a rejected credential is one that describes nothing about it.

    **A cookie session continues in a new one (R1.1).** The change ends every session of the
    account, the caller's included; a caller who came by cookie gets a fresh session's cookies
    with the response, as a bearer caller gets the replacement token in the body.
    """
    replacement = service.change_password(current_user, payload)
    if request.headers.get("Authorization") is None and request.cookies.get(ACCESS_COOKIE):
        set_session_cookies(response, sessions.start(current_user))
    return replacement


# --- the browser's cookie session (R1.1) ---------------------------------------------------------

CSRF_REJECTED_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "`CSRF_FAILED`: the session's cookie was sent without its CSRF token in "
        "`X-CSRF-Token`. Nothing was changed.",
    }
}


def _session_response(issued: IssuedSession, response: Response) -> SessionResponse:
    set_session_cookies(response, issued)
    return SessionResponse(
        user=issued.user,
        csrf_token=issued.csrf_token,
        expires_in=issued.access_expires_in,
        refresh_expires_in=issued.refresh_expires_in,
    )


@router.post(
    "/session",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Sign in with a cookie session",
    description="The browser's sign-in. Checks the credentials exactly as `POST /auth/login` "
    "does, then sets three cookies -- the access and refresh tokens `HttpOnly`, the CSRF token "
    "readable -- all `Secure; SameSite=Strict`. No token appears in the body. Every later "
    "request that changes something must echo the CSRF token in `X-CSRF-Token`. Rate limited "
    "with the login, per client address.",
    responses={**UNAUTHENTICATED_RESPONSE, **RATE_LIMITED_RESPONSE},
    dependencies=[Depends(login_rate_limit)],
)
def start_session(
    payload: LoginRequest, sessions: SessionServiceDep, response: Response
) -> SessionResponse:
    """One 401 for every rejection, as at `/auth/login`."""
    return _session_response(sessions.sign_in(payload.email, payload.password), response)


@router.post(
    "/session/refresh",
    response_model=SessionResponse,
    summary="Renew a cookie session",
    description="Exchanges the refresh cookie for the next one and a fresh access cookie. Needs "
    "`X-CSRF-Token`. A refresh token works once: presenting a used one ends the whole session, "
    "because two parties hold it. 401 when the session has ended, expired or been replayed, or "
    "the account was disabled or changed its password.",
    responses={**UNAUTHENTICATED_RESPONSE, **CSRF_REJECTED_RESPONSE},
)
def refresh_session(
    request: Request, sessions: SessionServiceDep, response: Response
) -> SessionResponse:
    issued = sessions.refresh(request.cookies.get(REFRESH_COOKIE), request.headers.get(CSRF_HEADER))
    return _session_response(issued, response)


@router.post(
    "/session/end",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of a cookie session",
    description="Ends the session the refresh cookie names -- its access tokens stop at once -- "
    "and clears the three cookies. Idempotent: with no session there is nothing to end, and it "
    "still answers 204. A cookie that names a session needs `X-CSRF-Token`.",
    responses=CSRF_REJECTED_RESPONSE,
)
def end_session(request: Request, sessions: SessionServiceDep) -> Response:
    """A POST under the session's own path rather than a DELETE: the refresh cookie is sent
    only to ``/api/v1/auth/session``, and this API offers no DELETE on a collection."""
    sessions.sign_out(request.cookies.get(REFRESH_COOKIE), request.headers.get(CSRF_HEADER))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookies(response)
    return response


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
