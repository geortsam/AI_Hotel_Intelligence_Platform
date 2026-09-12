/**
 * The analytics contracts, transcribed from `app.schemas.analytics`.
 *
 * Two responses only -- overview and the daily series -- because those are the two the
 * dashboard consumes. The breakdown and review endpoints exist and are deliberately absent
 * here; a type for a call nobody makes is dead weight that still has to be kept in step
 * with the backend.
 *
 * Confirmed against the live API (`GET /openapi.json` plus real payloads) rather than read
 * from the source alone. That check changed this file twice, which is the argument for
 * making it: `occupancy_rate` is a **fraction**, not a percentage, and every NUMERIC arrives
 * as a JSON **string**.
 */

/**
 * A backend `Decimal`, on the wire.
 *
 * Pydantic serialises `decimal.Decimal` as a JSON string -- `"9750.00"`, `"0.5804"` -- and
 * that is load-bearing, not incidental. Money is `NUMERIC(14,2)` in PostgreSQL; parsing it
 * into a JavaScript `number` would put a 64-bit binary float in the middle of a financial
 * figure, where 0.1 + 0.2 is famously not 0.3.
 *
 * So the string is carried unchanged, and only the formatter at the very edge turns it into
 * something a person reads. Nothing in this application does arithmetic on money.
 */
export type DecimalString = string

/** One currency's worth of a monetary metric. Never summed with another currency's. */
export interface MoneyByCurrency {
  readonly currency: string
  readonly amount: DecimalString
}

/**
 * Room revenue and its two derived rates, within one currency.
 *
 * `adr` and `revpar` are nullable because the backend mirrors the `NULLIF` guards on
 * `daily_hotel_metrics`: a hotel with no sold room nights has an **undefined** ADR, not one
 * of zero. Reporting zero would drag every average down, which is precisely why the null
 * exists and why the UI must render it as "unavailable" rather than as a number.
 */
export interface RoomRevenueByCurrency {
  readonly currency: string
  /** SUM(booking_room_nights.rate) over occupied nights in range, in this currency. */
  readonly room_revenue: DecimalString
  /** room_revenue / room_nights_sold. Null when no room nights were sold. */
  readonly adr: DecimalString | null
  /** room_revenue / available_room_nights. Null when the hotel has no active rooms. */
  readonly revpar: DecimalString | null
}

/** The range the backend actually applied, echoed so a response is self-describing. */
export interface DateRange {
  /** Inclusive start, `YYYY-MM-DD`. */
  readonly date_from: string
  /** Inclusive end, `YYYY-MM-DD`. */
  readonly date_to: string
  /** Inclusive day count: date_to - date_from + 1. */
  readonly days: number
}

/** A set of bookings split by their status *now*. The schema keeps no status history. */
export interface BookingStatusCounts {
  readonly total: number
  readonly pending: number
  readonly confirmed: number
  readonly checked_in: number
  readonly checked_out: number
  readonly cancelled: number
  readonly no_show: number
}

/** Room-night occupancy, counted from night rows rather than booking headers. */
export interface OccupancyMetrics {
  readonly occupied_room_nights: number
  /** Occupied nights excluding complimentary ones -- the ADR denominator. */
  readonly room_nights_sold: number
  readonly complimentary_room_nights: number
  /** Active rooms x days in range. See `available_room_nights_basis`. */
  readonly available_room_nights: number
  /**
   * occupied_room_nights / available_room_nights, as a **fraction in [0, 1]**.
   *
   * Not a percentage. `"0.5804"` means 58.04%, and multiplying by 100 is a presentation
   * step performed once, in the formatter.
   */
  readonly occupancy_rate: DecimalString | null
  /**
   * How the denominator was obtained. The backend states its own limitation in the payload:
   * the schema records no history for `rooms.is_active`, so capacity for a past range is the
   * hotel's inventory *as it stands now*. Surfaced in the UI rather than quietly dropped.
   */
  readonly available_room_nights_basis: string
}

/** Arrivals, departures and cancellations, each on its own date column. */
export interface StayFlowMetrics {
  readonly arrivals: number
  readonly departures: number
  readonly cancellations: number
}

/** Review analytics over `reviews.review_date`. */
export interface ReviewMetrics {
  readonly review_count: number
  readonly published_count: number
  /**
   * AVG(rating_normalized) -- rating / rating_scale -- as a fraction in [0, 1].
   *
   * The only average comparable across sources that score out of five and out of ten. Null
   * when the range holds no reviews.
   */
  readonly average_rating_normalized: DecimalString | null
}

/** `GET /hotels/{id}/analytics/overview`. */
export interface OverviewResponse {
  readonly hotel_public_id: string
  readonly range: DateRange

  /** Bookings whose `booked_at` falls in range -- demand as it was *taken*. */
  readonly bookings_created: BookingStatusCounts
  /** Bookings whose STAY overlaps the range -- occupancy as it is *served*. */
  readonly bookings_by_stay: BookingStatusCounts
  readonly stay_flow: StayFlowMetrics
  readonly occupancy: OccupancyMetrics

  /**
   * From `booking_room_nights.rate`. **Empty when the range holds no occupied nights** --
   * which is not the same as a zero bucket, and is why the UI has an "unavailable" state
   * distinct from "0.00".
   */
  readonly room_revenue: readonly RoomRevenueByCurrency[]
  /** Ledger revenue from categories where `is_room_revenue` is false. */
  readonly other_revenue: readonly MoneyByCurrency[]
  /**
   * Ledger revenue from categories where `is_room_revenue` is TRUE.
   *
   * Reported separately and added to nothing. Folding it into `room_revenue` would
   * double-count against `booking_room_nights`, which approved decision 21 makes the single
   * source of truth for room revenue.
   */
  readonly ledger_room_revenue: readonly MoneyByCurrency[]
  readonly total_expenses: readonly MoneyByCurrency[]
  /** (room_revenue + other_revenue) - total_expenses, **within each currency only**. */
  readonly net_operating_result: readonly MoneyByCurrency[]
  /** True when more than one currency appears anywhere above. */
  readonly is_multi_currency: boolean

  readonly reviews: ReviewMetrics
}

/** One calendar day. Every day in the range is present, including empty ones. */
export interface DailyMetricsRow {
  readonly date: string
  readonly occupied_room_nights: number
  readonly room_nights_sold: number
  readonly available_room_nights: number
  readonly occupancy_rate: DecimalString | null
  readonly room_revenue: readonly RoomRevenueByCurrency[]
  readonly other_revenue: readonly MoneyByCurrency[]
  readonly total_expenses: readonly MoneyByCurrency[]
  readonly arrivals: number
  readonly departures: number
  readonly bookings_created: number
  readonly cancellations: number
}

/**
 * `GET /hotels/{id}/analytics/daily`.
 *
 * Gap-free and ascending, including days with no activity. That guarantee is what lets the
 * chart plot the array directly: a series that silently omitted empty days would draw a
 * line implying activity that never happened.
 */
export interface DailySeriesResponse {
  readonly hotel_public_id: string
  readonly range: DateRange
  readonly days: readonly DailyMetricsRow[]
}
