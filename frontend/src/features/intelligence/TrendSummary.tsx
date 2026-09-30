import { CircleDashed, CircleOff, HelpCircle, Minus, TrendingDown, TrendingUp } from 'lucide-react'

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
 *
 * ## `no_activity` is a fifth, and it is not "stable" either
 *
 * A window in which no booking was taken on any day holds no demand whose direction could be
 * observed. The server says so, and so does this label.
 *
 * ## `sparse_activity` is a sixth: bookings were taken, and no direction can be given
 *
 * When more than half the days of each half had no bookings, both medians are zero and the
 * comparison is blind to the bookings the window does hold. That used to read "Stable". It is
 * labelled for what it is, and like `no_activity` it carries no direction.
 */
const PRESENTATION: Readonly<
  Record<TrendDirection, { label: string; icon: typeof TrendingUp; tone: string }>
> = {
  increasing: { label: 'Increasing', icon: TrendingUp, tone: 'up' },
  decreasing: { label: 'Decreasing', icon: TrendingDown, tone: 'down' },
  stable: { label: 'Stable', icon: Minus, tone: 'flat' },
  no_activity: { label: 'No booking activity', icon: CircleOff, tone: 'unknown' },
  sparse_activity: { label: 'Too sparse to judge', icon: CircleDashed, tone: 'unknown' },
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
          : trend.direction === 'no_activity'
            ? 'No booking was taken on any day of the observation window, so there is no demand whose direction could be classified.'
            : trend.direction === 'sparse_activity'
              ? 'Bookings were taken, but on more than half the days of each half of the window there were none. Both medians are therefore zero and cannot register those bookings, so no direction — stable included — is reported.'
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
