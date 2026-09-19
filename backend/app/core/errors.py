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
from app.core.security_headers import security_headers
from app.schemas.common import ErrorBody, ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

#: Returned instead of the real reason whenever the real reason is not the client's business.
GENERIC_SERVER_MESSAGE = "An internal error occurred. The incident has been logged."
GENERIC_DATABASE_MESSAGE = "The service is temporarily unable to reach its database."

#: What a service says when it cannot honestly account for an integrity failure.
#:
#: Every domain's ``_translate`` already ended with this sentence; it is named here so the
#: audit guard added in Stage 4.5.16 returns the SAME words as the fallback beneath it,
#: rather than a ninth copy of them that could drift.
GENERIC_CONFLICT_MESSAGE = "The request conflicts with the current state of the database."

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


#: Relations whose integrity failures are never the caller's fault.
#:
#: Stage 4.5.12 made six services write an audit event inside the transaction of the mutation
#: they describe. That is the right design -- the two commit or roll back together -- but it
#: means a failure in the AUDIT layer now surfaces inside a domain service's ``except
#: IntegrityError``, where it looks exactly like a domain conflict and, until Stage 4.5.16, was
#: reported as one. A client deleting a booking was told that payments still referenced it.
#:
#: A service may only describe a failure it can actually account for. When the failing relation
#: is one of these, it cannot, and says so generically.
AUDIT_RELATIONS = frozenset({"audit_events", "audit_events_archive"})


def constraint_name_of(error: Exception) -> str | None:
    """The violated constraint's NAME from the driver diagnostics, never its message.

    Declared once, here, for the same reason the SQLSTATE constants are: six services had
    identical private copies of this, and a per-service copy of a rule is a rule that drifts.

    The message is deliberately not consulted anywhere. psycopg renders the offending ROW into
    it -- a guest's details, an amount, a card fragment, an email address -- so the name and the
    SQLSTATE are the only two things this project ever reads from an integrity error.
    """
    diag = getattr(getattr(error, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None


def relation_of(error: Exception) -> str | None:
    """The table the integrity failure is ABOUT, from the driver diagnostics.

    **Not redundant with :func:`constraint_name_of`, and Stage 4.5.16 measured why.** A
    not-null violation carries no constraint name at all -- PostgreSQL reports the column and
    the table instead -- and two of the four ways a booking deletion can fail are exactly that:

        payments RESTRICT   -> 23001, constraint fk_payments_..., table payments
        revenue  SET NULL   -> 23502, constraint None,            table revenue
        reviews  SET NULL   -> 23502, constraint None,            table reviews
        audit actor FK      -> 23503, constraint fk_audit_...,    table audit_events

    The relation is the one discriminator present in all four. Attribution that relied on the
    constraint name alone would have to treat both SET NULL cases as unattributable.
    """
    diag = getattr(getattr(error, "orig", None), "diag", None)
    name = getattr(diag, "table_name", None)
    return str(name) if name else None


def is_audit_integrity_failure(error: Exception) -> bool:
    """Whether this integrity error came from the audit tables rather than the domain.

    The guard a service consults before blaming its own data for a failure. It is deliberately
    a question about the RELATION and not about the SQLSTATE: an audit write can fail as a
    foreign-key violation, a check violation or a unique violation, and each of those already
    means something specific in every domain that would otherwise claim it.
    """
    return relation_of(error) in AUDIT_RELATIONS


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


class InternalFaultError(AppError):
    """The server failed at something the client could not have caused and cannot fix.

    Stage 4.5.17. Every other :class:`AppError` describes a situation the caller can do
    something about: a resource that is not there, a role they lack, a value that collides with
    one already stored. This one is the opposite -- an invariant inside the server broke, and no
    change to the request would help.

    **Why it is not a ``ConflictError``.** Stage 4.5.16 stopped an audit-layer integrity failure
    being described as a booking dependency, which fixed a false statement but left a misleading
    one: a 409 tells a client "your request conflicts with stored data", inviting them to change
    the data and retry. For an audit-subsystem failure that advice is wrong in every particular.
    409 also puts a server fault in the 4xx class, where it is invisible to the monitoring that
    watches 5xx.

    **It carries the same code and sentence as any other 500**, deliberately. A client that
    could tell an audit failure from an unexpected exception would be learning something about
    the server's internals from an error response, which is the thing this module exists to
    prevent. The distinction is kept where it is useful -- in the logs, and in the type -- and
    discarded at the boundary.

    **It takes no message argument at its call sites.** The default is the only safe wording,
    and a per-site message would be exactly the place a constraint name or a table eventually
    got interpolated. A static test asserts none is passed.
    """

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "INTERNAL_ERROR"
    message = GENERIC_SERVER_MESSAGE


class DatabaseUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "DATABASE_UNAVAILABLE"
    message = GENERIC_DATABASE_MESSAGE


class ModelUnavailableError(AppError):
    """The approved demand model cannot be served right now (Stage 6.6).

    503 rather than 500, for the same reason :class:`DatabaseUnavailableError` is: the request
    was well-formed and the fault is a dependency the server could not reach rather than
    something the caller did. It is also the honest signal to an orchestrator -- this instance
    cannot answer this route, and a later attempt may.

    **One sentence for every cause**, exactly as ``AuthenticationError`` has one for four. The
    model may be unavailable because the ML runtime is not installed, because the artifact is
    absent, because its digest does not match its metadata, because it declares the wrong model
    or feature version, because it was built from a different dataset, because its claims have
    been edited, or because the deserialised estimator does not compute what the metadata says
    it computes. Which of those it was is an operator's question, answered in the log. A client
    able to tell them apart would be reading the server's deployment state off an error body.

    The message names no path, no filename, no version, no checksum and no library.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "MODEL_UNAVAILABLE"
    message = "The demand model is not available on this server."


class InsufficientHistoryError(AppError):
    """Too little recorded history to compute the model's features (Stage 6.6).

    422 rather than 404 or 503: the hotel exists, the server is healthy, and the request is
    understood -- it simply cannot be satisfied for this date, and a different date may well
    work. That makes it the caller's to act on, which is what puts it in the 4xx class.

    **No number is invented to fill the gap.** The alternative to this error is a prediction
    computed from a fabricated zero, and zero is a real demand value here: a hotel that sold
    nothing is not a hotel with no record. The training dataset dropped incomplete rows rather
    than imputing them, so imputing at serving time would score a row of a kind the model was
    never fitted on.

    The message names no feature, no table, no query and no row count.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "INSUFFICIENT_HISTORY"
    message = "There is not enough recorded demand history to forecast this date."


def internal_fault(error: Exception) -> InternalFaultError:
    """Classify an unattributable integrity failure, record it, and return what to raise.

    One place, so the log line and the classification cannot drift apart, and so six services
    do not each keep their own copy of a decision about what is safe to write down.

    **Logged at ERROR, with the SQLSTATE and the relation and nothing else.** Those two are
    what an operator needs to find the fault, and they are already the only two things this
    project reads from an integrity error. The request id arrives on the record automatically
    -- ``RequestIdFilter`` is attached to the root handler -- so the log line and the client's
    ``X-Request-ID`` header identify the same request without either being passed around.

    **No ``exc_info``, and this is the subtle part.** The error would be raised ``from`` the
    original ``IntegrityError``, so a traceback here would render the chained cause -- and
    psycopg puts the offending ROW in that message: a guest's name, an amount, a card fragment.
    ``test_no_service_logs_a_driver_exception`` forbids exactly this in services; the same
    reasoning applies to the helper they call, and a static test holds it here too.
    """
    logger.error(
        "Internal fault: an integrity failure could not be attributed to the request "
        "(sqlstate=%s, relation=%s)",
        sqlstate_of(error),
        relation_of(error),
    )
    return InternalFaultError()


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
        # Same reason, same place: this response never passes back through
        # SecurityHeadersMiddleware either.
        #
        # The scheme comes from the scope rather than through the trusted-proxy rule. The only
        # consequence is that HSTS is omitted from a 500 behind a TLS-terminating proxy, which
        # is the safe direction to be wrong in.
        #
        # `settings` is fetched defensively even though `create_app` always sets it before
        # registering handlers. This is the handler of last resort: if it raises, the client
        # gets a dropped connection instead of an envelope, so it must not be the place that
        # discovers a missing attribute.
        settings = getattr(request.app.state, "settings", None)
        if settings is not None:
            for name, value in security_headers(
                settings, request.scope.get("scheme", "http")
            ).items():
                response.headers[name] = value
        return response


__all__ = [
    "AUDIT_RELATIONS",
    "GENERIC_CONFLICT_MESSAGE",
    "AppError",
    "ConflictError",
    "DatabaseUnavailableError",
    "InternalFaultError",
    "NotFoundError",
    "ValidationError",
    "constraint_name_of",
    "error_response",
    "internal_fault",
    "is_audit_integrity_failure",
    "register_exception_handlers",
    "relation_of",
]
