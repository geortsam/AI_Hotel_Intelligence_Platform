import { api } from '@/services/api/client'
import type {
  AnomalyReport,
  DemandTrend,
  InsightsReport,
  OccupancyForecast,
  RevenueForecast,
} from '@/types/intelligence'

/**
 * The five Intelligence reads, all hotel-scoped and all GET.
 *
 * ## There is no write, and no flat route
 *
 * Nothing under `/intelligence` changes a rate, moves a booking, allocates a room or posts
 * revenue. It observes and explains. Every path sits under
 * `/hotels/{hotel_public_id}/intelligence`, because a portfolio-wide forecast would sit
 * outside the segment where tenant isolation is established — so there is no endpoint to
 * call for "all my hotels", and none is invented here.
 *
 * ## Authorization is membership, and a stranger gets 404 rather than 403
 *
 * `require_hotel` resolves the hotel and then requires the caller to be a member. A hotel
 * that does not exist and a hotel the caller has nothing to do with raise the **same 404
 * with the same message**, so the response cannot be used to discover which properties
 * exist. No role is required beyond membership, so there is **no 403 path** on these
 * endpoints for a member — verified live across both demo hotels.
 *
 * ## Every date is explicit, and that is the point
 *
 * `training_days` and `horizon_days` are fixed counts relative to the supplied dates, never
 * "the last 90 days from today". An identical request returns an identical prediction
 * whenever it is made, and a today-relative default would quietly break that. The caller
 * therefore always passes real dates; this module never defaults one.
 *
 * ## The bounds are the routers' own, and they are enforced
 *
 * Verified live: a 91-day horizon, a 376-day window, `training_days=13`, `training_days=366`
 * and `horizon_days=91` are each a **422**. A reversed range is a 422 from the service with
 * its own message. Unknown parameters are **ignored silently** — `?metric=occupancy`
 * returned an unfiltered 200 — which is why no filter this API lacks is offered anywhere.
 */
export const intelligenceService = {
  /**
   * Forecast occupied room nights across a horizon.
   *
   * Each day carries the room nights already **on the books** as an actual figure beside the
   * model's prediction; the two are never blended. A day with too little history reports
   * `insufficient_data` and a null prediction rather than an invented number.
   */
  occupancyForecast(
    hotelPublicId: string,
    query: { readonly dateFrom: string; readonly dateTo: string; readonly trainingDays: number },
    signal?: AbortSignal,
  ): Promise<OccupancyForecast> {
    return api.get<OccupancyForecast>(`/hotels/${hotelPublicId}/intelligence/forecast/occupancy`, {
      query: {
        date_from: query.dateFrom,
        date_to: query.dateTo,
        training_days: query.trainingDays,
      },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Forecast room revenue, independently per currency.
   *
   * Only currencies with real history are forecast; one the hotel has never traded in is
   * absent rather than predicted at zero. Currencies are never combined and there is no FX
   * anywhere — not in the backend, and not here.
   */
  revenueForecast(
    hotelPublicId: string,
    query: { readonly dateFrom: string; readonly dateTo: string; readonly trainingDays: number },
    signal?: AbortSignal,
  ): Promise<RevenueForecast> {
    return api.get<RevenueForecast>(`/hotels/${hotelPublicId}/intelligence/forecast/revenue`, {
      query: {
        date_from: query.dateFrom,
        date_to: query.dateTo,
        training_days: query.trainingDays,
      },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Classify booking demand as increasing, decreasing or stable.
   *
   * Split-window median comparison over `bookings.booked_at`. Both halves' medians and the
   * threshold come back, so the classification can be recomputed by hand — and the UI shows
   * them for exactly that reason.
   */
  demandTrend(
    hotelPublicId: string,
    query: { readonly dateFrom: string; readonly dateTo: string },
    signal?: AbortSignal,
  ): Promise<DemandTrend> {
    return api.get<DemandTrend>(`/hotels/${hotelPublicId}/intelligence/demand-trend`, {
      query: { date_from: query.dateFrom, date_to: query.dateTo },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Flag unusual occupancy, booking or revenue days.
   *
   * Modified z-score on the median absolute deviation. Every flag carries the metric, date,
   * value, window median, MAD, score and threshold. An empty list means nothing was unusual;
   * `metrics_scanned` says what was examined, so the empty case is unambiguous.
   */
  anomalies(
    hotelPublicId: string,
    query: { readonly dateFrom: string; readonly dateTo: string },
    signal?: AbortSignal,
  ): Promise<AnomalyReport> {
    return api.get<AnomalyReport>(`/hotels/${hotelPublicId}/intelligence/anomalies`, {
      query: { date_from: query.dateFrom, date_to: query.dateTo },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Structured, deterministic findings assembled from the trend, anomaly and forecast output.
   *
   * Explanations are templates filled from the numbers each insight carries. **No generated
   * prose, and no language model** — the same data always produces the same sentence.
   *
   * The forecast horizon starts the day after the observation window, so the window is
   * exactly the training data: no gap, no overlap, no leakage.
   */
  insights(
    hotelPublicId: string,
    query: { readonly dateFrom: string; readonly dateTo: string; readonly horizonDays: number },
    signal?: AbortSignal,
  ): Promise<InsightsReport> {
    return api.get<InsightsReport>(`/hotels/${hotelPublicId}/intelligence/insights`, {
      query: {
        date_from: query.dateFrom,
        date_to: query.dateTo,
        horizon_days: query.horizonDays,
      },
      ...(signal ? { signal } : {}),
    })
  },
} as const
