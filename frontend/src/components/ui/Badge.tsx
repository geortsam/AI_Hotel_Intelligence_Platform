import type { ReactNode } from 'react'

import styles from './Badge.module.css'

export type BadgeTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info'

export interface BadgeProps {
  readonly tone?: BadgeTone
  /** Shows a coloured dot before the label. Reinforcement only -- the label carries the meaning. */
  readonly withDot?: boolean
  readonly children: ReactNode
}

/**
 * A small status label.
 *
 * **Colour is never the only signal.** Every badge shows text, and the optional dot only
 * repeats what the text already says. Roughly one man in twelve has some form of colour
 * blindness, and a booking state shown purely as a green or red pill is unreadable to
 * them -- which matters more here than usual, because status is the thing an operations
 * console is mostly displaying.
 */
export function Badge({ tone = 'neutral', withDot = false, children }: BadgeProps) {
  return (
    <span className={`${styles.badge} ${styles[tone]}`}>
      {withDot ? <span className={styles.dot} aria-hidden="true" /> : null}
      {children}
    </span>
  )
}
