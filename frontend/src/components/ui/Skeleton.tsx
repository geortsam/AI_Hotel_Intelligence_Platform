import styles from './Skeleton.module.css'

export interface SkeletonProps {
  /** Any CSS length. Defaults to filling the available width. */
  readonly width?: string
  /** Any CSS length. Defaults to one line of body text. */
  readonly height?: string
  /** Describes what is loading, for screen readers. */
  readonly label?: string
}

/**
 * A loading placeholder shaped like the content it stands in for.
 *
 * Exists now, before any screen fetches anything, because every later stage will need it
 * and a shared one keeps loading states from being invented per page.
 *
 * `role="status"` announces the wait; when several skeletons make up one loading region,
 * label the region and leave the individual bars unlabelled rather than announcing six
 * separate "loading" messages.
 */
export function Skeleton({ width = '100%', height = '1rem', label }: SkeletonProps) {
  return (
    <span
      className={styles.skeleton}
      style={{ width, height }}
      role={label ? 'status' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : 'true'}
    />
  )
}
