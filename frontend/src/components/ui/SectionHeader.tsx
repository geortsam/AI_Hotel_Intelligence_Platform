import type { ReactNode } from 'react'

import styles from './SectionHeader.module.css'

export interface SectionHeaderProps {
  readonly title: string
  readonly description?: string
  /** Controls belonging to this section, e.g. a date range or a "New booking" button. */
  readonly actions?: ReactNode
  /**
   * Heading level. Defaults to `h2`, which is right for a section under a page title;
   * pass `h1` when this header *is* the page title.
   */
  readonly as?: 'h1' | 'h2'
}

/**
 * A titled section heading with optional description and actions.
 *
 * The level is a prop rather than fixed because heading order is a document-structure
 * guarantee, not styling: skipping from `h1` to `h3` breaks navigation for anyone moving
 * through the page by heading, and hard-coding one level here would force that on callers.
 */
export function SectionHeader({ title, description, actions, as = 'h2' }: SectionHeaderProps) {
  const Heading = as

  return (
    <div className={styles.header}>
      <div className={styles.text}>
        <Heading className={styles.title}>{title}</Heading>
        {description ? <p className={styles.description}>{description}</p> : null}
      </div>
      {actions ? <div className={styles.actions}>{actions}</div> : null}
    </div>
  )
}
