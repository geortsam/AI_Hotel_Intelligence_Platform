import { useCallback, useEffect, useMemo, useState } from 'react'

import { shiftDate, todayInZone } from '@/features/dashboard/period'
import { ApiError } from '@/services/api/ApiError'
import { intelligenceService } from '@/services/intelligence/intelligenceService'
import type {
  AnomalyReport,
  DemandTrend,
  InsightsReport,
  OccupancyForecast,
  RevenueForecast,
} from '@/types/intelligence'

/**
 * One hotel's intelligence, for one observation window and one horizon.
 *
 * ## The two spans, and why they are computed rather than typed in
 *
 * Every Intelligence endpoint takes explicit dates — deliberately, so an identical request
 * returns an identical prediction whenever it is made. That leaves the frontend to decide
 * *which* dates, and there are exactly two spans:
 *
 * * the **observation window**, `observationDays` ending today in the hotel's own time zone.
 *   Trend and anomaly detection look backwards, so the window ends today and never later.
 * * the **horizon**, `horizonDays` starting **tomorrow**. It begins the day after the window
 *   ends, which is what makes the window exactly the training data — no gap, no overlap, no
 *   leakage.
 *
 * "Today" is resolved in the hotel's zone with the Stage 5.4 helper rather than the browser's
 * clock, because a property in Athens rolls over before a laptop in London does and a
 * forecast that started yesterday would silently include a day already on the books.
 *
 * ## Five endpoints, five requests, and no more
 *
 * The five reads are independent: each is fetched once per parameter change, none depends on
 * another's response, and nothing is requested per chart point or per anomaly row. Two of
 * them are behind the tabs they belong to, so an operator who never opens the revenue tab
 * never pays for a revenue forecast.
 *
 * ## Nothing is computed here
 *
 * No forecast, no trend classification, no anomaly detection, no percentage change. Every
 * number on this screen came from the server, including the thresholds it was judged
 * against. The one arithmetic in this file is date arithmetic, and it decides only what to
 * *ask for*.
 */

export type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

/** A resource that is fetched once and rendered, with its own failure. */
export interface Resource<T> {
  readonly data: T | null
  readonly status: LoadStatus
  readonly error: ApiError | null
}

const IDLE: Resource<never> = { data: null, status: 'idle', error: null }

export interface IntelligenceParams {
  /** Days of history to observe, ending today in the hotel's zone. Bounded by the API at 366. */
  readonly observationDays: number
  /** Days to forecast, starting tomorrow. Bounded by the API at 90. */
  readonly horizonDays: number
  /** Days of history a forecast may learn from. Bounded by the API at 14..365. */
  readonly trainingDays: number
}

export interface IntelligenceState {
  readonly window: { readonly dateFrom: string; readonly dateTo: string }
  readonly horizon: { readonly dateFrom: string; readonly dateTo: string }

  readonly occupancy: Resource<OccupancyForecast>
  readonly revenue: Resource<RevenueForecast>
  readonly trend: Resource<DemandTrend>
  readonly anomalies: Resource<AnomalyReport>
  readonly insights: Resource<InsightsReport>

  readonly reload: () => void
}

/** Which of the five to fetch. The revenue forecast waits for its tab to be opened. */
export interface IntelligenceEnabled {
  readonly occupancy: boolean
  readonly revenue: boolean
}

/**
 * Runs one read and keeps its own status, so one endpoint's failure does not blank the page.
 *
 * A malformed 200 is a failure rather than an empty result: `points.map` on something that
 * is not an array would take the page down from a render, past the promise's `catch`.
 */
function useResource<T>(
  enabled: boolean,
  fetcher: (signal: AbortSignal) => Promise<T>,
  valid: (value: unknown) => boolean,
  deps: readonly unknown[],
): Resource<T> {
  const [state, setState] = useState<Resource<T>>(IDLE)

  useEffect(() => {
    if (!enabled) {
      setState(IDLE)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setState({ data: null, status: 'loading', error: null })

    fetcher(controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        if (!valid(result)) {
          setState({
            data: null,
            status: 'error',
            error: new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The intelligence response was not in the expected format.',
            ),
          })
          return
        }
        setState({ data: result, status: 'ready', error: null })
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setState({
          data: null,
          status: 'error',
          error:
            cause instanceof ApiError
              ? cause
              : new ApiError(0, ApiError.NETWORK_CODE, 'The request could not be completed.'),
        })
      })

    return () => {
      cancelled = true
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return state
}

const hasArray = (key: string) => (value: unknown) =>
  typeof value === 'object' && value !== null && Array.isArray((value as never)[key])

export function useIntelligence(
  hotelPublicId: string | null,
  timeZone: string,
  params: IntelligenceParams,
  enabled: IntelligenceEnabled,
): IntelligenceState {
  const [attempt, setAttempt] = useState(0)

  /*
   * The two spans. `observationDays - 1` because both bounds count: a 30-day window ending
   * today starts 29 days ago, not 30.
   */
  const { window, horizon } = useMemo(() => {
    const today = todayInZone(timeZone)
    return {
      window: {
        dateFrom: shiftDate(today, -(params.observationDays - 1)),
        dateTo: today,
      },
      horizon: {
        dateFrom: shiftDate(today, 1),
        dateTo: shiftDate(today, params.horizonDays),
      },
    }
  }, [timeZone, params.observationDays, params.horizonDays])

  const id = hotelPublicId
  const on = id !== null

  const occupancy = useResource<OccupancyForecast>(
    on && enabled.occupancy,
    (signal) =>
      intelligenceService.occupancyForecast(
        id!,
        { dateFrom: horizon.dateFrom, dateTo: horizon.dateTo, trainingDays: params.trainingDays },
        signal,
      ),
    hasArray('points'),
    [id, enabled.occupancy, horizon.dateFrom, horizon.dateTo, params.trainingDays, attempt],
  )

  const revenue = useResource<RevenueForecast>(
    on && enabled.revenue,
    (signal) =>
      intelligenceService.revenueForecast(
        id!,
        { dateFrom: horizon.dateFrom, dateTo: horizon.dateTo, trainingDays: params.trainingDays },
        signal,
      ),
    hasArray('currencies'),
    [id, enabled.revenue, horizon.dateFrom, horizon.dateTo, params.trainingDays, attempt],
  )

  const trend = useResource<DemandTrend>(
    on,
    (signal) =>
      intelligenceService.demandTrend(
        id!,
        { dateFrom: window.dateFrom, dateTo: window.dateTo },
        signal,
      ),
    (value) =>
      typeof value === 'object' && value !== null && typeof (value as never)['direction'] === 'string',
    [id, window.dateFrom, window.dateTo, attempt],
  )

  const anomalies = useResource<AnomalyReport>(
    on,
    (signal) =>
      intelligenceService.anomalies(
        id!,
        { dateFrom: window.dateFrom, dateTo: window.dateTo },
        signal,
      ),
    hasArray('anomalies'),
    [id, window.dateFrom, window.dateTo, attempt],
  )

  const insights = useResource<InsightsReport>(
    on,
    (signal) =>
      intelligenceService.insights(
        id!,
        {
          dateFrom: window.dateFrom,
          dateTo: window.dateTo,
          horizonDays: params.horizonDays,
        },
        signal,
      ),
    hasArray('insights'),
    [id, window.dateFrom, window.dateTo, params.horizonDays, attempt],
  )

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  return { window, horizon, occupancy, revenue, trend, anomalies, insights, reload }
}
