import type { HTMLAttributes, ReactNode } from 'react'

import styles from './Card.module.css'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Adds internal padding. Turn it off when the card holds a table that should meet the edge. */
  readonly padded?: boolean
  /** Lifts the card off the page. For overlays and popovers, not for every panel. */
  readonly raised?: boolean
  readonly children: ReactNode
}

/**
 * The panel every grouped piece of content sits in.
 *
 * `padded` is opt-out rather than fixed because the two common cases genuinely differ: a
 * summary panel wants padding, and a table wants to run to the card's edge. Baking padding
 * in would mean every table page cancelling it with a negative margin.
 */
export function Card({ padded = true, raised = false, className, children, ...rest }: CardProps) {
  const classes = [styles.card, padded ? styles.padded : undefined, raised ? styles.raised : undefined, className]
    .filter(Boolean)
    .join(' ')

  return (
    <div className={classes} {...rest}>
      {children}
    </div>
  )
}

export interface CardHeaderProps {
  readonly title: string
  /** Actions for this card, e.g. a filter or an overflow menu. */
  readonly actions?: ReactNode
}

/**
 * A card's title row.
 *
 * Renders an `<h3>`: cards sit under a page `<h1>` and a section `<h2>`, so this is the
 * level that keeps the document outline intact for anyone navigating by heading.
 */
export function CardHeader({ title, actions }: CardHeaderProps) {
  return (
    <div className={styles.header}>
      <h3 className={styles.title}>{title}</h3>
      {actions}
    </div>
  )
}

/** The padded region below a {@link CardHeader}, for use with `padded={false}` cards. */
export function CardBody({ children }: { readonly children: ReactNode }) {
  return <div className={styles.body}>{children}</div>
}
