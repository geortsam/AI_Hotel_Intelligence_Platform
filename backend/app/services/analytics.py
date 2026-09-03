"""Analytics domain logic: date-range validation, metric composition, currency discipline.

**Read-only.** This service never commits, never rolls back and never constructs a write. It
resolves the hotel, validates the range, calls one repository method per metric grain and
assembles typed projections. Analytics observing the business must never change it.

**Currency is the hard rule here.** ``revenue`` and ``expenses`` carry their own currency
column and room revenue inherits its booking's, so a range can hold several. Nothing in this
module adds two amounts in different currencies, and no FX rate exists anywhere in the
codebase. Every monetary metric leaves as a list of per-currency buckets; the net result is
computed **within** each currency and a currency present on only one side still appears, with
the other side treated as zero for that currency alone.

**Only defensible metrics are exposed.** The formulas for occupancy, ADR and RevPAR are
transcribed from ``daily_hotel_metrics``'s own generated columns rather than invented, NULLIF
guards included -- an undefined rate is reported as null, never as zero, because zero would
poison every average computed downstream.

``daily_hotel_metrics`` itself is neither read nor written. It is an empty snapshot table
awaiting a population job that does not exist (verified live), and populating it from a GET
request would make a read endpoint mutate state.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from app.core.errors import ValidationError
from app.models.enums import BookingStatus
from app.models.hotel import Hotel
from app.repositories.analytics import (
    AnalyticsRepository,
    CategoryAmount,
    CurrencyAmount,
    RoomRevenueRow,
)
from app.schemas.analytics import (
    MAX_RANGE_DAYS,
    BookingStatusCounts,
    DailyMetricsRow,
    DailySeriesResponse,
    DateRange,
    ExpenseBreakdownResponse,
    ExpenseCategoryBreakdown,
    MoneyByCurrency,
    OccupancyMetrics,
    OverviewResponse,
    RatingBucket,
    RevenueBreakdownResponse,
    RevenueCategoryBreakdown,
    ReviewAnalyticsResponse,
    ReviewMetrics,
    ReviewSourceBreakdown,
    RoomRevenueByCurrency,
    StayFlowMetrics,
)
from app.services.scope import HotelScopeResolver

ZERO = decimal.Decimal("0")

#: Money is NUMERIC(14,2) throughout; derived rates are quantised to the same scale so ADR
#: and RevPAR are comparable with the amounts they came from.
MONEY_PLACES = decimal.Decimal("0.01")
#: occupancy_rate is NUMERIC(5,4) on daily_hotel_metrics. Matched here.
RATE_PLACES = decimal.Decimal("0.0001")

#: The five bands rating_distribution reports, as (bucket, lower, upper).
RATING_BUCKETS: tuple[tuple[int, str, str], ...] = (
    (1, "0.0000", "0.2000"),
    (2, "0.2000", "0.4000"),
    (3, "0.4000", "0.6000"),
    (4, "0.6000", "0.8000"),
    (5, "0.8000", "1.0000"),
)


def _quantise(value: decimal.Decimal | None, places: decimal.Decimal) -> decimal.Decimal | None:
    """Round half-up to the schema's own scale, preserving null."""
    if value is None:
        return None
    return value.quantize(places, rounding=decimal.ROUND_HALF_UP)


def _amount(value: decimal.Decimal | None, places: decimal.Decimal) -> decimal.Decimal:
    """Quantise a monetary figure, treating an absent value as zero.

    The ``is None`` test is deliberate and load-bearing: ``Decimal("0.00")`` is **falsy**, so
    the obvious ``_quantise(x, places) or ZERO`` silently discarded the scale on every zero
    and rendered ``"0"`` beside ``"50.00"`` in the same payload. Caught in live verification.
    """
    quantised = _quantise(value if value is not None else ZERO, places)
    assert quantised is not None  # _quantise only returns None for a None input
    return quantised


def _divide(
    numerator: decimal.Decimal, denominator: int, places: decimal.Decimal
) -> decimal.Decimal | None:
    """The NULLIF guard from the generated columns, in Python.

    A hotel with no sold nights has an *undefined* ADR, not one of zero.
    """
    if denominator == 0:
        return None
    return _quantise(numerator / decimal.Decimal(denominator), places)


class AnalyticsService:
    """Read-only KPI composition for one hotel."""

    def __init__(self, repository: AnalyticsRepository, scope: HotelScopeResolver) -> None:
        # No session: this service owns no unit of work because it performs no writes.
        self._repository = repository
        self._scope = scope

    # --- endpoints ----------------------------------------------------------------------

    def overview(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> OverviewResponse:
        """The KPI snapshot for one range.

        Each metric comes from its own query at its own grain -- see the repository docstring
        for why a single wide join would multiply them.
        """
        hotel = self._resolve(hotel_public_id)
        span = self._require_range(date_from, date_to)

        occupancy = self._occupancy(hotel.id, span)
        room_revenue = self._room_revenue(hotel.id, span, occupancy.available_room_nights)
        other_revenue = self._money(
            self._repository.ledger_revenue_by_currency(
                hotel.id, span.date_from, span.date_to, is_room_revenue=False
            )
        )
        ledger_room_revenue = self._money(
            self._repository.ledger_revenue_by_currency(
                hotel.id, span.date_from, span.date_to, is_room_revenue=True
            )
        )
        expenses = self._money(
            self._repository.expenses_by_currency(hotel.id, span.date_from, span.date_to)
        )

        earned = self._combine(
            [MoneyByCurrency(currency=r.currency, amount=r.room_revenue) for r in room_revenue],
            other_revenue,
        )
        net = self._subtract(earned, expenses)

        currencies = {bucket.currency for bucket in [*earned, *expenses, *ledger_room_revenue]}

        return OverviewResponse(
            hotel_public_id=hotel.public_id,
            range=span,
            bookings_created=self._status_counts(
                self._repository.booking_counts_by_status(hotel.id, span.date_from, span.date_to)
            ),
            bookings_by_stay=self._status_counts(
                self._repository.booking_counts_by_stay_overlap(
                    hotel.id, span.date_from, span.date_to
                )
            ),
            stay_flow=StayFlowMetrics(
                arrivals=self._repository.arrival_count(hotel.id, span.date_from, span.date_to),
                departures=self._repository.departure_count(hotel.id, span.date_from, span.date_to),
                cancellations=self._repository.cancellation_count(
                    hotel.id, span.date_from, span.date_to
                ),
            ),
            occupancy=occupancy,
            room_revenue=room_revenue,
            other_revenue=other_revenue,
            ledger_room_revenue=ledger_room_revenue,
            total_expenses=expenses,
            net_operating_result=net,
            is_multi_currency=len(currencies) > 1,
            reviews=self._review_totals(hotel.id, span),
        )

    def daily(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> DailySeriesResponse:
        """A gap-free daily series, ascending by date.

        Days with no activity are emitted as zero rows rather than omitted: a chart that
        skips empty days draws a misleading line, and a regular series is what any later
        forecasting stage needs.
        """
        hotel = self._resolve(hotel_public_id)
        span = self._require_range(date_from, date_to)
        capacity = self._repository.active_room_count(hotel.id)

        occupancy = self._repository.occupied_nights_by_day(hotel.id, span.date_from, span.date_to)
        room_revenue = self._repository.room_revenue_by_day(hotel.id, span.date_from, span.date_to)
        other_revenue = self._repository.ledger_revenue_by_day(
            hotel.id, span.date_from, span.date_to, is_room_revenue=False
        )
        expenses = self._repository.expenses_by_day(hotel.id, span.date_from, span.date_to)
        arrivals = self._repository.arrivals_by_day(hotel.id, span.date_from, span.date_to)
        departures = self._repository.departures_by_day(hotel.id, span.date_from, span.date_to)
        created = self._repository.bookings_created_by_day(hotel.id, span.date_from, span.date_to)
        cancelled = self._repository.cancellations_by_day(hotel.id, span.date_from, span.date_to)

        days: list[DailyMetricsRow] = []
        for offset in range(span.days):
            day = span.date_from + dt.timedelta(days=offset)
            counts = occupancy.get(day)
            occupied = counts.occupied if counts else 0
            sold = counts.sold if counts else 0
            days.append(
                DailyMetricsRow(
                    date=day,
                    occupied_room_nights=occupied,
                    room_nights_sold=sold,
                    available_room_nights=capacity,
                    occupancy_rate=_divide(decimal.Decimal(occupied), capacity, RATE_PLACES),
                    room_revenue=self._room_revenue_rows(room_revenue.get(day, []), capacity),
                    other_revenue=self._money(other_revenue.get(day, [])),
                    total_expenses=self._money(expenses.get(day, [])),
                    arrivals=arrivals.get(day, 0),
                    departures=departures.get(day, 0),
                    bookings_created=created.get(day, 0),
                    cancellations=cancelled.get(day, 0),
                )
            )

        return DailySeriesResponse(hotel_public_id=hotel.public_id, range=span, days=days)

    def revenue_breakdown(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> RevenueBreakdownResponse:
        """Ledger revenue by category and currency. Room revenue from stay nights is not
        here -- it lives in ``booking_room_nights``, not the ledger."""
        hotel = self._resolve(hotel_public_id)
        span = self._require_range(date_from, date_to)
        rows = self._repository.revenue_by_category(hotel.id, span.date_from, span.date_to)
        return RevenueBreakdownResponse(
            hotel_public_id=hotel.public_id,
            range=span,
            categories=[
                RevenueCategoryBreakdown(
                    category_code=row.code,
                    is_room_revenue=row.flag,
                    currency=row.currency,
                    amount=_amount(row.amount, MONEY_PLACES),
                    tax_amount=_amount(row.tax_amount, MONEY_PLACES),
                    entry_count=row.entry_count,
                )
                for row in rows
            ],
        )

    def expense_breakdown(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> ExpenseBreakdownResponse:
        hotel = self._resolve(hotel_public_id)
        span = self._require_range(date_from, date_to)
        rows: list[CategoryAmount] = self._repository.expenses_by_category(
            hotel.id, span.date_from, span.date_to
        )
        return ExpenseBreakdownResponse(
            hotel_public_id=hotel.public_id,
            range=span,
            categories=[
                ExpenseCategoryBreakdown(
                    category_code=row.code,
                    is_fixed_cost=row.flag,
                    currency=row.currency,
                    amount=_amount(row.amount, MONEY_PLACES),
                    tax_amount=_amount(row.tax_amount, MONEY_PLACES),
                    entry_count=row.entry_count,
                )
                for row in rows
            ],
        )

    def reviews(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> ReviewAnalyticsResponse:
        hotel = self._resolve(hotel_public_id)
        span = self._require_range(date_from, date_to)
        distribution = self._repository.rating_distribution(hotel.id, span.date_from, span.date_to)
        by_source = self._repository.reviews_by_source(hotel.id, span.date_from, span.date_to)

        return ReviewAnalyticsResponse(
            hotel_public_id=hotel.public_id,
            range=span,
            totals=self._review_totals(hotel.id, span),
            # Every bucket is emitted, including empty ones, so a chart has a stable x axis.
            rating_distribution=[
                RatingBucket(
                    bucket=bucket,
                    lower_bound=decimal.Decimal(lower),
                    upper_bound=decimal.Decimal(upper),
                    count=distribution.get(bucket, 0),
                )
                for bucket, lower, upper in RATING_BUCKETS
            ],
            by_source=[
                ReviewSourceBreakdown(
                    source=row.source,
                    review_count=row.review_count,
                    published_count=row.published_count,
                    average_rating_normalized=_quantise(row.average_rating_normalized, RATE_PLACES),
                )
                for row in by_source
            ],
        )

    # --- internals ----------------------------------------------------------------------

    def _resolve(self, hotel_public_id: uuid.UUID) -> Hotel:
        """Turn the URL identifier into the internal key, or raise 404.

        Every analytics query begins here. No repository method accepts a public id, so a
        query cannot be issued before the tenant has been established.
        """
        return self._scope.require_hotel(hotel_public_id)

    @staticmethod
    def _require_range(date_from: dt.date, date_to: dt.date) -> DateRange:
        """Validate the range and echo it back.

        Both bounds are inclusive. A reversed range is rejected rather than silently swapped:
        swapping would answer a question the caller did not ask.
        """
        if date_to < date_from:
            raise ValidationError("date_to must not be earlier than date_from.")
        days = (date_to - date_from).days + 1
        if days > MAX_RANGE_DAYS:
            raise ValidationError(
                f"The requested range spans {days} days; the maximum is {MAX_RANGE_DAYS}."
            )
        return DateRange(date_from=date_from, date_to=date_to, days=days)

    @staticmethod
    def _status_counts(counts: dict[str, int]) -> BookingStatusCounts:
        """Fill the whole vocabulary, so an absent status reads as 0 rather than vanishing."""
        return BookingStatusCounts(
            total=sum(counts.values()),
            pending=counts.get(BookingStatus.PENDING.value, 0),
            confirmed=counts.get(BookingStatus.CONFIRMED.value, 0),
            checked_in=counts.get(BookingStatus.CHECKED_IN.value, 0),
            checked_out=counts.get(BookingStatus.CHECKED_OUT.value, 0),
            cancelled=counts.get(BookingStatus.CANCELLED.value, 0),
            no_show=counts.get(BookingStatus.NO_SHOW.value, 0),
        )

    def _occupancy(self, hotel_id: int, span: DateRange) -> OccupancyMetrics:
        """Occupancy from night rows, over a denominator of current active inventory.

        The denominator's limitation is real and is reported in the payload rather than
        buried: the schema keeps no history of ``rooms.is_active`` or ``rooms.status``, so
        capacity for a past range is the hotel's inventory *as it stands now*.
        """
        counts = self._repository.occupancy_counts(hotel_id, span.date_from, span.date_to)
        capacity = self._repository.active_room_count(hotel_id) * span.days
        return OccupancyMetrics(
            occupied_room_nights=counts.occupied,
            room_nights_sold=counts.sold,
            complimentary_room_nights=counts.complimentary,
            available_room_nights=capacity,
            occupancy_rate=_divide(decimal.Decimal(counts.occupied), capacity, RATE_PLACES),
        )

    def _room_revenue(
        self, hotel_id: int, span: DateRange, available_room_nights: int
    ) -> list[RoomRevenueByCurrency]:
        rows = self._repository.room_revenue_by_currency(hotel_id, span.date_from, span.date_to)
        return self._room_revenue_rows(rows, available_room_nights)

    @staticmethod
    def _room_revenue_rows(
        rows: list[RoomRevenueRow], available_room_nights: int
    ) -> list[RoomRevenueByCurrency]:
        """ADR and RevPAR, per currency, exactly as the generated columns define them.

        ADR divides by nights SOLD (complimentary excluded); RevPAR divides by nights
        AVAILABLE. Both are null when their denominator is zero.
        """
        return [
            RoomRevenueByCurrency(
                currency=row.currency,
                room_revenue=_amount(row.room_revenue, MONEY_PLACES),
                adr=_divide(row.room_revenue, row.room_nights_sold, MONEY_PLACES),
                revpar=_divide(row.room_revenue, available_room_nights, MONEY_PLACES),
            )
            for row in rows
        ]

    def _review_totals(self, hotel_id: int, span: DateRange) -> ReviewMetrics:
        total, published, average = self._repository.review_totals(
            hotel_id, span.date_from, span.date_to
        )
        return ReviewMetrics(
            review_count=total,
            published_count=published,
            average_rating_normalized=_quantise(average, RATE_PLACES),
        )

    @staticmethod
    def _money(rows: list[CurrencyAmount]) -> list[MoneyByCurrency]:
        """Repository tuples to response buckets, sorted by currency for determinism."""
        return [
            MoneyByCurrency(currency=row.currency, amount=_amount(row.amount, MONEY_PLACES))
            for row in sorted(rows, key=lambda row: row.currency)
        ]

    @staticmethod
    def _combine(*groups: list[MoneyByCurrency]) -> list[MoneyByCurrency]:
        """Add buckets **within** each currency. Nothing crosses a currency boundary."""
        totals: dict[str, decimal.Decimal] = {}
        for group in groups:
            for bucket in group:
                totals[bucket.currency] = totals.get(bucket.currency, ZERO) + bucket.amount
        return [
            MoneyByCurrency(currency=currency, amount=totals[currency])
            for currency in sorted(totals)
        ]

    @staticmethod
    def _subtract(
        left: list[MoneyByCurrency], right: list[MoneyByCurrency]
    ) -> list[MoneyByCurrency]:
        """left - right, per currency.

        A currency on only one side still appears; the absent side counts as zero **for that
        currency**, which is not an FX assumption but the plain fact that there were no
        entries. Nothing is converted or merged.
        """
        totals: dict[str, decimal.Decimal] = {b.currency: b.amount for b in left}
        for bucket in right:
            totals[bucket.currency] = totals.get(bucket.currency, ZERO) - bucket.amount
        return [
            MoneyByCurrency(currency=currency, amount=totals[currency])
            for currency in sorted(totals)
        ]


__all__ = ["MAX_RANGE_DAYS", "RATING_BUCKETS", "AnalyticsService"]
