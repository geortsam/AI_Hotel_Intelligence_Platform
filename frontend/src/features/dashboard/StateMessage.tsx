import type { LucideIcon } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'

import styles from './StateMessage.module.css'

export interface StateMessageProps {
  readonly icon: LucideIcon
  readonly title: string
  readonly detail: string
  /** Offered only when repeating the request could plausibly help. */
  readonly onRetry?: () => void
  /**
   * `alert` for a failure, `status` for an empty result.
   *
   * The distinction is not decoration. `alert` interrupts a screen reader, which is right
   * for "this did not load" and wrong for "this hotel had no bookings last week" -- the
   * second is an ordinary answer, and announcing it as an alert would make a quiet week
   * sound like a fault.
   */
  readonly tone: 'alert' | 'status'
}

/**
 * The panel that replaces the dashboard when there is no dashboard to show.
 *
 * One component for three situations the brief insists are different, and which this
 * application keeps different all the way down: a **request that failed**, a period with
 * **no data**, and an account with **no hotel**. They read differently, they are announced
 * differently, and only the first offers a retry.
 *
 * What none of them does is show a zero. A `0` is a measurement -- it says the hotel sold
 * nothing -- and printing it because a request timed out is the most consequential lie a
 * dashboard can tell, because it is indistinguishable from a real bad week.
 */
export function StateMessage({ icon: Icon, title, detail, onRetry, tone }: StateMessageProps) {
  return (
    <Card className={styles.card}>
      <div className={styles.body} role={tone === 'alert' ? 'alert' : 'status'}>
        <Icon className={styles.icon} size={28} aria-hidden="true" />
        <h3 className={styles.title}>{title}</h3>
        <p className={styles.detail}>{detail}</p>
        {onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            Try again
          </Button>
        ) : null}
      </div>
    </Card>
  )
}
