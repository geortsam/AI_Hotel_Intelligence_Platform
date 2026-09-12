import { api } from '@/services/api/client'
import type { DailySeriesResponse, DateRange, OverviewResponse } from '@/types/analytics'

/**
 * The analytics calls the dashboard makes.
 *
 * Two, and only two. The backend exposes five analytics routes; `revenue-by-category`,
 * `expenses-by-category` and `reviews` are not consumed here because the dashboard shows no
 * category breakdown and no rating distribution. Wrapping them anyway would be writing the
 * Stage 5.8 analytics screen inside Stage 5.4.
 *
 * Transport, headers, base URL and error translation stay in the API client. This module
 * knows only which paths exist, what they return, and how a date range is spelled.
 *
 * **Every route is hotel-scoped, and that is the tenant boundary.** There is no flat
 * `/analytics` on the backend -- deliberately, per its own router comment: the hotel segment
 * is where isolation is established. A caller who is not a member of the hotel receives
 * `404 Hotel not found.`, byte-identical to the response for a hotel that does not exist, so
 * the API cannot be used to discover which properties are real.
 */

/** The inclusive, hotel-local range a request asks for. Both bounds are required. */
export interface AnalyticsRange {
  /** `YYYY-MM-DD`, inclusive. */
  readonly dateFrom: string
  /** `YYYY-MM-DD`, inclusive. */
  readonly dateTo: string
}

/**
 * The widest range one request may span, from `app.schemas.analytics.MAX_RANGE_DAYS`.
 *
 * Restated here so the UI can refuse to build an impossible request rather than sending it
 * and rendering the 422. Nothing in the application offers a period this long, so it is a
 * guard rather than a limit anyone meets.
 */
export const MAX_RANGE_DAYS = 366

/**
 * `date_from` and `date_to`, exactly as the backend names them.
 *
 * Both are required: the API has no implicit "last 30 days", on the stated grounds that a
 * default window would make two identical-looking requests mean different things on
 * different days.
 */
function rangeQuery({ dateFrom, dateTo }: AnalyticsRange): Record<string, string> {
  return { date_from: dateFrom, date_to: dateTo }
}

export const analyticsService = {
  /**
   * The KPI snapshot for one range.
   *
   * Throws `ApiError`: 404 for an unknown hotel *or* one the caller cannot reach, 422 for a
   * reversed or over-long range, 401 for a rejected token.
   */
  getOverview(
    hotelPublicId: string,
    range: AnalyticsRange,
    signal?: AbortSignal,
  ): Promise<OverviewResponse> {
    return api.get<OverviewResponse>(`/hotels/${hotelPublicId}/analytics/overview`, {
      query: rangeQuery(range),
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * One row per calendar day, ascending, including days with no activity.
   *
   * The gap-free guarantee is the backend's, and the chart depends on it: it plots the array
   * in order and does not interpolate.
   */
  getDailySeries(
    hotelPublicId: string,
    range: AnalyticsRange,
    signal?: AbortSignal,
  ): Promise<DailySeriesResponse> {
    return api.get<DailySeriesResponse>(`/hotels/${hotelPublicId}/analytics/daily`, {
      query: rangeQuery(range),
      ...(signal ? { signal } : {}),
    })
  },
} as const

/** Narrow a response's echoed range back to the request shape, for display. */
export function toAnalyticsRange(range: DateRange): AnalyticsRange {
  return { dateFrom: range.date_from, dateTo: range.date_to }
}
