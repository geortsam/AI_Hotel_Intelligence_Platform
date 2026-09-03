"""Error foundation: one domain exception hierarchy, one response envelope.

Two rules govern this module.

**One shape.** Every failure -- validation, domain, database, unexpected -- leaves the API as
:class:`~app.schemas.common.ErrorResponse`. A client parses one structure, never several.

**Nothing leaks.** Stack traces, SQL text, driver messages, connection strings, credentials and
filesystem paths are *logged* at full fidelity and *never* returned. The boundary between what
an operator may see and what a client may see is enforced here, in one place, rather than being
re-decided at every endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_id import REQUEST_ID_HEADER
from app.schemas.common import ErrorBody, ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

#: Returned instead of the real reason whenever the real reason is not the client's business.
GENERIC_SERVER_MESSAGE = "An internal error occurred. The incident has been logged."
GENERIC_DATABASE_MESSAGE = "The service is temporarily unable to reach its database."

# --- PostgreSQL SQLSTATE vocabulary ------------------------------------------------------
#
# Shared by every service that has to turn an IntegrityError into a domain error. Defined
# once because the distinction below is genuinely easy to get wrong -- it was, in Stage
# 3B.1, and cost a debugging round.

SQLSTATE_UNIQUE_VIOLATION = "23505"
SQLSTATE_CHECK_VIOLATION = "23514"

#: not_null_violation. Deliberately NOT a member of SQLSTATE_DEPENDENCY_VIOLATIONS below:
#: on an INSERT it means a required column was omitted, which is the client's fault and a
#: different conversation entirely. It becomes a dependency signal only in the narrow case
#: documented in GuestService.delete, where a composite ON DELETE SET NULL cannot fire.
SQLSTATE_NOT_NULL_VIOLATION = "23502"

#: Two distinct codes both mean "dependent rows still exist":
#:
#:   23503 foreign_key_violation -- the generic case (NO ACTION, or a child whose parent is
#:                                  absent)
#:   23001 restrict_violation    -- raised specifically when an explicit ON DELETE RESTRICT
#:                                  refuses the delete
#:
#: This schema declares RESTRICT throughout, so a refused delete arrives as 23001. Checking
#: only 23503 falls silently through to a generic message.
SQLSTATE_DEPENDENCY_VIOLATIONS = frozenset({"23503", "23001"})


def sqlstate_of(error: Exception) -> str | None:
    """Extract the PostgreSQL SQLSTATE from a driver error, if there is one.

    psycopg 3 exposes it as ``.sqlstate`` on the wrapped exception reachable via
    SQLAlchemy's ``.orig``. Returns None for anything else, so callers fall back to their
    generic branch rather than crashing on an unexpected exception type.
    """
    return getattr(getattr(error, "orig", None), "sqlstate", None)


class AppError(Exception):
    """Base class for errors the API is willing to describe to a client.

    Anything raised as an ``AppError`` has been consciously written for external eyes. Every
    other exception is treated as internal and reported generically.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "APP_ERROR"
    message: str = GENERIC_SERVER_MESSAGE

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.message = message or type(self).message
        self.code = code or type(self).code
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    message = "The requested resource does not exist."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    message = "The request conflicts with the current state of the resource."


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "VALIDATION_ERROR"
    message = "The request payload is invalid."


class AuthenticationError(AppError):
    """Credentials were not accepted.

    401 rather than 403: the request was not authenticated, as opposed to authenticated and
    refused -- and 403 is a distinction Stage 4.1 cannot make, because it knows nothing about
    permissions.

    **One message for every cause.** An unknown address, a wrong password and a disabled
    account all produce this, unchanged. Anything else turns the login endpoint into an
    oracle: a distinct response for an unknown address enumerates which of a leaked address
    list have accounts here.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "AUTHENTICATION_FAILED"
    message = "Invalid email or password."


class InvalidTokenError(AppError):
    """A bearer token was missing, malformed, expired or not trustworthy.

    One message for all four, for the same reason: which one it was is useful only to someone
    probing the endpoint.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "INVALID_TOKEN"
    message = "Not authenticated."


class ForbiddenError(AppError):
    """The caller is authenticated and is a member, but their role is too low.

    403 rather than 404 -- and the distinction is the whole 4.2 policy. A non-member gets the
    hotel's own 404, because telling them the property exists would be an existence oracle.
    Once membership IS established the hotel is no longer a secret, so the honest answer is
    more useful than a misleading one.
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"
    message = "You do not have permission to perform this operation."


class RateLimitExceededError(AppError):
    """Too many requests from one source, too quickly.

    **The message is the same whatever was being attempted and whoever was attempting it.**
    A 429 that varied with the account -- "this account is locked", or a different wording for
    a known address -- would turn the limiter into the enumeration oracle that keying it by IP
    rather than by email was meant to avoid.

    ``retry_after`` is the honest number of seconds until the current window ends, not a
    guess: the limiter knows when the window it counted against began.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"
    message = "Too many requests. Please wait before trying again."

    def __init__(self, retry_after: int) -> None:
        super().__init__()
        self.retry_after = retry_after


class DatabaseUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "DATABASE_UNAVAILABLE"
    message = GENERIC_DATABASE_MESSAGE


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
) -> JSONResponse:
    """Render the one and only error envelope."""
    payload = ErrorResponse(error=ErrorBody(code=code, message=message, details=details or []))
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))


def _safe_details(raw_errors: list[Any]) -> list[ErrorDetail]:
    """Reduce Pydantic's error list to the three fields that are safe to return.

    Pydantic includes the offending ``input`` value and a documentation ``url``. The input is
    dropped deliberately: a rejected payload may contain a password or token, and echoing it
    back would write that secret into client logs and error trackers.
    """
    details: list[ErrorDetail] = []
    for err in raw_errors:
        details.append(
            ErrorDetail(
                location=[str(part) for part in err.get("loc", ())],
                message=str(err.get("msg", "Invalid value.")),
                type=str(err.get("type", "invalid")),
            )
        )
    return details


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler. Called once by the application factory."""

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        """Deliberate, client-facing errors are reported as written."""
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RateLimitExceededError)
    async def handle_rate_limited(_: Request, exc: RateLimitExceededError) -> JSONResponse:
        """The same envelope as every other refusal, plus the one header a client can act on.

        Registered separately from the generic ``AppError`` handler only because of that
        header; Starlette resolves the most specific handler, so this wins for this subclass
        and nothing else changes.
        """
        response = error_response(exc.status_code, exc.code, exc.message)
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        """The client's own input is at fault, so field-level detail is useful and safe."""
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "VALIDATION_ERROR",
            "The request payload is invalid.",
            _safe_details(list(exc.errors())),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        """404s and friends, wrapped in the same envelope as everything else."""
        code = {
            status.HTTP_404_NOT_FOUND: "NOT_FOUND",
            status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
        }.get(exc.status_code, "HTTP_ERROR")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        """Database faults: logged in full, reported as a fixed sentence.

        A raw SQLAlchemy message routinely contains the failing SQL, parameter values and the
        connection URL. None of that may cross the API boundary.
        """
        logger.exception(
            "Database error handling %s %s", request.method, request.url.path, exc_info=exc
        )
        return error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DATABASE_UNAVAILABLE",
            GENERIC_DATABASE_MESSAGE,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """The catch-all. Full traceback to the logs, one flat sentence to the client.

        This is the ONE handler that has to set ``X-Request-ID`` itself. Starlette routes the
        ``Exception`` handler to ServerErrorMiddleware, which wraps every user middleware --
        so this response is produced outside ``RequestIdMiddleware`` and never passes back
        through its ``send``. The other four handlers run inside it and need nothing.

        The id comes from the ASGI scope rather than the ContextVar: by the time an exception
        has propagated out this far, the middleware's ``finally`` has already unbound it.
        """
        request_id = request.scope.get("state", {}).get("request_id")
        logger.exception(
            "Unhandled error handling %s %s", request.method, request.url.path, exc_info=exc
        )
        response = error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "INTERNAL_ERROR",
            GENERIC_SERVER_MESSAGE,
        )
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        return response


__all__ = [
    "AppError",
    "ConflictError",
    "DatabaseUnavailableError",
    "NotFoundError",
    "ValidationError",
    "error_response",
    "register_exception_handlers",
]
