import { Badge } from '@/components/ui/Badge'
import { statusPresentation } from '@/features/bookings/vocabulary'
import type { BookingStatus } from '@/types/booking'

import styles from './BookingStatusBadge.module.css'

export interface BookingStatusBadgeProps {
  readonly status: BookingStatus
}

/**
 * A booking's status, readable three ways.
 *
 * The word carries the meaning. The icon reinforces it for anyone scanning a column of forty
 * rows. The tone reinforces it again for everyone who reads colour -- and for everyone who
 * does not, removing it changes nothing, which is the test.
 *
 * The longer description goes in a visually hidden span rather than a `title` attribute:
 * `title` is invisible to touch, unreliable to keyboard, and inconsistently announced. This
 * way "Checked in" is followed by "the guest is in the property now" for a screen reader,
 * and the badge stays four characters wide on screen.
 *
 * Built on the shared `Badge`, not a second pill style. The design system already answers
 * "what does a small status label look like here".
 */
export function BookingStatusBadge({ status }: BookingStatusBadgeProps) {
  const { label, tone, icon: Icon, description } = statusPresentation(status)

  return (
    <Badge tone={tone}>
      <Icon size={12} aria-hidden="true" className={styles.icon} />
      {label}
      <span className={styles.srOnly}>. {description}</span>
    </Badge>
  )
}
