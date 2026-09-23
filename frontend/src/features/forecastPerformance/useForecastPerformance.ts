import { useCallback, useEffect, useMemo, useState } from 'react'

import { rangeFor, todayInZone, type PeriodId } from '@/features/dashboard/period'
import { analyticsService, type AnalyticsRange } from '@/services/analytics/analyticsService'
import { ApiError } from '@/services/api/ApiError'
import { mlService } from '@/services/ml/mlService'
import type { DailySeriesResponse } from '@/types/analytics'
import type { Page } from '@/types/api'
import type { Hotel } from '@/types/hotel'
import type {
  ForecastAccuracyResponse,
  PredictionDistributionResponse,
  StoredDemandPrediction,
} from '@/types/mlPerformance'

/**
 * Everything the forecast-performance section shows, fetched once per hotel and period.
 *
 * ## Four requests, four independent outcomes
 *
 * | Request | Answers | Role |
 * |---|---|---|
 * | `analytics/daily` | what actually happened, per day | member |
 * | `ml/demand-predictions` | what the model said, per day | member |
 * | `ml/prediction-distribution` | what the predictions and their inputs looked like | member |
 * | `ml/forecast-accuracy` | how far off they were, under `accuracy_v1` | **manager** |
 *
 * They are issued **concurrently** — four round trips overlap into roughly one — and the count
 * is fixed regardless of how many days, predictions or model versions the hotel has, so this
 * is not an N+1.
 *
 * Each outcome is carried **separately**, and this is the part that matters: `Promise.allSettled`
 * with no shared failure. A viewer's `403` on accuracy must not take the distribution panel
 * down with it, and a hotel with no stored predictions must not make the chart look broken.
 * There is no page-level error state here at all — every panel renders its own outcome, and
 * **nothing renders zero for a request that did not succeed**.
 *
 * ## The manager-only request is issued, and its refusal is the answer
 *
 * The frontend has no way to know the caller's role before asking. `HotelResponse` carries no
 * role, `/auth/me` deliberately carries none — "the frontend must not infer authorization from
 * it" — and `/hotels/{id}/members`, which would say, itself requires the manager role. So the
 * accuracy request is issued and a `403` is treated as the authoritative answer: the panel is
 * not rendered, and the reader is told why in the shared `forbidden` copy.
 *
 * This is the pattern every role-sensitive screen in this application already uses, and it
 * keeps the server the only authority on access. A client-side role check would be a second
 * authorization decision in a place that cannot enforce one.
 *
 * ## No cache
 *
 * Deliberately, for the reason the dashboard states: a request is issued when the hotel or the
 * period changes and at no other time.
 */

export type PerformanceStatus = 'idle' | 'loading' | 'ready'

/** A request that either answered or did not. The refusal is data, not an exception. */
export type Outcome<T> =
  | { readonly kind: 'ready'; readonly value: T }
  | { readonly kind: 'failed'; readonly error: ApiError }

export interface ForecastPerformanceData {
  readonly daily: Outcome<DailySeriesResponse>
  readonly predictions: Outcome<Page<StoredDemandPrediction>>
  readonly distribution: Outcome<PredictionDistributionResponse>
  /** `failed` with a 403 for a member below manager. That is the expected viewer state. */
  readonly accuracy: Outcome<ForecastAccuracyResponse>
}

export interface ForecastPerformanceState {
  readonly status: PerformanceStatus
  readonly data: ForecastPerformanceData | null
  /** Null until a hotel is known, mirroring the other feature hooks. */
  readonly range: AnalyticsRange | null
  /** The as-of date the accuracy measurement was requested for, for display. */
  readonly asOfDate: string | null
  readonly retry: () => void
}

/** Wraps a settled promise without inventing a reason for a failure it cannot read. */
function outcomeOf<T>(result: PromiseSettledResult<T>): Outcome<T> {
  if (result.status === 'fulfilled') {
    return { kind: 'ready', value: result.value }
  }
  const reason: unknown = result.reason
  return {
    kind: 'failed',
    error:
      reason instanceof ApiError
        ? reason
        : new ApiError(0, 'REQUEST_FAILED', 'The request did not complete.'),
  }
}

export function useForecastPerformance(
  hotel: Hotel | null,
  period: PeriodId,
): ForecastPerformanceState {
  const [status, setStatus] = useState<PerformanceStatus>('idle')
  const [data, setData] = useState<ForecastPerformanceData | null>(null)
  const [attempt, setAttempt] = useState(0)

  const timeZone = hotel?.timezone ?? 'UTC'
  const hotelPublicId = hotel?.public_id ?? null

  /* Memoised for the reason `useDashboardData` states: `rangeFor` reads the clock, so an
     unmemoised call returns a new object every render and the effect below would loop. */
  const range = useMemo(() => rangeFor(period, timeZone), [period, timeZone])

  /* The hotel's own today. The accuracy endpoint requires an explicit as-of date — it decides
     which target dates have cleared the settlement lag — and resolving "today" in the
     property's zone is what makes a London manager and an Athens receptionist see one answer. */
  const asOfDate = useMemo(() => todayInZone(timeZone), [timeZone])

  const retry = useCallback(() => setAttempt((n) => n + 1), [])

  useEffect(() => {
    if (!hotelPublicId) {
      setStatus('idle')
      setData(null)
      return
    }

    const controller = new AbortController()
    const { signal } = controller
    let cancelled = false

    setStatus('loading')

    void (async () => {
      const [daily, predictions, distribution, accuracy] = await Promise.allSettled([
        analyticsService.getDailySeries(hotelPublicId, range, signal),
        mlService.listStoredPredictions(
          hotelPublicId,
          { dateFrom: range.dateFrom, dateTo: range.dateTo },
          signal,
        ),
        mlService.getPredictionDistribution(
          hotelPublicId,
          { windowFrom: range.dateFrom, windowTo: range.dateTo },
          signal,
        ),
        mlService.getForecastAccuracy(
          hotelPublicId,
          { asOfDate, windowFrom: range.dateFrom, windowTo: range.dateTo },
          signal,
        ),
      ])

      if (cancelled || signal.aborted) {
        return
      }

      setData({
        daily: outcomeOf(daily),
        predictions: outcomeOf(predictions),
        distribution: outcomeOf(distribution),
        accuracy: outcomeOf(accuracy),
      })
      setStatus('ready')
    })()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, range, asOfDate, attempt])

  return {
    status,
    data,
    range: hotelPublicId ? range : null,
    asOfDate: hotelPublicId ? asOfDate : null,
    retry,
  }
}
