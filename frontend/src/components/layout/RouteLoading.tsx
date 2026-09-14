import { PageContainer } from '@/components/ui/PageContainer'
import { Skeleton } from '@/components/ui/Skeleton'

import styles from './RouteLoading.module.css'

/**
 * Shown while a route's own code is being fetched.
 *
 * Every screen except the dashboard and the login page is now loaded on demand, so between
 * clicking a navigation item and seeing that screen there is a moment where the code for it
 * is still arriving. This stands in for it.
 *
 * It renders INSIDE the shell's `<main>`, not over it: the sidebar, the header and the
 * breadcrumb are already loaded and already correct for the destination, so replacing them
 * with a full-screen spinner would throw away context the user can already act on -- and
 * would make a 40ms chunk fetch look like a page reload.
 *
 * Deliberately shaped like a page rather than centred on a spinner: a heading, then a panel.
 * That is the layout of every screen behind it, so the content does not jump when it
 * arrives. It shows no figures and names no property -- there is nothing to report yet, and
 * inventing a placeholder number here would be the dashboard's zero problem all over again.
 *
 * One `role="status"` on the region, with the individual bars hidden, so a screen reader is
 * told once that a page is loading rather than six times that something is.
 */
export function RouteLoading() {
  return (
    <PageContainer>
      <div className={styles.region} role="status" aria-label="Loading the page">
        <div className={styles.header}>
          <Skeleton width="14rem" height="1.75rem" />
          <Skeleton width="24rem" height="0.875rem" />
        </div>
        <div className={styles.panel}>
          <Skeleton width="100%" height="1rem" />
          <Skeleton width="92%" height="1rem" />
          <Skeleton width="78%" height="1rem" />
        </div>
      </div>
    </PageContainer>
  )
}
