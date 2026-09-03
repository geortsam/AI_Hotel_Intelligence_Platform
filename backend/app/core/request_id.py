"""The correlation identifier for one request.

Stage 4.5.4.1. A request id ties the lines one request produced together across thirteen
loggers. That is all it is.

**It is not a credential, and nothing may treat it as one.** It never identifies a user,
selects a hotel, reaches a database lookup, or keys a rate-limit bucket -- those are decided by
the bearer token, the URL, a public id and the peer address respectively, and a header the
client controls must not influence any of them. Structural tests assert it appears in none of
those paths.

**A supplied id is sanitised, never trusted.** The header is client-controlled, and this
codebase logs through ``%s`` formatting straight into a record: a value containing a newline
would let an attacker forge whole log lines, and an unbounded one would be copied into every
record of the request. Anything that does not match :data:`VALID_REQUEST_ID` is discarded in
silence -- not rejected with a 400, because a debugging aid must never become a way to make a
request fail, and not echoed back, because reflecting attacker-chosen bytes is the thing being
avoided.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token

#: The accepted shape of a supplied ``X-Request-ID``: printable, bounded, and free of anything
#: that could break a log line or a header.
#:
#: Matched with ``fullmatch`` rather than ``match`` and deliberately NOT anchored with ``$``.
#: In Python ``$`` also matches immediately before a trailing newline, so ``^[A-Za-z0-9._-]+$``
#: would ACCEPT ``"abc\\n"`` -- exactly the injection this is meant to stop. ``fullmatch``
#: requires the whole string, newline included, to be consumed by the character class.
VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

#: The header carrying it, in and out.
REQUEST_ID_HEADER = "x-request-id"

#: What a log record shows when it was emitted outside any request -- application startup, a
#: background call, a unit test. A literal placeholder rather than an empty string, so a log
#: line always has a value in that column and nothing has to guess whether one was dropped.
NO_REQUEST_ID = "-"

#: ``None`` rather than the placeholder as the default: "there is no request" and "there is a
#: request whose id is a dash" are different states, and only the formatter should conflate
#: them.
_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """A fresh identifier.

    UUID4 hex, not a ``UUID`` object: what goes into a log record and a header should already
    be the string it will be rendered as, so no formatter has to call ``str()`` on it and no
    two call sites can disagree about the punctuation.
    """
    return uuid.uuid4().hex


def sanitise_request_id(supplied: str | None) -> str:
    """The id to use for this request: the supplied one if it is acceptable, else a new one.

    Returns a value that is always safe to log and to echo. The rejected input is neither
    returned nor recorded anywhere -- it is dropped here and never reaches a log record.
    """
    if supplied is not None and VALID_REQUEST_ID.fullmatch(supplied):
        return supplied
    return new_request_id()


def current_request_id() -> str | None:
    """The id of the request being handled, or None outside one."""
    return _REQUEST_ID.get()


def bind_request_id(request_id: str) -> Token[str | None]:
    """Make *request_id* current, returning the token that undoes it."""
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Restore whatever was current before the matching :func:`bind_request_id`.

    Resetting with the token rather than setting None is what makes nesting safe and what
    guarantees one request cannot inherit another's id.
    """
    _REQUEST_ID.reset(token)


__all__ = [
    "NO_REQUEST_ID",
    "REQUEST_ID_HEADER",
    "VALID_REQUEST_ID",
    "bind_request_id",
    "current_request_id",
    "new_request_id",
    "reset_request_id",
    "sanitise_request_id",
]
