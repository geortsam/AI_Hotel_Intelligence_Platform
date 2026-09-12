import { useCallback, useEffect, useMemo, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { analyticsService, type AnalyticsRange } from '@/services/analytics/analyticsService'
import { previousRange, rangeFor, type PeriodId, periodOption } from '@/features/dashboard/period'
import type { DailySeriesResponse, OverviewResponse } from '@/types/analytics'
import type { Hotel } from '@/types/hotel'

/**
 * Fetching everything the dashboard shows, once per hotel and period.
 *
 * ## Three requests, and why not fewer
 *
 * 1. `analytics/overview` for the selected period -- the KPI figures.
 * 2. `analytics/overview` for the immediately preceding, equal-length period -- the only
 *    honest source for a "vs previous" comparison.
 * 3. `analytics/daily` for the selected period -- the trend series.
 *
 * None is redundant. The daily series cannot produce the overview's figures: **ADR is a
 * ratio of sums, not a sum of ratios**, so averaging thirty daily ADRs gives a different --
 * and wrong -- number from `total room revenue / total nights sold`. The overview cannot
 * produce the series either; it has no per-day grain. And the comparison window is a
 * different range, which this API expresses as a different request because it has no
 * "compare to" parameter.
 *
 * They are issued **concurrently**, so the wall-clock cost is one round trip rather than
 * three. This is emphatically not an N+1: the count is fixed at three regardless of how many
 * bookings, rooms, days or currencies the hotel has.
 *
 * ## Why there is no cache
 *
 * There is no query library here and no global store. A request is issued when the hotel or
 * the period changes and at no other time; switching back to a period re-fetches, which is
 * correct for an operations console where the underlying figures move during a shift. Adding
 * a cache would mean deciding how long a stale occupancy figure may be shown for, and that
 * decision has no justification from measured behaviour yet.
 */

export type DashboardStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface DashboardData {
  readonly overview: OverviewResponse
  /** The preceding equal-length window. Null when that request failed on its own. */
  readonly previous: OverviewResponse | null
  readonly daily: DailySeriesResponse
}

export interface DashboardState {
  readonly status: DashboardStatus
  readonly data: DashboardData | null
  /** The failure, when `status` is `error`. Never rendered raw -- see `describeFailure`. */
  readonly error: ApiError | null
  /** The range that was actually requested, for labelling the period on screen. */
  readonly range: AnalyticsRange | null
  readonly retry: () => void
}

export function useDashboardData(hotel: Hotel | null, period: PeriodId): DashboardState {
  const [status, setStatus] = useState<DashboardStatus>('idle')
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [attempt, setAttempt] = useState(0)

  const timezone = hotel?.timezone ?? 'UTC'
  const hotelId = hotel?.public_id ?? null

  /*
   * The range is computed once per (period, zone) rather than on each render.
   *
   * Not a micro-optimisation: `rangeFor` reads the clock, so an unmemoised call would return
   * a new object every render and the effect below -- which depends on it -- would re-fetch
   * forever. Deriving it from the hotel's zone is also what keeps the request hotel-local;
   * see `period.ts` for why the browser's zone is the wrong answer.
   */
  const range = useMemo(
    () => (hotelId === null ? null : rangeFor(period, timezone)),
    // `attempt` is listed on purpose although the body does not name it: a retry must
    // re-read the clock, so a failure just before midnight does not retry against
    // yesterday's range.
    [hotelId, period, timezone, attempt],
  )

  useEffect(() => {
    if (hotelId === null || range === null) {
      setStatus('idle')
      setData(null)
      setError(null)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    const { days } = periodOption(period)
    const comparison = previousRange(range, days)

    /*
     * The comparison is allowed to fail on its own.
     *
     * A dashboard whose KPI figures are all present should not go blank because the window
     * before them could not be fetched; the comparison simply is not shown. The two calls
     * that carry the actual numbers are not treated this way -- if either fails, the page
     * reports a failure rather than rendering half a picture.
     */
    const previous = analyticsService
      .getOverview(hotelId, comparison, controller.signal)
      .catch(() => null)

    Promise.all([
      analyticsService.getOverview(hotelId, range, controller.signal),
      analyticsService.getDailySeries(hotelId, range, controller.signal),
      previous,
    ])
      .then(([overview, daily, comparisonOverview]) => {
        if (cancelled) {
          return
        }
        setData({ overview, daily, previous: comparisonOverview })
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled) {
          return
        }
        // An abort is this component tidying up after itself, not a failure to report. It
        // arrives here as a network-shaped ApiError because the client cannot see a
        // response; leaving it to set `error` would flash a failure on every period change.
        if (controller.signal.aborted) {
          return
        }
        setData(null)
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The dashboard could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelId, period, range])

  const retry = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  return { status, data, error, range, retry }
}
