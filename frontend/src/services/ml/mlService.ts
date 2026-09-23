import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type { DemandPredictionResponse } from '@/types/ml'
import type {
  ForecastAccuracyResponse,
  PredictionDistributionResponse,
  StoredDemandPrediction,
} from '@/types/mlPerformance'

/**
 * The served demand model and the measurements taken over it, as the browser reaches them.
 *
 * Four calls, and **only one of them reaches a model**: the serving route declares a `503` for
 * an unavailable artifact, and the other three declare none, because they load none — they read
 * or measure rows the serving route already wrote. Stage 7.2 added the first of these methods;
 * Stage 7.4 added the other three.
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

/** An inclusive window of target dates. Both ends required, as the API requires them. */
export interface TargetDateWindow {
  /** `YYYY-MM-DD`, inclusive. */
  readonly windowFrom: string
  /** `YYYY-MM-DD`, inclusive. */
  readonly windowTo: string
}

export interface StoredPredictionsRequest {
  readonly dateFrom: string
  readonly dateTo: string
  /** 1..100. Defaults to the API's maximum, so one request covers the widest useful window. */
  readonly pageSize?: number
}

export interface ForecastAccuracyRequest extends TargetDateWindow {
  /**
   * `YYYY-MM-DD`. The date the measurement is taken as of.
   *
   * Required by the API and passed through unchanged. It decides which target dates have
   * cleared the settlement lag and is therefore the only notion of "when" on this path.
   */
  readonly asOfDate: string
}

export interface PredictionDistributionRequest extends TargetDateWindow {
  /** A second window, or nothing. Never half a pair — the API refuses that with a 422. */
  readonly baseline?: TargetDateWindow
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

  /**
   * One page of this hotel's stored predictions, oldest first (Stage 6.11).
   *
   * Every stored row in the window, not one per date: a target date legitimately carries more
   * than one prediction when a late booking moved the model's inputs. Callers that need one
   * value per day must decide what to do about that, and this method does not decide for them.
   *
   * `pageSize` is capped at 100 by the API. A window wider than one page returns a `total`
   * larger than `items.length`, and the caller is expected to say so rather than quietly
   * present a partial answer as a whole one.
   *
   * Throws `ApiError`: 404 unknown hotel or non-member, 422 a reversed window or a page size
   * out of bounds, 401 rejected token. An empty window is not an error — it returns an empty
   * page.
   */
  listStoredPredictions(
    hotelPublicId: string,
    { dateFrom, dateTo, pageSize = 100 }: StoredPredictionsRequest,
    signal?: AbortSignal,
  ): Promise<Page<StoredDemandPrediction>> {
    return api.get<Page<StoredDemandPrediction>>(
      `/hotels/${hotelPublicId}/ml/demand-predictions`,
      {
        query: { date_from: dateFrom, date_to: dateTo, page: 1, page_size: pageSize },
        ...(signal ? { signal } : {}),
      },
    )
  },

  /**
   * How far off this hotel's served predictions were, under the frozen `accuracy_v1` protocol.
   *
   * **Requires the manager role.** A member below it receives `403`, which is distinct from the
   * `404` a non-member receives: a member already knows the hotel exists. This method does not
   * translate either — `ApiError` carries the status and the feature decides what a reader
   * sees.
   *
   * `asOfDate` is required and explicit. It decides which target dates have cleared the
   * settlement lag, so a today-relative default would make one request mean different things on
   * different days.
   *
   * Throws `ApiError`: 403 below manager, 404 unknown hotel or non-member, 422 a reversed
   * window or one longer than 366 days, 401 rejected token.
   */
  getForecastAccuracy(
    hotelPublicId: string,
    { asOfDate, windowFrom, windowTo }: ForecastAccuracyRequest,
    signal?: AbortSignal,
  ): Promise<ForecastAccuracyResponse> {
    return api.get<ForecastAccuracyResponse>(`/hotels/${hotelPublicId}/ml/forecast-accuracy`, {
      query: { as_of_date: asOfDate, window_from: windowFrom, window_to: windowTo },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * What this hotel's stored predictions and their inputs looked like over a window.
   *
   * Membership is enough, unlike the accuracy route: every field summarised is either calendar
   * arithmetic or a figure the same caller already reads in full from `/analytics/daily`.
   *
   * The baseline window is a pair or nothing. Supplying one end without the other is a `422`
   * rather than a silently ignored parameter, so this method sends both or neither.
   *
   * Throws `ApiError`: 404 unknown hotel or non-member, 422 a malformed window, 401 rejected
   * token.
   */
  getPredictionDistribution(
    hotelPublicId: string,
    { windowFrom, windowTo, baseline }: PredictionDistributionRequest,
    signal?: AbortSignal,
  ): Promise<PredictionDistributionResponse> {
    return api.get<PredictionDistributionResponse>(
      `/hotels/${hotelPublicId}/ml/prediction-distribution`,
      {
        query: {
          window_from: windowFrom,
          window_to: windowTo,
          ...(baseline
            ? { baseline_from: baseline.windowFrom, baseline_to: baseline.windowTo }
            : {}),
        },
        ...(signal ? { signal } : {}),
      },
    )
  },
} as const
