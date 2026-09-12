import { Minus, TrendingDown, TrendingUp, HelpCircle } from 'lucide-react'

import { UNAVAILABLE } from '@/lib/format'
import type { DemandTrend, TrendDirection } from '@/types/intelligence'

import styles from './TrendSummary.module.css'

export interface TrendSummaryProps {
  readonly trend: DemandTrend
}

/**
 * How the server classified booking demand, and the two medians it used.
 *
 * ## The verdict is the server's, and so is everything under it
 *
 * `direction` is read, never derived. The frontend does not compare the two medians itself,
 * does not compute a percentage change, and does not decide what counts as "increasing" — the
 * threshold is a field of the response. Showing the medians and the threshold beside the
 * verdict is what lets an operator check the classification instead of trusting it.
 *
 * ## `relative_change` is genuinely undefined sometimes
 *
 * When the earlier half's median is zero there is no ratio to report, and the backend sends
 * null rather than an infinite rise. That is printed as "not defined", with the reason — a
 * dash alone would read like a missing value rather than an undefined one.
 *
 * ## `insufficient_data` is a fourth state, not a failure
 *
 * A window too sparse to split is answered honestly by the API and is rendered honestly
 * here. It is not "stable".
 */
const PRESENTATION: Readonly<
  Record<TrendDirection, { label: string; icon: typeof TrendingUp; tone: string }>
> = {
  increasing: { label: 'Increasing', icon: TrendingUp, tone: 'up' },
  decreasing: { label: 'Decreasing', icon: TrendingDown, tone: 'down' },
  stable: { label: 'Stable', icon: Minus, tone: 'flat' },
  insufficient_data: { label: 'Not enough data', icon: HelpCircle, tone: 'unknown' },
}

export function TrendSummary({ trend }: TrendSummaryProps) {
  const shown = PRESENTATION[trend.direction]
  const Icon = shown.icon

  return (
    <div className={styles.summary}>
      <p className={`${styles.verdict} ${styles[shown.tone] ?? ''}`}>
        <Icon size={20} aria-hidden="true" />
        {/* The word carries the meaning; the icon and colour only repeat it. */}
        <span className={styles.verdictLabel}>{shown.label}</span>
      </p>

      <p className={styles.basis}>
        {trend.direction === 'insufficient_data'
          ? 'The observation window did not hold enough booking history to split in half and compare.'
          : 'Classified by comparing the median of the window’s earlier half with its recent half. Both medians and the threshold are below, so the classification can be checked.'}
      </p>

      <dl className={styles.fields}>
        <Field label="Metric">{trend.metric}</Field>
        <Field label="Earlier half median">{trend.earlier_median ?? UNAVAILABLE}</Field>
        <Field label="Recent half median">{trend.recent_median ?? UNAVAILABLE}</Field>
        <Field label="Relative change">
          {trend.relative_change ?? (
            <span className={styles.absent}>
              Not defined &mdash; the earlier half&rsquo;s median was zero
            </span>
          )}
        </Field>
        <Field label="Threshold">{trend.threshold}</Field>
        <Field label="Observed days">{String(trend.window.days)}</Field>
      </dl>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <dt className={styles.fieldLabel}>{label}</dt>
      <dd className={styles.fieldValue}>{children}</dd>
    </div>
  )
}
