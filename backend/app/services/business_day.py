"""The hotel's own calendar date, now (Issue H1).

A rule that depends on "today" depends on WHOSE today: a booking that checks out on the 6th has
ended in Athens hours before it has ended in UTC. Every such rule asks the hotel's own zone,
through an injectable clock so a test can stand at any instant. A zone name the runtime does not
know falls back to UTC -- the same fallback ``DemandObservationService`` and the analytics
bucketing take.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.hotel import Hotel

#: The instant "now" is read from. Injected, so no rule here reads the wall clock directly.
Clock = Callable[[], dt.datetime]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def hotel_today(hotel: Hotel, clock: Clock) -> dt.date:
    """The hotel's calendar date at the clock's instant, in its own zone; UTC's if unknown."""
    try:
        zone = ZoneInfo(hotel.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return clock().astimezone(zone).date()


__all__ = ["Clock", "hotel_today", "utc_now"]
