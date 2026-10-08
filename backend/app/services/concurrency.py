"""The service side of the ``If-Match`` precondition (Issue H6).

A service that honours the precondition re-reads its row under a lock, then calls
:func:`require_unchanged` -- in that order, and before anything else the update does, the
empty-update shortcut included. The lock is what makes the comparison mean something: a
concurrent writer either committed before it (and its ``updated_at`` is what is compared) or
waits behind it. Equality only, never order -- see :mod:`app.api.preconditions`.
"""

from __future__ import annotations

import datetime as dt

from app.core.errors import StaleUpdateError


def require_unchanged(current: dt.datetime, expected: dt.datetime | None) -> None:
    """Refuse with 412 ``STALE_UPDATE`` unless the record is still the version the client
    edited. ``expected`` is ``None`` when the request carried no precondition."""
    if expected is not None and current != expected:
        raise StaleUpdateError()


__all__ = ["require_unchanged"]
