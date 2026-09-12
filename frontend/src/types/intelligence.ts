/**
 * The Intelligence contract, transcribed from `app/schemas/intelligence.py`.
 *
 * Five read-only, hotel-scoped endpoints under
 * `/hotels/{hotel_public_id}/intelligence/...`. Nothing here mutates anything, and there is
 * no flat portfolio-wide route: a cross-hotel forecast would sit outside the segment where
 * tenant isolation is established.
 *
 * ## Predictions are never mixed with facts
 *
 * A hotel already knows part of its future — next month's bookings exist in the database
 * today. Every forecast day therefore carries an **actual** figure
 * (`on_the_books_room_nights`, `on_the_books_room_revenue`) **beside** the predicted one,
 * never blended into it. One is a confirmed reservation; the other is an estimate. The UI
 * keeps them apart for the same reason the API does.
 *
 * ## There is no language model here
 *
 * `Insight.explanation` is a deterministic template filled from the numbers in
 * `supporting_metrics`. The same data always produces the same sentence. Nothing on this
 * screen is generated prose, and the UI must never present it as such.
 *
 * ## Every number arrives as a string
 *
 * Decimals are JSON strings so a `Decimal` keeps its scale. They are displayed as strings
 * and converted to a number at exactly one boundary — a chart coordinate — following the
 * convention Stage 5.4 established for the dashboard's trend chart.
 */

/** `ForecastMethodLiteral`. A single response can mix methods across its days. */
export type ForecastMethod = 'seasonal_dow_median' | 'overall_median' | 'insufficient_data'

/** `TrendDirectionLiteral`. */
export type TrendDirection = 'increasing' | 'decreasing' | 'stable' | 'insufficient_data'

/** `SeverityLiteral`. Carried by insights only — an anomaly has no severity. */
export type InsightSeverity = 'info' | 'warning' | 'critical'

/**
 * `ModelMetadata` — everything needed to reproduce a prediction, minus the data.
 *
 * `generated_at` is provenance, not an input: it is the only field that differs between two
 * otherwise identical requests.
 */
export interface ModelMetadata {
  readonly model_name: string
  readonly model_version: string
  /** A one-line description of the arithmetic, so the response explains itself. */
  readonly methodology: string
  readonly generated_at: string
}

/**
 * `TrainingWindow` — the history a forecast was fitted on.
 *
 * It always ends strictly **before** the first forecast day. That boundary is the leakage
 * guard, and the UI states it rather than leaving a reader to infer it.
 */
export interface TrainingWindow {
  readonly date_from: string
  readonly date_to: string
  readonly days: number
  readonly observations: number
}

/** `ForecastHorizon`. Both bounds inclusive. */
export interface ForecastHorizon {
  readonly date_from: string
  readonly date_to: string
  readonly days: number
}

/** `ObservationWindow` — the past span a trend or anomaly scan looked at. Both inclusive. */
export interface ObservationWindow {
  readonly date_from: string
  readonly date_to: string
  readonly days: number
  readonly observations: number
}

/** `OccupancyForecastPoint`. */
export interface OccupancyForecastPoint {
  readonly date: string
  /** **Actual.** Room nights already on the books — a fact, not a prediction. */
  readonly on_the_books_room_nights: number
  /** Capacity, on a current-inventory basis. */
  readonly available_room_nights: number
  /** **Predicted.** Null where the training window held no usable history. */
  readonly predicted_room_nights: string | null
  readonly predicted_occupancy_rate: string | null
  /** Robust prediction interval. Null alongside a null prediction. */
  readonly interval_lower: string | null
  readonly interval_upper: string | null
  readonly confidence_level: string | null
  readonly method: ForecastMethod
  readonly observations: number
  /** True when the prediction was reduced to available capacity rather than published above it. */
  readonly capacity_clamped: boolean
}

export interface OccupancyForecast {
  readonly hotel_public_id: string
  readonly model: ModelMetadata
  readonly training_window: TrainingWindow
  readonly horizon: ForecastHorizon
  readonly points: readonly OccupancyForecastPoint[]
}

/** `RevenueForecastPoint` — one day within one currency. */
export interface RevenueForecastPoint {
  readonly date: string
  /** **Actual**, from `booking_room_nights.rate` — never payments, never a list price. */
  readonly on_the_books_room_revenue: string
  readonly predicted_room_revenue: string | null
  readonly interval_lower: string | null
  readonly interval_upper: string | null
  readonly confidence_level: string | null
  readonly method: ForecastMethod
  readonly observations: number
}

/**
 * `CurrencyForecast` — an independent forecast for one currency.
 *
 * Currencies are forecast separately and **never combined**. There is no FX rate anywhere in
 * this codebase, and the hotel's own currency is not a conversion target.
 */
export interface CurrencyForecast {
  readonly currency: string
  readonly points: readonly RevenueForecastPoint[]
}

export interface RevenueForecast {
  readonly hotel_public_id: string
  readonly model: ModelMetadata
  readonly training_window: TrainingWindow
  readonly horizon: ForecastHorizon
  /** One entry per currency **with history**. A currency never traded in is absent, not zero. */
  readonly currencies: readonly CurrencyForecast[]
  readonly is_multi_currency: boolean
}

/**
 * `DemandTrendResponse`.
 *
 * The window is split in half and the halves' medians compared. Both medians and the
 * threshold are returned, so the classification can be recomputed by hand — which is why the
 * UI shows them rather than only the verdict.
 *
 * `relative_change` is null when the earlier half's median is zero: a ratio against zero is
 * undefined, and the backend says so instead of reporting an infinite rise.
 */
export interface DemandTrend {
  readonly hotel_public_id: string
  readonly model: ModelMetadata
  readonly window: ObservationWindow
  /** Counted by `bookings.booked_at` — demand as it was *taken*. */
  readonly metric: string
  readonly direction: TrendDirection
  readonly earlier_median: string | null
  readonly recent_median: string | null
  readonly relative_change: string | null
  readonly threshold: string
}

/**
 * `AnomalyPoint` — one flagged day, carrying the whole statistical basis for the flag.
 *
 * **There is no severity field.** The backend flags or does not flag; it does not grade. A
 * severity invented here would be a frontend heuristic wearing the server's authority.
 */
export interface AnomalyPoint {
  /** e.g. `occupied_room_nights`, `bookings_created`, `room_revenue[EUR]`. */
  readonly metric: string
  readonly date: string
  readonly value: string
  /** The window median the value was judged against. */
  readonly median: string
  /** The window's median absolute deviation — the robust spread. */
  readonly median_absolute_deviation: string
  /** `0.6745 * (value - median) / MAD`. */
  readonly modified_z_score: string
  readonly threshold: string
  readonly direction: 'above' | 'below'
}

export interface AnomalyReport {
  readonly hotel_public_id: string
  readonly model: ModelMetadata
  readonly window: ObservationWindow
  /** Named so an empty result is unambiguous: nothing was unusual, not nothing was examined. */
  readonly metrics_scanned: readonly string[]
  readonly anomalies: readonly AnomalyPoint[]
}

/** `SupportingMetric`. Values are strings so a Decimal keeps its scale and a count stays a count. */
export interface SupportingMetric {
  readonly name: string
  readonly value: string
  /** Present only where the metric is monetary; absent elsewhere rather than defaulted. */
  readonly currency?: string | null
}

/** `Insight` — one structured, deterministic finding. Not generated text. */
export interface Insight {
  /** `data_sufficiency` | `demand_trend` | `anomaly` | `occupancy_outlook`, observed live. */
  readonly type: string
  readonly severity: InsightSeverity
  readonly title: string
  readonly explanation: string
  readonly supporting_metrics: readonly SupportingMetric[]
  readonly date_from: string
  readonly date_to: string
  /** Present where the finding rests on a model with a stated confidence; null for a fact. */
  readonly confidence: string | null
}

export interface InsightsReport {
  readonly hotel_public_id: string
  readonly model: ModelMetadata
  readonly window: ObservationWindow
  readonly horizon: ForecastHorizon
  readonly insights: readonly Insight[]
}

/* --- the bounds the routers declare ------------------------------------------------------ */

/** `MIN_TRAINING_DAYS` / `MAX_TRAINING_DAYS` / `DEFAULT_TRAINING_DAYS`. */
export const MIN_TRAINING_DAYS = 14
export const MAX_TRAINING_DAYS = 365
export const DEFAULT_TRAINING_DAYS = 90

/** `MAX_HORIZON_DAYS` / `DEFAULT_HORIZON_DAYS`. 91 days is a 422, verified live. */
export const MAX_HORIZON_DAYS = 90
export const DEFAULT_HORIZON_DAYS = 7

/** `MAX_OBSERVATION_DAYS`. A 376-day window is a 422, verified live. */
export const MAX_OBSERVATION_DAYS = 366
