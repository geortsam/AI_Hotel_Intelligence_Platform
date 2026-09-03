"""Read-only analytical queries.

**The single most important rule in this module: one metric, one query.**

Bookings fan out to allocations, allocations to nights, and a booking independently fans out
to revenue lines and reviews. Aggregating any two of those in one statement multiplies both:
a booking with 2 rooms x 4 nights and 3 revenue lines would report 24 revenue rows and
sum its revenue six times over. Every method below therefore aggregates exactly one grain,
and the service composes the results. It is slightly more round trips and enormously more
correct, and ``tests/integration/test_analytics_api.py`` builds precisely that fixture to
prove it.

Nothing here commits, rolls back, mutates, or raises. Every method takes ``hotel_id`` -- an
internal key the service has already resolved from the URL's ``public_id`` -- and there is no
method that reaches across hotels.

``daily_hotel_metrics`` is deliberately absent. It is an empty snapshot table awaiting a
population job that does not exist yet (verified live: zero rows, no function or trigger
writes to it), so reading it would report zeros for every hotel and writing it from a GET
would make analytics mutate operational state. Its **generated columns are still the
authority for the formulas** -- occupancy_rate, adr and revpar below are transcribed from
them, NULLIF guards included.
"""

from __future__ import annotations

import datetime as dt
import decimal
from collections.abc import Sequence
from typing import Any, NamedTuple

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models.booking import Booking, BookingRoom, BookingRoomNight
from app.models.enums import OCCUPANCY_STATUSES
from app.models.finance import Expense, ExpenseCategory, Revenue, RevenueCategory
from app.models.review import Review
from app.models.room import Room


class CurrencyAmount(NamedTuple):
    """A monetary total that never leaves its currency."""

    currency: str
    amount: decimal.Decimal


class RoomRevenueRow(NamedTuple):
    """Room revenue and the night counts ADR needs, within one currency."""

    currency: str
    room_revenue: decimal.Decimal
    room_nights_sold: int


class OccupancyCounts(NamedTuple):
    """Night counts at one grain. ``sold`` excludes complimentary nights."""

    occupied: int
    sold: int
    complimentary: int


class CategoryAmount(NamedTuple):
    """One category within one currency."""

    code: str
    flag: bool
    currency: str
    amount: decimal.Decimal
    tax_amount: decimal.Decimal
    entry_count: int


class SourceStats(NamedTuple):
    """One review channel."""

    source: str
    review_count: int
    published_count: int
    average_rating_normalized: decimal.Decimal | None


ZERO = decimal.Decimal("0")


class AnalyticsRepository:
    """Aggregate projections over one hotel's operational and ledger data."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- bookings -----------------------------------------------------------------------

    def booking_counts_by_status(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[str, int]:
        """Bookings CREATED in range, grouped by their current status.

        The date column is ``booked_at``, cast to a date -- the same column
        ``daily_hotel_metrics.bookings_created`` counts. Grouping happens in the database so
        no row set crosses the boundary.
        """
        rows = (
            self._session.execute(
                select(Booking.status, func.count())
                .where(
                    Booking.hotel_id == hotel_id,
                    func.date(Booking.booked_at) >= date_from,
                    func.date(Booking.booked_at) <= date_to,
                )
                .group_by(Booking.status)
            )
            .tuples()
            .all()
        )
        return dict(rows)

    def booking_counts_by_stay_overlap(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[str, int]:
        """Bookings whose STAY WINDOW overlaps the range, grouped by current status.

        A different question from ``booking_counts_by_status``, and both are worth asking:
        "how many bookings were made in September" is not "how many bookings stay in
        September". Counting only by creation date makes the headline figure zero for any
        forward-looking window, which is exactly the kind of quietly misleading metric this
        domain must not produce.

        The predicate treats a stay as half-open ``[check_in, check_out)``, matching the
        ``daterange(check_in_date, check_out_date, '[)')`` the schema's own exclusion
        constraint uses: a booking arriving on ``date_to`` overlaps, one departing on
        ``date_from`` does not.
        """
        rows = (
            self._session.execute(
                select(Booking.status, func.count())
                .where(
                    Booking.hotel_id == hotel_id,
                    Booking.check_in_date <= date_to,
                    Booking.check_out_date > date_from,
                )
                .group_by(Booking.status)
            )
            .tuples()
            .all()
        )
        return dict(rows)

    def arrival_count(self, hotel_id: int, date_from: dt.date, date_to: dt.date) -> int:
        """Bookings whose ``check_in_date`` falls in range, whatever their status."""
        return self._count(
            Booking,
            Booking.hotel_id == hotel_id,
            Booking.check_in_date >= date_from,
            Booking.check_in_date <= date_to,
        )

    def departure_count(self, hotel_id: int, date_from: dt.date, date_to: dt.date) -> int:
        """Bookings whose ``check_out_date`` falls in range.

        Distinct from a night: the schema's CHECK requires ``stay_date < check_out_date``, so
        a departure date contributes a departure and no room night.
        """
        return self._count(
            Booking,
            Booking.hotel_id == hotel_id,
            Booking.check_out_date >= date_from,
            Booking.check_out_date <= date_to,
        )

    def cancellation_count(self, hotel_id: int, date_from: dt.date, date_to: dt.date) -> int:
        """Bookings cancelled in range, by ``cancelled_at``.

        ``ck_bookings_cancellation_consistent`` makes that column non-null exactly when the
        status is cancelled, so no status filter is needed.
        """
        return self._count(
            Booking,
            Booking.hotel_id == hotel_id,
            func.date(Booking.cancelled_at) >= date_from,
            func.date(Booking.cancelled_at) <= date_to,
        )

    def bookings_created_by_day(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, int]:
        return self._count_by_day(
            func.date(Booking.booked_at),
            Booking,
            Booking.hotel_id == hotel_id,
            func.date(Booking.booked_at) >= date_from,
            func.date(Booking.booked_at) <= date_to,
        )

    def arrivals_by_day(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, int]:
        return self._count_by_day(
            Booking.check_in_date,
            Booking,
            Booking.hotel_id == hotel_id,
            Booking.check_in_date >= date_from,
            Booking.check_in_date <= date_to,
        )

    def departures_by_day(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, int]:
        return self._count_by_day(
            Booking.check_out_date,
            Booking,
            Booking.hotel_id == hotel_id,
            Booking.check_out_date >= date_from,
            Booking.check_out_date <= date_to,
        )

    def cancellations_by_day(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, int]:
        return self._count_by_day(
            func.date(Booking.cancelled_at),
            Booking,
            Booking.hotel_id == hotel_id,
            func.date(Booking.cancelled_at) >= date_from,
            func.date(Booking.cancelled_at) <= date_to,
        )

    # --- occupancy ----------------------------------------------------------------------

    def active_room_count(self, hotel_id: int) -> int:
        """The hotel's CURRENT active physical rooms -- the occupancy denominator's basis.

        ``rooms.is_active`` marks a room as sellable inventory; ``rooms.status`` is a live
        housekeeping state (available / occupied / cleaning / maintenance / out_of_order) with
        **no history**, so applying it to a past date would be wrong and it is deliberately
        not part of this count. ``daily_hotel_metrics`` takes the same view: its
        ``occupancy_rate`` divides by ``available_rooms`` and tracks ``out_of_order_rooms``
        as a separate figure rather than subtracting it.
        """
        return self._count(Room, Room.hotel_id == hotel_id, Room.is_active.is_(True))

    def occupancy_counts(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> OccupancyCounts:
        """Room nights actually occupied in range.

        Counted from ``booking_room_nights`` -- one row per room per night, guaranteed
        gap-free by the deferred completeness trigger -- never from booking headers. A
        booking is not occupancy; a night row is.

        The status filter uses ``OCCUPANCY_STATUSES`` (confirmed, checked_in, checked_out),
        which is wider than the inventory-holding pair on purpose: a completed stay no longer
        blocks a room but certainly occupied it. ``pending`` is excluded, so an abandoned
        checkout never appears as occupancy.
        """
        complimentary = func.count().filter(BookingRoomNight.is_complimentary.is_(True))
        row = self._session.execute(
            select(func.count(), complimentary)
            .select_from(BookingRoomNight)
            .join(BookingRoom, BookingRoom.id == BookingRoomNight.booking_room_id)
            .where(*self._night_filters(hotel_id, date_from, date_to))
        ).one()
        occupied, comp = int(row[0] or 0), int(row[1] or 0)
        return OccupancyCounts(occupied=occupied, sold=occupied - comp, complimentary=comp)

    def occupied_nights_by_day(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, OccupancyCounts]:
        """The same counts at day granularity, keyed by ``stay_date``."""
        complimentary = func.count().filter(BookingRoomNight.is_complimentary.is_(True))
        rows = (
            self._session.execute(
                select(BookingRoomNight.stay_date, func.count(), complimentary)
                .select_from(BookingRoomNight)
                .join(BookingRoom, BookingRoom.id == BookingRoomNight.booking_room_id)
                .where(*self._night_filters(hotel_id, date_from, date_to))
                .group_by(BookingRoomNight.stay_date)
            )
            .tuples()
            .all()
        )
        return {
            day: OccupancyCounts(
                occupied=int(total or 0),
                sold=int(total or 0) - int(comp or 0),
                complimentary=int(comp or 0),
            )
            for day, total, comp in rows
        }

    # --- room revenue -------------------------------------------------------------------

    def room_revenue_by_currency(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> list[RoomRevenueRow]:
        """Room revenue from the night rate, grouped by the owning booking's currency.

        ``booking_room_nights`` carries no currency of its own (approved decision 18), so it
        is taken from the booking. Both joins are many-to-one -- night to allocation to
        booking -- so no row is counted twice.

        ``room_nights_sold`` excludes complimentary nights: it is the ADR denominator, and a
        free night would otherwise drag the average rate down.
        """
        sold = func.count().filter(BookingRoomNight.is_complimentary.is_(False))
        rows = (
            self._session.execute(
                select(Booking.currency, func.coalesce(func.sum(BookingRoomNight.rate), 0), sold)
                .select_from(BookingRoomNight)
                .join(BookingRoom, BookingRoom.id == BookingRoomNight.booking_room_id)
                .join(Booking, Booking.id == BookingRoom.booking_id)
                .where(*self._night_filters(hotel_id, date_from, date_to))
                .group_by(Booking.currency)
                .order_by(Booking.currency)
            )
            .tuples()
            .all()
        )
        return [
            RoomRevenueRow(currency=c, room_revenue=amount or ZERO, room_nights_sold=int(n or 0))
            for c, amount, n in rows
        ]

    def room_revenue_by_day(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, list[RoomRevenueRow]]:
        """The same, at day granularity, keyed by ``stay_date``."""
        sold = func.count().filter(BookingRoomNight.is_complimentary.is_(False))
        rows = (
            self._session.execute(
                select(
                    BookingRoomNight.stay_date,
                    Booking.currency,
                    func.coalesce(func.sum(BookingRoomNight.rate), 0),
                    sold,
                )
                .select_from(BookingRoomNight)
                .join(BookingRoom, BookingRoom.id == BookingRoomNight.booking_room_id)
                .join(Booking, Booking.id == BookingRoom.booking_id)
                .where(*self._night_filters(hotel_id, date_from, date_to))
                .group_by(BookingRoomNight.stay_date, Booking.currency)
                .order_by(BookingRoomNight.stay_date, Booking.currency)
            )
            .tuples()
            .all()
        )
        by_day: dict[dt.date, list[RoomRevenueRow]] = {}
        for day, currency, amount, n in rows:
            by_day.setdefault(day, []).append(
                RoomRevenueRow(
                    currency=currency, room_revenue=amount or ZERO, room_nights_sold=int(n or 0)
                )
            )
        return by_day

    # --- ledger -------------------------------------------------------------------------

    def ledger_revenue_by_currency(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date, *, is_room_revenue: bool
    ) -> list[CurrencyAmount]:
        """Revenue-ledger totals per currency, split by the category's ``is_room_revenue``.

        That flag exists precisely so this split is data-driven rather than a hard-coded
        category-name comparison.
        """
        rows = (
            self._session.execute(
                select(Revenue.currency, func.coalesce(func.sum(Revenue.amount), 0))
                .select_from(Revenue)
                .join(RevenueCategory, RevenueCategory.id == Revenue.category_id)
                .where(
                    Revenue.hotel_id == hotel_id,
                    Revenue.revenue_date >= date_from,
                    Revenue.revenue_date <= date_to,
                    RevenueCategory.is_room_revenue.is_(is_room_revenue),
                )
                .group_by(Revenue.currency)
                .order_by(Revenue.currency)
            )
            .tuples()
            .all()
        )
        return [CurrencyAmount(currency=c, amount=amount or ZERO) for c, amount in rows]

    def ledger_revenue_by_day(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date, *, is_room_revenue: bool
    ) -> dict[dt.date, list[CurrencyAmount]]:
        rows = (
            self._session.execute(
                select(
                    Revenue.revenue_date,
                    Revenue.currency,
                    func.coalesce(func.sum(Revenue.amount), 0),
                )
                .select_from(Revenue)
                .join(RevenueCategory, RevenueCategory.id == Revenue.category_id)
                .where(
                    Revenue.hotel_id == hotel_id,
                    Revenue.revenue_date >= date_from,
                    Revenue.revenue_date <= date_to,
                    RevenueCategory.is_room_revenue.is_(is_room_revenue),
                )
                .group_by(Revenue.revenue_date, Revenue.currency)
                .order_by(Revenue.revenue_date, Revenue.currency)
            )
            .tuples()
            .all()
        )
        return self._group_money_by_day(rows)

    def expenses_by_currency(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> list[CurrencyAmount]:
        rows = (
            self._session.execute(
                select(Expense.currency, func.coalesce(func.sum(Expense.amount), 0))
                .where(
                    Expense.hotel_id == hotel_id,
                    Expense.expense_date >= date_from,
                    Expense.expense_date <= date_to,
                )
                .group_by(Expense.currency)
                .order_by(Expense.currency)
            )
            .tuples()
            .all()
        )
        return [CurrencyAmount(currency=c, amount=amount or ZERO) for c, amount in rows]

    def expenses_by_day(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, list[CurrencyAmount]]:
        rows = (
            self._session.execute(
                select(
                    Expense.expense_date,
                    Expense.currency,
                    func.coalesce(func.sum(Expense.amount), 0),
                )
                .where(
                    Expense.hotel_id == hotel_id,
                    Expense.expense_date >= date_from,
                    Expense.expense_date <= date_to,
                )
                .group_by(Expense.expense_date, Expense.currency)
                .order_by(Expense.expense_date, Expense.currency)
            )
            .tuples()
            .all()
        )
        return self._group_money_by_day(rows)

    def revenue_by_category(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> list[CategoryAmount]:
        """Grouped by category code AND currency, ordered for a deterministic response."""
        rows = (
            self._session.execute(
                select(
                    RevenueCategory.code,
                    RevenueCategory.is_room_revenue,
                    Revenue.currency,
                    func.coalesce(func.sum(Revenue.amount), 0),
                    func.coalesce(func.sum(Revenue.tax_amount), 0),
                    func.count(),
                )
                .select_from(Revenue)
                .join(RevenueCategory, RevenueCategory.id == Revenue.category_id)
                .where(
                    Revenue.hotel_id == hotel_id,
                    Revenue.revenue_date >= date_from,
                    Revenue.revenue_date <= date_to,
                )
                .group_by(RevenueCategory.code, RevenueCategory.is_room_revenue, Revenue.currency)
                .order_by(RevenueCategory.code, Revenue.currency)
            )
            .tuples()
            .all()
        )
        return [
            CategoryAmount(
                code=code,
                flag=flag,
                currency=currency,
                amount=amount or ZERO,
                tax_amount=tax or ZERO,
                entry_count=int(n or 0),
            )
            for code, flag, currency, amount, tax, n in rows
        ]

    def expenses_by_category(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> list[CategoryAmount]:
        rows = (
            self._session.execute(
                select(
                    ExpenseCategory.code,
                    ExpenseCategory.is_fixed_cost,
                    Expense.currency,
                    func.coalesce(func.sum(Expense.amount), 0),
                    func.coalesce(func.sum(Expense.tax_amount), 0),
                    func.count(),
                )
                .select_from(Expense)
                .join(ExpenseCategory, ExpenseCategory.id == Expense.category_id)
                .where(
                    Expense.hotel_id == hotel_id,
                    Expense.expense_date >= date_from,
                    Expense.expense_date <= date_to,
                )
                .group_by(ExpenseCategory.code, ExpenseCategory.is_fixed_cost, Expense.currency)
                .order_by(ExpenseCategory.code, Expense.currency)
            )
            .tuples()
            .all()
        )
        return [
            CategoryAmount(
                code=code,
                flag=flag,
                currency=currency,
                amount=amount or ZERO,
                tax_amount=tax or ZERO,
                entry_count=int(n or 0),
            )
            for code, flag, currency, amount, tax, n in rows
        ]

    # --- reviews ------------------------------------------------------------------------

    def review_totals(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> tuple[int, int, decimal.Decimal | None]:
        """Count, published count and mean normalized rating over ``review_date``.

        ``rating_normalized`` is the PostgreSQL-generated ``rating / rating_scale``: the only
        average that is meaningful across a source scoring out of ten and one out of five.
        Reviews with no guest and no booking are included -- the schema permits them, and
        dropping them would erase every harvested review from the average.
        """
        published = func.count().filter(Review.is_published.is_(True))
        row = self._session.execute(
            select(func.count(), published, func.avg(Review.rating_normalized)).where(
                *self._review_filters(hotel_id, date_from, date_to)
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0), row[2]

    def rating_distribution(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[int, int]:
        """Counts per fifth of the normalized scale.

        The bucketing is an analytics-layer presentation of the generated column, defined
        here so it is reproducible: bucket n covers ((n-1)/5, n/5], with 0.0 placed in bucket
        1 so a zero rating is not lost.
        """
        bucket = case(
            (Review.rating_normalized <= decimal.Decimal("0.2"), 1),
            (Review.rating_normalized <= decimal.Decimal("0.4"), 2),
            (Review.rating_normalized <= decimal.Decimal("0.6"), 3),
            (Review.rating_normalized <= decimal.Decimal("0.8"), 4),
            else_=5,
        )
        rows = (
            self._session.execute(
                select(bucket.label("bucket"), func.count())
                .where(*self._review_filters(hotel_id, date_from, date_to))
                .group_by(bucket)
            )
            .tuples()
            .all()
        )
        return {int(b): int(n) for b, n in rows}

    def reviews_by_source(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> list[SourceStats]:
        published = func.count().filter(Review.is_published.is_(True))
        rows = (
            self._session.execute(
                select(Review.source, func.count(), published, func.avg(Review.rating_normalized))
                .where(*self._review_filters(hotel_id, date_from, date_to))
                .group_by(Review.source)
                .order_by(Review.source)
            )
            .tuples()
            .all()
        )
        return [
            SourceStats(
                source=source,
                review_count=int(total or 0),
                published_count=int(pub or 0),
                average_rating_normalized=avg,
            )
            for source, total, pub, avg in rows
        ]

    # --- internals ----------------------------------------------------------------------

    @staticmethod
    def _night_filters(hotel_id: int, date_from: dt.date, date_to: dt.date) -> tuple[Any, ...]:
        """The one definition of "an occupied room night in range", shared by every
        night-grained query so they can never disagree."""
        return (
            BookingRoomNight.hotel_id == hotel_id,
            BookingRoomNight.stay_date >= date_from,
            BookingRoomNight.stay_date <= date_to,
            BookingRoom.booking_status.in_(OCCUPANCY_STATUSES),
        )

    @staticmethod
    def _review_filters(hotel_id: int, date_from: dt.date, date_to: dt.date) -> tuple[Any, ...]:
        return (
            Review.hotel_id == hotel_id,
            Review.review_date >= date_from,
            Review.review_date <= date_to,
        )

    def _count(self, model: Any, *filters: Any) -> int:
        return int(
            self._session.scalar(select(func.count()).select_from(model).where(and_(*filters))) or 0
        )

    def _count_by_day(self, day_column: Any, model: Any, *filters: Any) -> dict[dt.date, int]:
        rows = (
            self._session.execute(
                select(day_column, func.count())
                .select_from(model)
                .where(and_(*filters))
                .group_by(day_column)
            )
            .tuples()
            .all()
        )
        return {day: int(n or 0) for day, n in rows}

    @staticmethod
    def _group_money_by_day(
        rows: Sequence[tuple[dt.date, str, decimal.Decimal]],
    ) -> dict[dt.date, list[CurrencyAmount]]:
        by_day: dict[dt.date, list[CurrencyAmount]] = {}
        for day, currency, amount in rows:
            by_day.setdefault(day, []).append(
                CurrencyAmount(currency=currency, amount=amount or ZERO)
            )
        return by_day


__all__ = [
    "AnalyticsRepository",
    "CategoryAmount",
    "CurrencyAmount",
    "OccupancyCounts",
    "RoomRevenueRow",
    "SourceStats",
]
