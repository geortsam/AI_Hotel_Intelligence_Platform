import { useCallback, useEffect, useMemo, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { analyticsService, type AnalyticsRange } from '@/services/analytics/analyticsService'
import { financeService } from '@/services/finance/financeService'
import { mlService } from '@/services/ml/mlService'
import { rangeFor, shiftDate, todayInZone, type PeriodId } from '@/features/dashboard/period'
import type { DailySeriesResponse, OverviewResponse } from '@/types/analytics'
import type { ExpenseBreakdownResponse, RevenueBreakdownResponse } from '@/types/finance'
import type { Hotel } from '@/types/hotel'
import type { DemandPredictionResponse } from '@/types/ml'

/**
 * Everything the analytics report shows, fetched once per hotel and period.
 *
 * ## Why these five requests
 *
 * | Request | Answers |
 * |---|---|
 * | `analytics/overview` | occupancy, ADR, RevPAR, room nights, booking activity |
 * | `analytics/daily` | the per-day series the activity charts plot |
 * | `analytics/revenue-by-category` | where the money came from |
 * | `analytics/expenses-by-category` | where it went |
 * | `ml/demand-forecast` | the served model's estimate for tomorrow |
 *
 * None is derivable from another. The overview has no per-day grain, and the daily series
 * cannot reproduce the overview's figures — **ADR is a ratio of sums, not a sum of ratios**, so
 * averaging daily ADRs gives a different and wrong number. The breakdowns group by a dimension
 * neither of the other two carries. The forecast is a different subsystem entirely.
 *
 * They are issued **concurrently**: five round trips overlap into roughly one. The count is
 * fixed at five regardless of how many bookings, rooms, days, categories or currencies the
 * hotel has, so this is not an N+1.
 *
 * ## The forecast is allowed to fail on its own
 *
 * `Promise.allSettled`, not `Promise.all`. Four of the five requests are the report; the fifth
 * is an extra. A hotel with no occupancy on the days the model's lag features read gets a
 * `422 INSUFFICIENT_HISTORY` — which is the serving stage working as designed, not a fault —
 * and the whole page must not collapse because a new property cannot be forecast yet. The
 * forecast's outcome is therefore carried separately, as one of three states, and the report
 * renders with or without it.
 *
 * The four core requests are treated together: if any of them fails the report has a hole in
 * it, and a partial page that looks complete is worse than an honest failure. **Nothing here
 * renders zero for a request that did not succeed.**
 *
 * ## No cache
 *
 * Deliberately, and for the reason the dashboard states: a request is issued when the hotel or
 * the period changes and at no other time. Caching would mean deciding how long a stale
 * occupancy figure may be shown, and nothing measured justifies an answer yet.
 */

export type ReportStatus = 'idle' | 'loading' | 'ready' | 'error'

/** The served model either answered, declined for a stated reason, or was unreachable. */
export type ForecastOutcome =
  | { readonly kind: 'ready'; readonly prediction: DemandPredictionResponse }
  | { readonly kind: 'unavailable'; readonly code: string; readonly detail: string }

export interface AnalyticsReportData {
  readonly overview: OverviewResponse
  readonly daily: DailySeriesResponse
  readonly revenue: RevenueBreakdownResponse
  readonly expenses: ExpenseBreakdownResponse
  /** Null while the forecast request is still in flight or was never attempted. */
  readonly forecast: ForecastOutcome | null
}

export interface AnalyticsReportState {
  readonly status: ReportStatus
  readonly data: AnalyticsReportData | null
  readonly error: ApiError | null
  /** Null until a hotel is known, mirroring `useDashboardData`. */
  readonly range: AnalyticsRange | null
  readonly retry: () => void
}

/** Reads the reason out of an `ApiError` without inventing one. */
function describeRefusal(error: unknown): ForecastOutcome {
  if (error instanceof ApiError) {
    return { kind: 'unavailable', code: error.code, detail: error.message }
  }
  return {
    kind: 'unavailable',
    code: 'REQUEST_FAILED',
    detail: 'The forecast request did not complete.',
  }
}

export function useAnalyticsReport(hotel: Hotel | null, period: PeriodId): AnalyticsReportState {
  const [status, setStatus] = useState<ReportStatus>('idle')
  const [data, setData] = useState<AnalyticsReportData | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [attempt, setAttempt] = useState(0)

  const timeZone = hotel?.timezone ?? 'UTC'
  const hotelPublicId = hotel?.public_id ?? null

  /*
   * Memoised for the reason `useDashboardData` states: `rangeFor` reads the clock, so an
   * unmemoised call returns a new object on every render and the effect below would re-fetch
   * forever.
   */
  const range = useMemo(() => rangeFor(period, timeZone), [period, timeZone])

  /*
   * Tomorrow in the hotel's own calendar. The served model forecasts a fixed horizon ahead of
   * the last day it may read, and the endpoint requires an explicit target: there is no
   * implicit "next day" on the API, deliberately, so that one request does not mean different
   * things on different days.
   */
  const targetDate = useMemo(() => shiftDate(todayInZone(timeZone), 1), [timeZone])

  const retry = useCallback(() => setAttempt((n) => n + 1), [])

  useEffect(() => {
    if (!hotelPublicId) {
      setStatus('idle')
      setData(null)
      setError(null)
      return
    }

    const controller = new AbortController()
    const { signal } = controller
    let cancelled = false

    setStatus('loading')
    setError(null)

    void (async () => {
      const [overview, daily, revenue, expenses, forecast] = await Promise.allSettled([
        analyticsService.getOverview(hotelPublicId, range, signal),
        analyticsService.getDailySeries(hotelPublicId, range, signal),
        financeService.getRevenueBreakdown(hotelPublicId, range, signal),
        financeService.getExpenseBreakdown(hotelPublicId, range, signal),
        mlService.getDemandForecast(hotelPublicId, { targetDate }, signal),
      ])

      if (cancelled || signal.aborted) {
        return
      }

      // The four that constitute the report. One failure is the page's failure.
      const core = [overview, daily, revenue, expenses]
      const failed = core.find((result) => result.status === 'rejected')
      if (failed && failed.status === 'rejected') {
        const reason: unknown = failed.reason
        setError(
          reason instanceof ApiError
            ? reason
            : new ApiError(0, 'REQUEST_FAILED', 'The report could not be loaded.'),
        )
        setData(null)
        setStatus('error')
        return
      }

      setData({
        overview: (overview as PromiseFulfilledResult<OverviewResponse>).value,
        daily: (daily as PromiseFulfilledResult<DailySeriesResponse>).value,
        revenue: (revenue as PromiseFulfilledResult<RevenueBreakdownResponse>).value,
        expenses: (expenses as PromiseFulfilledResult<ExpenseBreakdownResponse>).value,
        forecast:
          forecast.status === 'fulfilled'
            ? { kind: 'ready', prediction: forecast.value }
            : describeRefusal(forecast.reason),
      })
      setError(null)
      setStatus('ready')
    })()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, range, targetDate, attempt])

  return { status, data, error, range: hotelPublicId ? range : null, retry }
}
