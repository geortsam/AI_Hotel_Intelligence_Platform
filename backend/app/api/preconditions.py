"""The optional ``If-Match`` precondition on PATCH (Issue H6).

Without it an update is last-write-wins: a client that loaded a record, waited, and saved
overwrote whatever had been written in between. With it the client says which version it
edited, and a version that is no longer current is refused with **412** ``STALE_UPDATE`` before
anything is written.

**The token is the record's own ``updated_at``**, as a strong entity-tag::

    If-Match: "2026-10-09T10:00:00.123456Z"

``updated_at`` is set by the ``set_updated_at`` trigger on every write, to the transaction's
start time, so it changes whenever the row does. It is a ``timestamptz`` -- microsecond
precision -- and the API serialises it without loss, so the string a client received *is* a
valid token. The comparison is by **instant and exact equality**, never by order: the trigger
stamps ``now()``, which is when the transaction began, not when it committed, so two writes
need not stamp in commit order. Any RFC 3339 rendering of the same instant matches (``Z`` or an
offset, with or without trailing zero microseconds); :func:`entity_tag` is the canonical one.

**Grammar accepted**, after surrounding whitespace is trimmed: ``*`` (the record exists -- which
an update requires anyway, so it constrains nothing), or exactly one strong entity-tag whose
content is a timezone-aware RFC 3339 timestamp with at most six fractional digits. Anything else
is a 422 ``VALIDATION_ERROR`` located at ``["header", "If-Match"]``, in the same envelope as
every other malformed input: a weak tag (``W/"..."``, which RFC 9110 says can never satisfy
``If-Match``), an unquoted value, a list, an empty value, a timestamp without a zone, or one
precise beyond a microsecond (which could only match by being silently truncated).

**Absent, nothing changes**: the update runs exactly as it did before H6, with no lock and no
comparison. The header is optional so that every existing client keeps working.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any

from fastapi import Depends, Header, status
from fastapi.exceptions import RequestValidationError

from app.schemas.common import ErrorResponse

HEADER = "If-Match"

#: One strong entity-tag: DQUOTE *etagc DQUOTE, etagc being %x21 / %x23-7E (RFC 9110 8.8.3).
STRONG_TAG = re.compile(r'"([\x21\x23-\x7e]*)"')
#: More than six fractional digits: precision PostgreSQL never stored.
SUB_MICROSECOND = re.compile(r"[.,]\d{7,}")

IF_MATCH_DESCRIPTION = (
    "Optional. The `updated_at` of the record as it was loaded, quoted as a strong "
    'entity-tag, e.g. `"2026-10-09T10:00:00.123456Z"`. If the record has changed since, the '
    "update is refused with 412 `STALE_UPDATE` and nothing is written. Compared by instant, "
    "exactly. `*` is accepted and constrains nothing; any other form is a 422. Without the "
    "header the update is applied as it always was."
)

STALE_UPDATE_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_412_PRECONDITION_FAILED: {
        "model": ErrorResponse,
        "description": "`STALE_UPDATE`: the `If-Match` precondition named a version of the "
        "record that is no longer current. Nothing was written; reload and re-apply.",
    }
}


def entity_tag(updated_at: dt.datetime) -> str:
    """The canonical ``If-Match`` value for a record last written at ``updated_at``.

    UTC, always six fractional digits, ``Z``: one string per instant, whatever the session's
    time zone or the instant's microseconds.
    """
    if updated_at.tzinfo is None:
        raise ValueError("updated_at must be timezone-aware")
    utc = updated_at.astimezone(dt.UTC).isoformat(timespec="microseconds")
    return '"' + utc.removesuffix("+00:00") + 'Z"'


def _malformed(message: str, value: str) -> RequestValidationError:
    return RequestValidationError(
        [{"type": "value_error", "loc": ("header", HEADER), "msg": message, "input": value}]
    )


def parse_if_match(value: str | None) -> dt.datetime | None:
    """The instant an ``If-Match`` value names, or ``None`` when it constrains nothing.

    Raises :class:`RequestValidationError` -- the API's 422 -- for any value outside the
    grammar in the module docstring.
    """
    if value is None:
        return None
    text = value.strip()
    if text == "*":
        return None
    if text.startswith("W/"):
        raise _malformed(
            "If-Match takes a strong entity-tag; a weak one (W/) can never match.", value
        )
    match = STRONG_TAG.fullmatch(text)
    if match is None:
        raise _malformed(
            'If-Match must be exactly one quoted entity-tag, e.g. "2026-10-09T10:00:00Z", or *.',
            value,
        )
    content = match.group(1)
    if SUB_MICROSECOND.search(content):
        raise _malformed("The If-Match timestamp is more precise than a microsecond.", value)
    try:
        stamp = dt.datetime.fromisoformat(content)
    except ValueError:
        raise _malformed("The If-Match entity-tag is not an RFC 3339 timestamp.", value) from None
    if stamp.tzinfo is None:
        raise _malformed("The If-Match timestamp must carry a time zone.", value)
    return stamp


def if_match_precondition(
    if_match: Annotated[str | None, Header(alias=HEADER, description=IF_MATCH_DESCRIPTION)] = None,
) -> dt.datetime | None:
    return parse_if_match(if_match)


#: The parsed precondition: the ``updated_at`` the client edited, or ``None`` for none.
IfMatch = Annotated[dt.datetime | None, Depends(if_match_precondition)]


__all__ = [
    "HEADER",
    "IF_MATCH_DESCRIPTION",
    "STALE_UPDATE_RESPONSE",
    "IfMatch",
    "entity_tag",
    "if_match_precondition",
    "parse_if_match",
]
