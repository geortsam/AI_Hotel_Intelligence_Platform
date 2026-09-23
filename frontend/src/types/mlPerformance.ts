/**
 * The Stage 7.3 forecast-performance contracts, and the Stage 6.11 stored-prediction row.
 *
 * Transcribed from the API, not invented. Every field below exists in the OpenAPI document; a
 * field that is absent here is absent there, and the feature is required to render what it was
 * given rather than fill a gap.
 *
 * ## What these responses are, and what they are not
 *
 * They are **measurements taken under a protocol that was fixed and checksummed before any
 * number was computed**. They are not evidence that the model is accurate. Every response
 * carries {@link MeasurementMetadata} saying so in its own payload, including a prose
 * `statement` the UI renders verbatim rather than paraphrasing — a paraphrase is a claim, and
 * the point of the field is that the claim boundary is the server's to set.
 *
 * ## Two fields the API deliberately does not send
 *
 * `canonical_model_digest` and the per-prediction digest lists stop at the API boundary. They
 * are absent here for the same reason: there is nothing for the browser to receive.
 *
 * ## No paired series
 *
 * Neither response carries a per-date series, a realised value or a predicted value. The
 * accuracy response is aggregate error; the distribution response is summary statistics. The
 * forecast-vs-actual chart is therefore built from two *other* endpoints — see
 * `useForecastPerformance` — and nothing in this file is used to reconstruct one.
 */

import type { DecimalString } from '@/types/analytics'

/** The rules a set of figures was produced under, and what they do not establish. */
export interface MeasurementMetadata {
  /** e.g. `accuracy_v1`. */
  readonly protocol_version: string
  /** SHA-256 over the protocol's own values. Same checksum, same rules. */
  readonly protocol_checksum: string
  /** Always `false`. Rendered, never interpreted. */
  readonly establishes_production_accuracy: boolean
  /** The claim boundary in prose. Rendered verbatim. */
  readonly statement: string
}

/* --- accuracy --------------------------------------------------------------------------- */

/**
 * One segment's error and its denominator.
 *
 * Every field is `null` rather than `0` when nothing was scored: a segment with no
 * observations has no error, and rendering zero would read as a perfect one.
 */
export interface AccuracyMetrics {
  readonly observations: number
  /** Eligible predictions with no recorded occupancy to score against. */
  readonly skipped: number
  /** Mean absolute error, in room nights. */
  readonly mae: number | null
  readonly rmse: number | null
  /** Symmetric mean absolute percentage error. */
  readonly smape: number | null
}

export interface SegmentAccuracy {
  /** `below_calibration` or `within_calibration`. */
  readonly segment: string
  readonly metrics: AccuracyMetrics
}

/**
 * One model version's error, split into two segments that never share a denominator.
 *
 * There is no combined field here, in the API or in this type. That is deliberate on both
 * sides: a single headline accuracy figure across the two segments would average populations
 * the protocol separates precisely because the model behaves differently across them.
 */
export interface ModelVersionAccuracy {
  readonly model_version: string
  readonly below_calibration: SegmentAccuracy
  readonly within_calibration: SegmentAccuracy
}

export interface ForecastAccuracyResponse {
  readonly hotel_public_id: string
  readonly as_of_date: string
  readonly window_from: string
  readonly window_to: string
  /** Null when nothing in the window had cleared the settlement lag by `as_of_date`. */
  readonly scored_from: string | null
  readonly scored_to: string | null
  /** Days a target date must be past before it is scored. Rendered on screen, not hidden. */
  readonly settlement_lag_days: number
  readonly candidates: number
  readonly ineligible_by_settlement: number
  readonly out_of_scope_model_digest: number
  readonly unsettled_allocations: number
  /** False means an allocation covering the scored window can still change: provisional. */
  readonly settled: boolean
  readonly by_model_version: readonly ModelVersionAccuracy[]
  readonly measurement: MeasurementMetadata
}

/* --- distribution ----------------------------------------------------------------------- */

export interface QuantileValue {
  /** The protocol's label, e.g. `p05`. */
  readonly label: string
  readonly value: number | null
}

export interface FieldSummary {
  /** A model input column, or `predicted_room_nights`. */
  readonly field: string
  readonly count: number
  readonly minimum: number | null
  readonly maximum: number | null
  readonly mean: number | null
  readonly median: number | null
  readonly quantiles: readonly QuantileValue[]
}

export interface SegmentSummary {
  readonly segment: string
  readonly observations: number
  readonly fields: readonly FieldSummary[]
}

export interface ModelVersionSummary {
  readonly model_version: string
  readonly below_calibration: SegmentSummary
  readonly within_calibration: SegmentSummary
}

export interface WindowSummary {
  readonly window_from: string
  readonly window_to: string
  readonly candidates: number
  readonly out_of_scope_model_digest: number
  readonly by_model_version: readonly ModelVersionSummary[]
}

/** Target minus baseline, per statistic. `null` wherever either side had nothing. */
export interface FieldDifference {
  readonly field: string
  /** May be negative. */
  readonly count: number
  readonly minimum: number | null
  readonly maximum: number | null
  readonly mean: number | null
  readonly median: number | null
  readonly quantiles: readonly QuantileValue[]
}

export interface SegmentDifference {
  readonly segment: string
  readonly observations: number
  readonly fields: readonly FieldDifference[]
}

export interface ModelVersionDifference {
  readonly model_version: string
  readonly below_calibration: SegmentDifference
  readonly within_calibration: SegmentDifference
}

export interface DistributionComparison {
  readonly baseline: WindowSummary
  readonly by_model_version: readonly ModelVersionDifference[]
}

/**
 * What the stored predictions and their inputs looked like, and optionally how far they moved.
 *
 * **This describes; it does not decide.** There is no threshold, verdict, alert or drift field
 * in this contract, because the protocol behind it contains no such rule. A difference here is
 * a difference — the UI must not present it as drift.
 */
export interface PredictionDistributionResponse {
  readonly hotel_public_id: string
  readonly observed: WindowSummary
  /** Null when no baseline window was requested. Never a fabricated set of zero differences. */
  readonly comparison: DistributionComparison | null
  readonly measurement: MeasurementMetadata
}

/* --- stored predictions (Stage 6.11) ---------------------------------------------------- */

/**
 * One prediction this hotel was served, as it was stored.
 *
 * Ten fields, exhaustive. `feature_values`, `feature_digest`, `canonical_model_digest` and
 * `request_id` are withheld by the API and so are absent here.
 *
 * **A `target_date` may carry more than one row.** `uq_demand_predictions_identity` includes
 * `feature_digest`, so a booking recorded late legitimately produces a second prediction for
 * the same day beside the first rather than instead of it. The server owns the rule for
 * choosing between them when measuring; this feature does not reimplement it.
 */
export interface StoredDemandPrediction {
  readonly public_id: string
  readonly target_date: string
  readonly forecast_horizon_days: number
  readonly prediction_cutoff: string
  /** Room nights, as a JSON number. A modelled quantity, never money. */
  readonly predicted_room_nights: number
  readonly model_name: string
  readonly model_version: string
  readonly feature_version: string
  readonly dataset_version: string
  readonly generated_at: string
}

/** Re-exported so the feature can name a server decimal without importing two modules. */
export type { DecimalString }
