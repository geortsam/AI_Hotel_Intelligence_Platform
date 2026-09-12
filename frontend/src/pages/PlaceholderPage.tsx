import { Construction } from 'lucide-react'

import { Card } from '@/components/ui/Card'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'

import styles from './PlaceholderPage.module.css'

export interface PlaceholderPageProps {
  readonly title: string
  /** What this area will do, in the platform's own terms. */
  readonly description: string
  /** The stage that builds it. Naming it keeps "coming soon" from being indefinite. */
  readonly plannedStage: string
}

/**
 * What a navigation target renders before its feature exists.
 *
 * The alternative was a link that 404s or a screen of invented figures. Both lie in
 * different directions -- one looks broken, the other looks finished -- so this says
 * plainly that the area is planned, what it will contain, and which stage delivers it.
 *
 * **It fetches nothing and displays no data**, because there is no data here that would
 * not be fabricated.
 */
export function PlaceholderPage({ title, description, plannedStage }: PlaceholderPageProps) {
  return (
    <PageContainer>
      <SectionHeader as="h2" title={title} description={description} />
      <Card>
        <div className={styles.empty}>
          <span className={styles.icon} aria-hidden="true">
            <Construction size={20} />
          </span>
          <p className={styles.headline}>This area has not been built yet</p>
          <p className={styles.detail}>
            Planned for {plannedStage}. The application shell, navigation and design system
            are in place; no data is requested from the backend on this page.
          </p>
        </div>
      </Card>
    </PageContainer>
  )
}
