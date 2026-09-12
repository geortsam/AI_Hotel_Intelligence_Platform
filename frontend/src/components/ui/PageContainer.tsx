import type { ReactNode } from 'react'

import styles from './PageContainer.module.css'

export interface PageContainerProps {
  readonly children: ReactNode
}

/**
 * The width and padding every page shares.
 *
 * One place decides how wide content runs and how far it sits from the edge, so pages do
 * not each pick their own and drift apart. The max width keeps line lengths readable on an
 * ultrawide monitor rather than letting a table stretch to 3000px.
 */
export function PageContainer({ children }: PageContainerProps) {
  return <div className={styles.container}>{children}</div>
}
