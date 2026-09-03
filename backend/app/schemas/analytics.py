"""Analytics API contracts: read-only KPI projections.

**Every monetary figure is a list of per-currency buckets, never a scalar.** `revenue` and
`expenses` each carry their own `currency` column, and room revenue inherits the currency of
its booking, so a hotel can legitimately hold EUR, USD and JPY lines in one date range.
Summing those would produce a number that is not money. No FX conversion happens anywhere in
this stage, and the hotel's own currency is never assumed to be the answer.

**Nothing here is a raw database row.** No internal BIGINT appears in any field, and every
metric below has a definition recorded in ``docs/analytics-design.md`` naming its source
table and its date column.

**Ratios are nullable, deliberately.** They mirror the ``NULLIF`` guards on
``daily_hotel_metrics``'s own generated columns: a hotel with no sold room nights has an
*undefined* ADR, not one of zero. Reporting zero would drag every average down.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from pydantic import BaseModel, ConfigDict, Field

#: The widest range a single request may span. A year plus a day covers any calendar year,
#: and bounds the daily series so one request cannot ask for an unbounded response.
MAX_RANGE_DAYS = 366


class MoneyByCurrency(BaseModel):
    """One currency's worth of a monetary metric.

    Buckets are always returned sorted by currency code so a response is byte-deterministic
    for the same data.
    """

    model_config = ConfigDict(from_attributes=True)

    currency: str
    amount: decimal.Decimal


class RoomRevenueByCurrency(BaseModel):
    """Room revenue and its two derived rates, within one currency.

    ADR and RevPAR are per-currency for the same reason the totals are: their numerator is.
    """

    model_config = ConfigDict(from_attributes=True)

    currency: str
    #: SUM(booking_room_nights.rate) over occupied nights in range, in this currency.
    room_revenue: decimal.Decimal
    #: room_revenue / room_nights_sold. Null when no room nights were sold in this currency.
    adr: decimal.Decimal | None
    #: room_revenue / available_room_nights. Null when the hotel has no active rooms.
    revpar: decimal.Decimal | None


class DateRange(BaseModel):
    """The range actually applied, echoed so a response is self-describing.

    Both bounds are **inclusive**. For stay-night metrics that means a night whose
    ``stay_date`` equals ``date_to`` is counted; a booking whose ``check_out_date`` equals
    ``date_to`` contributes a departure but no night, because the schema's own CHECK requires
    ``stay_date < check_out_date``.
    """

    date_from: dt.date
    date_to: dt.date
    #: Inclusive day count: date_to - date_from + 1.
    days: int


class BookingStatusCounts(BaseModel):
    """A set of bookings, split by their status *now*.

    Status is current, not historical: the schema keeps no status history, so a booking
    created in January and cancelled in March counts as cancelled wherever it appears.
    """

    total: int
    pending: int
    confirmed: int
    checked_in: int
    checked_out: int
    cancelled: int
    no_show: int


class OccupancyMetrics(BaseModel):
    """Room-night occupancy, counted from actual night rows rather than booking headers."""

    #: COUNT of booking_room_nights whose stay_date falls in range and whose allocation is in
    #: an occupancy-bearing status. Complimentary nights are included: the room was occupied.
    occupied_room_nights: int
    #: Occupied nights excluding is_complimentary -- the ADR denominator.
    room_nights_sold: int
    #: Occupied nights that were complimentary. Surfaced rather than silently dropped.
    complimentary_room_nights: int
    #: active rooms at the hotel x days in range. See the limitation note below.
    available_room_nights: int
    #: occupied_room_nights / available_room_nights, mirroring the generated column on
    #: daily_hotel_metrics. Null when the hotel has no active rooms.
    occupancy_rate: decimal.Decimal | None
    #: **Limitation, stated in the payload rather than only in the docs.** The schema records
    #: no history for rooms.status or rooms.is_active, so the denominator uses the hotel's
    #: CURRENT active room count. For a past range it is exact only if inventory has not
    #: changed since.
    available_room_nights_basis: str = "current_active_rooms"


class StayFlowMetrics(BaseModel):
    """Arrivals, departures and cancellations, each on its own date column."""

    #: bookings.check_in_date within range.
    arrivals: int
    #: bookings.check_out_date within range.
    departures: int
    #: bookings.cancelled_at::date within range.
    cancellations: int


class ReviewMetrics(BaseModel):
    """Review analytics over ``reviews.review_date``.

    Reviews with no guest and no booking are included: the schema permits them, and excluding
    them would silently drop every review harvested from an external platform.
    """

    review_count: int
    published_count: int
    #: AVG(rating_normalized), the PostgreSQL-generated rating / rating_scale. This is the
    #: only average comparable across sources that score out of five and out of ten. Null
    #: when the range holds no reviews.
    average_rating_normalized: decimal.Decimal | None


class RatingBucket(BaseModel):
    """One fifth of the normalized rating scale.

    An analytics-layer presentation of ``rating_normalized``, not a schema concept: bucket *n*
    covers ``(n-1)/5 <= rating_normalized <= n/5``, with the upper bound exclusive except in
    bucket 5. Stated so the numbers can be reproduced exactly.
    """

    bucket: int = Field(ge=1, le=5)
    lower_bound: decimal.Decimal
    upper_bound: decimal.Decimal
    count: int


class ReviewSourceBreakdown(BaseModel):
    """One review channel's share, ordered by source code for determinism."""

    source: str
    review_count: int
    published_count: int
    average_rating_normalized: decimal.Decimal | None


class ReviewAnalyticsResponse(BaseModel):
    """The reviews endpoint: totals, distribution and channel mix."""

    hotel_public_id: uuid.UUID
    range: DateRange
    totals: ReviewMetrics
    rating_distribution: list[RatingBucket]
    by_source: list[ReviewSourceBreakdown]


class RevenueCategoryBreakdown(BaseModel):
    """One revenue category within one currency.

    ``is_room_revenue`` is carried through so a consumer can apply the schema's own exclusion
    rule rather than re-deriving it from a category name.
    """

    category_code: str
    is_room_revenue: bool
    currency: str
    amount: decimal.Decimal
    tax_amount: decimal.Decimal
    entry_count: int


class ExpenseCategoryBreakdown(BaseModel):
    """One expense category within one currency."""

    category_code: str
    is_fixed_cost: bool
    currency: str
    amount: decimal.Decimal
    tax_amount: decimal.Decimal
    entry_count: int


class RevenueBreakdownResponse(BaseModel):
    """Ledger revenue grouped by category and currency, ordered by (code, currency)."""

    hotel_public_id: uuid.UUID
    range: DateRange
    categories: list[RevenueCategoryBreakdown]


class ExpenseBreakdownResponse(BaseModel):
    """Ledger expenses grouped by category and currency, ordered by (code, currency)."""

    hotel_public_id: uuid.UUID
    range: DateRange
    categories: list[ExpenseCategoryBreakdown]


class OverviewResponse(BaseModel):
    """The hotel KPI snapshot for one date range.

    Room revenue and ledger revenue are kept apart on purpose. Approved decision 21 makes
    ``booking_room_nights`` the single source of truth for room revenue; the ledger carries
    the other streams. A ledger line posted against an ``is_room_revenue`` category is
    reported in its own bucket rather than being folded into either total, because adding it
    to ``room_revenue`` would double-count and adding it to ``other_revenue`` would defeat the
    flag's entire purpose.
    """

    hotel_public_id: uuid.UUID
    range: DateRange

    #: Bookings whose ``booked_at`` falls in the range -- demand as it was *taken*. This is
    #: the column ``daily_hotel_metrics.bookings_created`` counts.
    bookings_created: BookingStatusCounts
    #: Bookings whose STAY overlaps the range -- occupancy as it is *served*. Both are given
    #: because neither answers the other's question, and reporting only creation would show
    #: zero for any forward-looking window.
    bookings_by_stay: BookingStatusCounts
    stay_flow: StayFlowMetrics
    occupancy: OccupancyMetrics

    #: From booking_room_nights.rate, by the currency of the owning booking.
    room_revenue: list[RoomRevenueByCurrency]
    #: From the revenue ledger, categories where is_room_revenue is false.
    other_revenue: list[MoneyByCurrency]
    #: From the revenue ledger, categories where is_room_revenue is TRUE. Reported separately
    #: and added to nothing -- see the class docstring.
    ledger_room_revenue: list[MoneyByCurrency]
    #: From the expense ledger.
    total_expenses: list[MoneyByCurrency]
    #: (room_revenue + other_revenue) - total_expenses, **within each currency only**. A
    #: currency present on one side and absent on the other still appears, with the missing
    #: side treated as zero for that currency; it is never converted or merged.
    net_operating_result: list[MoneyByCurrency]
    #: True when more than one currency appears anywhere above. A consumer that wants a
    #: single headline number must decide how to handle it; this API will not guess.
    is_multi_currency: bool

    reviews: ReviewMetrics


class DailyMetricsRow(BaseModel):
    """One calendar day. Every day in the range is present, including empty ones.

    Only metrics that are correct at day granularity appear. Booking status counts are
    absent: status is current rather than historical, so attributing today's status to the
    day a booking was created would misreport every past day.
    """

    date: dt.date

    occupied_room_nights: int
    room_nights_sold: int
    available_room_nights: int
    occupancy_rate: decimal.Decimal | None

    room_revenue: list[RoomRevenueByCurrency]
    other_revenue: list[MoneyByCurrency]
    total_expenses: list[MoneyByCurrency]

    arrivals: int
    departures: int
    bookings_created: int
    cancellations: int


class DailySeriesResponse(BaseModel):
    """A chronologically ordered, gap-free daily series.

    Gap-free matters: a chart that silently omits zero days draws a misleading line, and a
    future forecasting stage needs a regular series.
    """

    hotel_public_id: uuid.UUID
    range: DateRange
    days: list[DailyMetricsRow]


__all__ = [
    "MAX_RANGE_DAYS",
    "BookingStatusCounts",
    "DailyMetricsRow",
    "DailySeriesResponse",
    "DateRange",
    "ExpenseBreakdownResponse",
    "ExpenseCategoryBreakdown",
    "MoneyByCurrency",
    "OccupancyMetrics",
    "OverviewResponse",
    "RatingBucket",
    "RevenueBreakdownResponse",
    "RevenueCategoryBreakdown",
    "ReviewAnalyticsResponse",
    "ReviewMetrics",
    "ReviewSourceBreakdown",
    "RoomRevenueByCurrency",
    "StayFlowMetrics",
]
