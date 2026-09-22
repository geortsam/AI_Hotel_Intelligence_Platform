import { api } from '@/services/api/client'
import type { DemandPredictionResponse } from '@/types/ml'

/**
 * The served demand model, as the browser reaches it.
 *
 * One call. The ML router carries two routes and only one of them reaches a model: the
 * serving route declares a `503` for an unavailable artifact, and `demand-predictions` — the
 * Stage 6.11 read API over stored rows — declares none, because it loads none. Stage 7.2 shows
 * a forecast, so it wraps the serving route and not the reader.
 *
 * Transport, headers and error translation stay in the API client, as with every other service
 * here. This module knows the path, the parameters and what comes back.
 *
 * **Hotel-scoped, and that is the tenant boundary.** As with analytics there is no flat
 * `/ml/...`: the hotel segment is where isolation is established, and a caller who is not a
 * member receives `404`, byte-identical to the response for a hotel that does not exist.
 *
 * ## The failures are ordinary answers, not faults
 *
 * Three of the four documented responses are things a working system says routinely, and the
 * caller is expected to render each differently:
 *
 * * `422 INSUFFICIENT_HISTORY` — the hotel has not recorded occupancy on the days the model's
 *   lag features read. Common for a new property, and **not an error**: the model declines
 *   rather than guessing, which is the behaviour the serving stage was built to have.
 * * `503 MODEL_UNAVAILABLE` — no verified artifact in this runtime.
 * * `422` for an unsupported `horizon_days` — the served model forecasts one horizon.
 *
 * This module does not translate them. `ApiError` already carries the code, and the feature
 * decides what a reader should see.
 */

/** What the serving route needs. `horizon_days` is deliberately not exposed: see below. */
export interface DemandForecastRequest {
  /** `YYYY-MM-DD`. Required and explicit — the API has no implicit "tomorrow". */
  readonly targetDate: string
}

export const mlService = {
  /**
   * One prediction for one day.
   *
   * `horizon_days` is omitted rather than passed. The parameter exists on the endpoint so that
   * a caller expecting something other than the served horizon is told so with a `422` instead
   * of being handed a number computed for a different question; sending the value the server
   * already uses would add a way to get that wrong and buy nothing. The response states the
   * horizon it used, and the UI reads it from there.
   *
   * Throws `ApiError`: 404 unknown hotel or non-member, 422 insufficient history, 503 model
   * unavailable, 401 rejected token.
   */
  getDemandForecast(
    hotelPublicId: string,
    { targetDate }: DemandForecastRequest,
    signal?: AbortSignal,
  ): Promise<DemandPredictionResponse> {
    return api.get<DemandPredictionResponse>(`/hotels/${hotelPublicId}/ml/demand-forecast`, {
      query: { target_date: targetDate },
      ...(signal ? { signal } : {}),
    })
  },
} as const
