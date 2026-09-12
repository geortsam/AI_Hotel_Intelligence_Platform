import { ArrowDown, ArrowUp } from 'lucide-react'

import { formatDate } from '@/lib/format'
import { useIsCompact } from '@/lib/useIsCompact'
import type { AnomalyPoint } from '@/types/intelligence'

import styles from './AnomalyList.module.css'

export interface AnomalyListProps {
  readonly anomalies: readonly AnomalyPoint[]
}

/**
 * Days the server flagged as unusual, with the whole statistical basis for each flag.
 *
 * ## There is no severity here, because the contract has none
 *
 * `AnomalyPoint` carries a metric, a date, a value, the window median, the MAD, a modified
 * z-score, the threshold and a direction. It does **not** carry a severity, and none is
 * derived: grading these into "critical" and "warning" would be a frontend heuristic wearing
 * the server's authority. The z-score and the threshold are shown instead — they are what
 * the judgement was actually made on, and a reader can check the arithmetic.
 *
 * ## Direction is a word and an arrow, never a colour
 *
 * `above` and `below` are printed, and the arrow repeats the same fact in shape. An unusual
 * day is not inherently good or bad — an occupancy spike is a good problem and a revenue
 * collapse is a bad one, and the same red would be wrong for one of them — so no judgement
 * is coloured in.
 *
 * ## The metric name is the server's
 *
 * `room_revenue[EUR]` is rendered as sent. The currency is inside the metric name because the
 * backend scans each currency's series separately, and rewriting it would obscure that.
 */
export function AnomalyList({ anomalies }: AnomalyListProps) {
  const isCompact = useIsCompact()

  if (isCompact) {
    return (
      <ul className={styles.cards}>
        {anomalies.map((anomaly) => (
          <li key={`${anomaly.metric}-${anomaly.date}`} className={styles.card}>
            <div className={styles.cardHeader}>
              <span className={styles.metric}>{anomaly.metric}</span>
              <span className={styles.when}>{formatDate(anomaly.date)}</span>
            </div>
            <p className={styles.reading}>
              <Direction direction={anomaly.direction} />
              <span className={styles.value}>{anomaly.value}</span>
              <span className={styles.against}>
                against a median of {anomaly.median}
              </span>
            </p>
            <dl className={styles.fields}>
              <Field label="Modified z-score">{anomaly.modified_z_score}</Field>
              <Field label="Threshold">{anomaly.threshold}</Field>
              <Field label="Median absolute deviation">{anomaly.median_absolute_deviation}</Field>
            </dl>
          </li>
        ))}
      </ul>
    )
  }

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          Days the model flagged as unusual, ordered by metric then date. Each row carries the
          figures the flag was computed from, so it can be checked by hand.
        </caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Metric</th>
            <th scope="col">Direction</th>
            <th scope="col">Value</th>
            <th scope="col">Window median</th>
            <th scope="col">MAD</th>
            <th scope="col">Modified z-score</th>
            <th scope="col">Threshold</th>
          </tr>
        </thead>
        <tbody>
          {anomalies.map((anomaly) => (
            <tr key={`${anomaly.metric}-${anomaly.date}`}>
              <th scope="row" className={styles.whenCell}>
                {formatDate(anomaly.date)}
              </th>
              <td className={styles.metricCell}>{anomaly.metric}</td>
              <td>
                <Direction direction={anomaly.direction} />
              </td>
              <td className={styles.numeric}>{anomaly.value}</td>
              <td className={styles.numeric}>{anomaly.median}</td>
              <td className={styles.numeric}>{anomaly.median_absolute_deviation}</td>
              <td className={styles.numeric}>{anomaly.modified_z_score}</td>
              <td className={styles.numeric}>{anomaly.threshold}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** The direction, as a word plus a matching arrow. Never colour alone. */
function Direction({ direction }: { direction: 'above' | 'below' }) {
  const Icon = direction === 'above' ? ArrowUp : ArrowDown
  return (
    <span className={styles.direction}>
      <Icon size={14} aria-hidden="true" />
      {direction === 'above' ? 'Above normal' : 'Below normal'}
    </span>
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
