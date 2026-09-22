/**
 * The served demand model's response shape, as `GET /hotels/{id}/ml/demand-forecast` sends it.
 *
 * Stage 7.2 is the first screen to read this endpoint at all: the trained model has been served
 * since Stage 6.6 and packaged into the API image since 6.7, and until now nothing in the browser
 * asked it anything.
 *
 * **This is a model estimate, not a booking.** Every field below is carried so the number on
 * screen can be attributed — which model version, fitted on which dataset, at what cutoff — and
 * the UI is required to say so. The model's own metadata reports `production_ready: false` and
 * `status: 'offline_research_candidate'`, and the component that renders it shows that rather
 * than hiding it.
 */

/** Identity and provenance of the model that produced a prediction. */
export interface DemandModelMetadata {
  readonly model_name: string
  readonly model_version: string
  readonly feature_version: string
  readonly dataset_version: string
  /** `offline_research_candidate` today. Rendered, not interpreted. */
  readonly status: string
  /** `false` today. The UI states this; it does not decide what it means. */
  readonly production_ready: boolean
  /** One paragraph from the server describing how the estimate is produced. */
  readonly methodology: string
}

export interface DemandPredictionResponse {
  readonly hotel_public_id: string
  /** `YYYY-MM-DD`, the day being forecast. */
  readonly target_date: string
  readonly forecast_horizon_days: number
  /** The last day whose realised demand the model was allowed to see. */
  readonly cutoff_date: string
  /** The instant after which no booking influenced the estimate, ISO-8601 UTC. */
  readonly prediction_cutoff: string
  /**
   * The estimate, as a JSON number.
   *
   * A float here is correct and is not the money rule being bent: this is a modelled quantity
   * of room nights, not a ledger amount. No money value in this application is ever a float.
   */
  readonly predicted_room_nights: number
  readonly model: DemandModelMetadata
  /** The feature columns the model consumed, in its canonical order. */
  readonly features_used: readonly string[]
}
