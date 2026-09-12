import { ArrowDownRight, ArrowRight, ArrowUpRight } from 'lucide-react'

import { Card } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { formatDelta, UNAVAILABLE, type Delta } from '@/features/dashboard/format'

import styles from './KpiCard.module.css'

export interface KpiCardProps {
  /** What the metric is called, e.g. `Occupancy`. */
  readonly label: string
  /**
   * The formatted figure, or **null when the backend reported the metric as undefined**.
   *
   * Null is not zero and is not an error. A hotel with no sold room nights has an undefined
   * ADR -- the backend says so with an explicit `null`, mirroring the `NULLIF` guards on its
   * own generated columns -- and rendering that as `0.00` would drag every reading of the
   * page downwards. The card shows a dash and says why.
   */
  readonly value: string | null
  /** Shown when `value` is null, in place of a number. Say what is missing, not "error". */
  readonly unavailableReason?: string
  /** One line under the figure: what it counts and where it came from. */
  readonly caption?: string
  /** Period-over-period change. Omitted entirely when it cannot be stated honestly. */
  readonly delta?: Delta | null
  /** Names the window a delta is against, e.g. `vs previous 7 days`. */
  readonly deltaLabel?: string
  readonly isLoading?: boolean
}

const DELTA_ICON = {
  up: ArrowUpRight,
  down: ArrowDownRight,
  flat: ArrowRight,
} as const

/**
 * One figure, with everything needed to read it correctly.
 *
 * A KPI tile that shows only a number is a tile that can be misread, so each one carries its
 * name, its unit or currency, the period it covers (through the page's own period control),
 * and -- when there is one -- a comparison that names the window it compares against.
 *
 * ## Four states, kept distinct
 *
 * **Loading** is a skeleton the size of the eventual figure, so nothing moves when the data
 * lands. **Ready** is a figure. **Unavailable** is a dash plus a reason, for a metric the
 * backend explicitly declined to define. **Failed** is not a state of this card at all: when
 * a request fails the page replaces the whole grid rather than showing tiles, because a
 * dashboard of dashes looks like a hotel with no business rather than a request that did not
 * arrive.
 *
 * ## Direction is never colour alone
 *
 * A rise carries an arrow, a sign and a word in its accessible name. Colour repeats what
 * those already say. Around one man in twelve cannot rely on the red/green distinction, and
 * "is this up or down" is the single thing a KPI comparison exists to answer.
 *
 * Rendered as `<dt>`/`<dd>` inside the page's definition list: a KPI genuinely is a term and
 * its value, and the pairing is what lets a screen reader announce "Occupancy, 58.0%" rather
 * than reading two unrelated fragments.
 */
export function KpiCard({
  label,
  value,
  unavailableReason,
  caption,
  delta,
  deltaLabel,
  isLoading = false,
}: KpiCardProps) {
  const DeltaIcon = delta ? DELTA_ICON[delta.direction] : null

  return (
    <Card className={styles.card}>
      <dt className={styles.label}>{label}</dt>
      <dd className={styles.valueRow}>
        {isLoading ? (
          <Skeleton width="60%" height="1.875rem" />
        ) : (
          <span className={value === null ? styles.valueUnavailable : styles.value}>
            {value ?? UNAVAILABLE}
          </span>
        )}
      </dd>

      {isLoading ? (
        <p className={styles.caption}>
          <Skeleton width="80%" height="0.75rem" />
        </p>
      ) : value === null && unavailableReason ? (
        <p className={styles.caption}>{unavailableReason}</p>
      ) : caption ? (
        <p className={styles.caption}>{caption}</p>
      ) : null}

      {!isLoading && delta && DeltaIcon ? (
        <p className={`${styles.delta} ${styles[delta.direction]}`}>
          <DeltaIcon size={14} aria-hidden="true" />
          <span>{formatDelta(delta)}</span>
          {deltaLabel ? <span className={styles.deltaLabel}>{deltaLabel}</span> : null}
        </p>
      ) : null}
    </Card>
  )
}
