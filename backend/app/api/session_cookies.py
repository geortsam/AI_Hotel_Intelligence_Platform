"""The three cookies of a browser session, and the header that echoes one (R1.1).

Every cookie is ``Secure`` and ``SameSite=Strict``: sent only over HTTPS -- browsers treat
``http://localhost`` as secure for this -- and never on a request another site starts.

* ``__Host-ahip_access`` -- the access token. ``HttpOnly``: script cannot read it. ``__Host-``
  makes the browser refuse it unless it is ``Secure``, has ``Path=/`` and names no domain, so a
  sibling subdomain cannot plant or shadow it.
* ``__Secure-ahip_refresh`` -- the refresh secret. ``HttpOnly``, and sent only to the session
  routes (``Path=/api/v1/auth/session``), so no other request carries it. ``__Secure-`` rather
  than ``__Host-``, which would forbid the narrower path.
* ``__Host-ahip_csrf`` -- the CSRF token. Deliberately **not** ``HttpOnly``: the page reads it and
  sends it back as ``X-CSRF-Token``, which another site can do neither of.

The access and CSRF cookies live as long as the session, not the access token: an expired
access cookie is what makes the page refresh, and the refresh needs the CSRF token.
"""

from __future__ import annotations

from typing import Final

from fastapi import Response

from app.services.session import IssuedSession

ACCESS_COOKIE: Final = "__Host-ahip_access"
REFRESH_COOKIE: Final = "__Secure-ahip_refresh"
CSRF_COOKIE: Final = "__Host-ahip_csrf"
CSRF_HEADER: Final = "X-CSRF-Token"
#: The routes the refresh cookie is sent to: the session resource and its refresh.
REFRESH_PATH: Final = "/api/v1/auth/session"
#: Methods that change nothing, and therefore need no CSRF token (RFC 9110 safe methods).
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def set_session_cookies(response: Response, issued: IssuedSession) -> None:
    """Put a started or refreshed session into the response's cookies.

    The CSRF token also goes in the ``X-CSRF-Token`` response header, so the page can pick up
    a new session's token from any response that starts one -- a password change included --
    without parsing cookies.
    """
    lifetime = issued.refresh_expires_in
    response.headers[CSRF_HEADER] = issued.csrf_token
    response.set_cookie(
        ACCESS_COOKIE,
        issued.access_token,
        max_age=lifetime,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_secret,
        max_age=lifetime,
        path=REFRESH_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        issued.csrf_token,
        max_age=lifetime,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def clear_session_cookies(response: Response) -> None:
    """Expire all three, with the attributes they were set with, so the browser matches them."""
    for name, path, httponly in (
        (ACCESS_COOKIE, "/", True),
        (REFRESH_COOKIE, REFRESH_PATH, True),
        (CSRF_COOKIE, "/", False),
    ):
        response.delete_cookie(name, path=path, secure=True, httponly=httponly, samesite="strict")


__all__ = [
    "ACCESS_COOKIE",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "REFRESH_COOKIE",
    "REFRESH_PATH",
    "SAFE_METHODS",
    "clear_session_cookies",
    "set_session_cookies",
]
