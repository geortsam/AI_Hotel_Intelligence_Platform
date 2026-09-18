"""Extraction for the V2 demand dataset: aggregate queries, no row-at-a-time work.

Read-only. Nothing here writes, commits or rolls back -- the transaction boundary belongs to the
caller, as it does for every repository in this layer.

## Why grouped queries and not one per hotel-day

A dataset over H hotels and D dates is HxD observations, and the naive shape of this extraction
is one query per observation. Each method below returns a whole hotel's series in one round
trip, grouped in the database:

    demand_by_date                 realised room nights per stay date
    on_books_room_nights_by_date   room nights known at each date's own cutoff
    rooms_existing_by_date         rooms that existed at each date's own cutoff

The middle one is the interesting case. Its cutoff moves with the row -- every stay date has a
different one -- so it is expressed as date arithmetic on ``stay_date`` inside the predicate
rather than as a bound parameter, which keeps it a single grouped scan instead of D scans.

## The target, and why it is not defined here

``OCCUPANCY_STATUSES`` and the join it applies to are the *existing* definition of an occupied
room night, shared with :class:`app.repositories.analytics.AnalyticsRepository`. This module
imports that vocabulary rather than restating it: a dataset whose target quietly disagreed with
the analytics an operator already reads would be worse than no dataset at all.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Date, DateTime, and_, cast, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingRoom, BookingRoomNight
from app.models.enums import OCCUPANCY_STATUSES
from app.models.room import Room


def _utc_midnight_ending(day_expression: object) -> object:
    """The instant midnight UTC at the START of *day_expression*, as a timestamptz.

    Written out rather than left to PostgreSQL's implicit date-to-timestamp coercion, which
    resolves against the session's ``TimeZone`` setting. That would make the extracted dataset
    depend on the connection that built it -- a difference of hours, in the one comparison where
    hours decide whether a fact leaked.
    """
    return func.timezone("UTC", cast(day_expression, DateTime))


class MlDemandRepository:
    """Day-grained extraction for one hotel at a time.

    One hotel per call, deliberately. A method taking a list of hotels is one edit away from a
    method taking none, and "every hotel's data in one result" is precisely the query this
    domain must not own -- see the tenant-isolation rules in ``docs/architecture.md``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- the target --------------------------------------------------------------------------

    def demand_by_date(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, int]:
        """Realised occupied room nights per stay date. This is the prediction target.

        Counts rows in ``booking_room_nights`` whose parent allocation is in a status meaning
        the room was physically occupied -- the same filter the analytics layer applies for
        occupancy, imported rather than rewritten.

        Dates with no rows are absent from the mapping rather than present as zero. The caller
        decides what an absent day means; this method reports only what is recorded.
        """
        rows = self._session.execute(
            select(BookingRoomNight.stay_date, func.count())
            .join(BookingRoom, BookingRoom.id == BookingRoomNight.booking_room_id)
            .where(
                and_(
                    BookingRoomNight.hotel_id == hotel_id,
                    BookingRoomNight.stay_date >= date_from,
                    BookingRoomNight.stay_date <= date_to,
                    BookingRoom.booking_status.in_(OCCUPANCY_STATUSES),
                )
            )
            .group_by(BookingRoomNight.stay_date)
        ).all()
        return {stay_date: int(count) for stay_date, count in rows}

    # --- features known at the cutoff ----------------------------------------------------------

    def on_books_room_nights_by_date(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date, horizon_days: int
    ) -> dict[dt.date, int]:
        """Room nights for each stay date already on the books at that date's own cutoff.

        **This is the leakage-critical query**, and its shape is the whole argument.

        For stay date D the cutoff instant is midnight UTC ending ``D - horizon_days``, which in
        date arithmetic is the start of ``D - horizon_days + 1``. A night is counted when:

        * its booking was created strictly before that instant (``booked_at``), and
        * it had not been cancelled by then (``cancelled_at`` null, or at/after the instant).

        Both columns are immutable records of *when* something happened, which is what makes the
        reconstruction sound. What is **not** reconstructible is the booking's status at that
        instant: only creation and cancellation are timestamped, so pending and confirmed cannot
        be told apart in the past. This count is therefore status-agnostic, and is named so that
        it cannot be read as "confirmed on the books" -- a claim the schema cannot support.

        The cutoff is computed per row from ``stay_date``, so one grouped scan answers the whole
        range rather than one query per date.
        """
        if horizon_days < 1:
            raise ValueError(f"horizon_days must be at least 1, got {horizon_days}")

        # stay_date - horizon_days + 1, as a date, then that date's midnight in UTC.
        cutoff_day = cast(BookingRoomNight.stay_date - literal(horizon_days - 1), Date)
        cutoff = _utc_midnight_ending(cutoff_day)

        rows = self._session.execute(
            select(BookingRoomNight.stay_date, func.count())
            .join(BookingRoom, BookingRoom.id == BookingRoomNight.booking_room_id)
            .join(Booking, Booking.id == BookingRoom.booking_id)
            .where(
                and_(
                    BookingRoomNight.hotel_id == hotel_id,
                    BookingRoomNight.stay_date >= date_from,
                    BookingRoomNight.stay_date <= date_to,
                    Booking.booked_at < cutoff,
                    or_(Booking.cancelled_at.is_(None), Booking.cancelled_at >= cutoff),
                )
            )
            .group_by(BookingRoomNight.stay_date)
        ).all()
        return {stay_date: int(count) for stay_date, count in rows}

    def rooms_existing_by_date(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date, horizon_days: int
    ) -> dict[dt.date, int]:
        """Rooms that demonstrably existed at each stay date's own cutoff.

        ``is_active`` is deliberately not consulted. It carries no history, so its current value
        describes the room now rather than at the cutoff; reading it would import a fact from
        after the instant this feature claims to respect. Counting existence from an immutable
        ``created_at`` is the part that can be reconstructed honestly.

        A correlated count would be one query per date. Instead every room's ``created_at`` is
        fetched once -- there are tens of rooms per hotel, not millions -- and the per-date
        counts are accumulated here. Bounded by construction, and one round trip.
        """
        if horizon_days < 1:
            raise ValueError(f"horizon_days must be at least 1, got {horizon_days}")

        created = (
            self._session.execute(select(Room.created_at).where(Room.hotel_id == hotel_id))
            .scalars()
            .all()
        )

        counts: dict[dt.date, int] = {}
        day = date_from
        while day <= date_to:
            cutoff = dt.datetime.combine(
                day - dt.timedelta(days=horizon_days - 1), dt.time.min, tzinfo=dt.UTC
            )
            counts[day] = sum(1 for moment in created if moment < cutoff)
            day += dt.timedelta(days=1)
        return counts

    # --- bounds ----------------------------------------------------------------------------------

    def first_and_last_stay_date(self, hotel_id: int) -> tuple[dt.date, dt.date] | None:
        """The extent of this hotel's occupied history, or None when it has none.

        Used to bound extraction instead of guessing a range, so a hotel with two months of data
        is not scanned across two years of empty dates.
        """
        first, last = self._session.execute(
            select(func.min(BookingRoomNight.stay_date), func.max(BookingRoomNight.stay_date))
            .join(BookingRoom, BookingRoom.id == BookingRoomNight.booking_room_id)
            .where(
                and_(
                    BookingRoomNight.hotel_id == hotel_id,
                    BookingRoom.booking_status.in_(OCCUPANCY_STATUSES),
                )
            )
        ).one()
        if first is None or last is None:
            return None
        return first, last
