import { AlertTriangle, Lock } from 'lucide-react'

import { PeriodSelector } from '@/features/dashboard/PeriodSelector'
import { StateMessage } from '@/features/dashboard/StateMessage'
import type { PeriodId } from '@/features/dashboard/period'
import { ForecastChart } from '@/features/intelligence/ForecastChart'
import { formatCount, formatDate } from '@/lib/format'
import { describeFailure } from '@/services/api/failures'
import type { Hotel } from '@/types/hotel'

import { AccuracyPanel } from './AccuracyPanel'
import { DistributionPanel } from './DistributionPanel'
import { pairActualsWithPredictions } from './pairing'
import styles from './ForecastPerformanceSection.module.css'
import {
  useForecastPerformance,
  type Outcome,
} from './useForecastPerformance'

/**
 * The trained demand model's measured performance, as a section of the intelligence screen.
 *
 * ## Why it is here and yet fenced off
 *
 * The screen above it is V1's statistical forecaster — seasonal day-of-week medians computed
 * per request. This is a different subsystem: an artifact fitted offline, versioned and
 * checksummed. They answer a similar question by entirely different means, and the codebase is
 * deliberate about not implying a lineage they do not share. So this lives under its own
 * heading, names the model version it is about, and never shares a chart, an axis or a figure
 * with the statistical forecasts above.
 *
 * ## Three panels, three independent outcomes
 *
 * Every request settles on its own and renders its own result or its own failure. In
 * particular, **the accuracy request failing does not take the rest of the section down** —
 * which is the ordinary case for a viewer, whose `403` is the answer rather than a fault.
 *
 * Nothing renders zero for a request that did not succeed.
 *
 * ## No confidence band
 *
 * The chart is passed `lower: null`, `upper: null` and `confidenceLabel: null`. The served
 * model is a point forecaster and the Stage 7.3 protocol produces no interval, so there is
 * none to draw. `ForecastChart` renders the band only where the server supplied both bounds,
 * and states in its own description that the response carried no interval.
 */

export interface ForecastPerformanceSectionProps {
  readonly hotel: Hotel | null
  readonly period: PeriodId
  readonly onPeriodChange: (period: PeriodId) => void
}

/** Failure copy for the member-level requests. A 403 is not reachable on these. */
const MEMBER_FAILURE_COPY = {
  notFound: {
    title: 'Not available for this property',
    detail:
      'The property could not be found, or your access to it has been removed. The API answers the same way for both, so there is nothing more specific to say.',
    canRetry: false,
  },
  serverFault: {
    title: 'Temporarily unavailable',
    detail: 'This measurement could not be reached. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

/**
 * Failure copy for the accuracy request, whose 403 is an ordinary state rather than a fault.
 *
 * The default `forbidden` copy says the account may not view "this information", which would
 * be read as covering the whole section. A viewer can see everything else here, so the copy
 * names the one panel they cannot.
 */
const ACCURACY_FAILURE_COPY = {
  ...MEMBER_FAILURE_COPY,
  forbidden: {
    title: 'Measured error is restricted',
    detail:
      'Reading how far the model has been from what happened requires the manager role at this property. Everything else in this section is available to you.',
    canRetry: false,
  },
} as const

function Failure({
  outcome,
  copy,
  onRetry,
}: {
  readonly outcome: Extract<Outcome<unknown>, { kind: 'failed' }>
  readonly copy: Parameters<typeof describeFailure>[1]
  readonly onRetry: () => void
}) {
  const notice = describeFailure(outcome.error, copy)
  return (
    <StateMessage
      icon={outcome.error.isForbidden ? Lock : AlertTriangle}
      title={notice.title}
      detail={notice.detail}
      tone={outcome.error.isForbidden ? 'status' : 'alert'}
      {...(notice.canRetry ? { onRetry } : {})}
    />
  )
}

export function ForecastPerformanceSection({
  hotel,
  period,
  onPeriodChange,
}: ForecastPerformanceSectionProps) {
  const { status, data, range, asOfDate, retry } = useForecastPerformance(hotel, period)

  if (status === 'idle' || status === 'loading' || data === null) {
    return (
      <div className={styles.section}>
        <p className={styles.loading} role="status">
          Loading measured forecast performance…
        </p>
      </div>
    )
  }

  const chart =
    data.daily.kind === 'ready' && data.predictions.kind === 'ready'
      ? pairActualsWithPredictions(data.daily.value.days, data.predictions.value.items)
      : null

  /* The stored-prediction read is one page. A wider window than the page holds is disclosed
     rather than silently presented as the whole history. */
  const truncated =
    data.predictions.kind === 'ready' &&
    data.predictions.value.total > data.predictions.value.items.length

  return (
    <div className={styles.section}>
      <div className={styles.controls}>
        <PeriodSelector value={period} onChange={onPeriodChange} />
        {range === null ? null : (
          <p className={styles.range}>
            {`${formatDate(range.dateFrom)} to ${formatDate(range.dateTo)}`}
            {asOfDate === null ? '' : ` · measured as of ${formatDate(asOfDate)}`}
          </p>
        )}
      </div>

      <section className={styles.panel} aria-label="Forecast against what actually happened">
        <h3 className={styles.panelTitle}>Forecast against actual</h3>
        <p className={styles.panelIntro}>
          Occupied room nights already recorded, beside what the trained model predicted for the
          same day. Two separate series — they are never added, averaged or blended.
        </p>
        {chart === null ? (
          data.daily.kind === 'failed' ? (
            <Failure outcome={data.daily} copy={MEMBER_FAILURE_COPY} onRetry={retry} />
          ) : data.predictions.kind === 'failed' ? (
            <Failure outcome={data.predictions} copy={MEMBER_FAILURE_COPY} onRetry={retry} />
          ) : null
        ) : (
          <>
            <ForecastChart
              metric="Occupied room nights"
              points={chart.points}
              formatTick={formatCount}
              unit="room nights"
              /* No interval exists, so none is named and none is drawn. */
              confidenceLabel={null}
              emptyMessage="No days in this window, so there is nothing to plot."
            />
            {chart.unpredictedDays === 0 ? null : (
              <p className={styles.note}>
                {`${formatCount(chart.unpredictedDays)} day(s) in this window carry no stored prediction. They are drawn as a gap rather than as zero.`}
              </p>
            )}
            {chart.ambiguousDays === 0 ? null : (
              /*
                A target date may hold more than one stored prediction: the uniqueness key
                includes the feature digest, so a late booking produces a second prediction
                beside the first. The server owns the rule for choosing between them when it
                measures; this screen does not reimplement it, and says so rather than
                silently picking one.
              */
              <p className={styles.note}>
                {`${formatCount(chart.ambiguousDays)} day(s) hold more than one stored prediction, because recorded bookings moved the model's inputs after the first was made. Choosing between them is the measurement protocol's rule, not this screen's, so no predicted point is drawn for those days.`}
              </p>
            )}
            {truncated && data.predictions.kind === 'ready' ? (
              <p className={styles.note}>
                {`Showing ${formatCount(data.predictions.value.items.length)} of ${formatCount(data.predictions.value.total)} stored predictions in this window. Choose a shorter period to see all of them.`}
              </p>
            ) : null}
          </>
        )}
      </section>

      <section className={styles.panel} aria-label="Measured forecast error">
        <h3 className={styles.panelTitle}>Measured error</h3>
        {data.accuracy.kind === 'failed' ? (
          <Failure outcome={data.accuracy} copy={ACCURACY_FAILURE_COPY} onRetry={retry} />
        ) : (
          <AccuracyPanel accuracy={data.accuracy.value} />
        )}
      </section>

      <section className={styles.panel} aria-label="Distribution of stored predictions">
        <h3 className={styles.panelTitle}>Prediction distribution</h3>
        {data.distribution.kind === 'failed' ? (
          <Failure outcome={data.distribution} copy={MEMBER_FAILURE_COPY} onRetry={retry} />
        ) : (
          <DistributionPanel distribution={data.distribution.value} />
        )}
      </section>
    </div>
  )
}
