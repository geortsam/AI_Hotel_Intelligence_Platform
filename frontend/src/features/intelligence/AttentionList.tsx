import { Activity, CalendarRange, TrendingUp } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { formatDate } from '@/lib/format'
import type { PriorityItem, PriorityKind } from '@/types/intelligence'

import styles from './InsightList.module.css'

export interface AttentionListProps {
  readonly items: readonly PriorityItem[]
}

/**
 * The Stage 7.12 attention list, as the server ranked it.
 *
 * ## Nothing here is decided in the browser
 *
 * Which days are listed, in what order, and every sentence -- the observation, the comparison
 * and the limitation -- come from `GET …/intelligence/priorities`, where fixed templates are
 * filled from the item's own figures. This component prints them. It ranks nothing, filters
 * nothing, and adds no severity: the contract carries a rank and a kind, not a severity, and
 * grading items here would be a second implementation of the ranking.
 *
 * ## The rank is the priority
 *
 * Items arrive in list order -- peak days first, then anomalies, then the trend -- and `rank`
 * is the item's position in that whole list, 1 first. Both are shown as the server sent them.
 */
const KIND: Readonly<Record<PriorityKind, { label: string; icon: typeof Activity }>> = {
  upcoming_peak_day: { label: 'Upcoming peak day', icon: CalendarRange },
  observed_anomaly: { label: 'Observed anomaly', icon: Activity },
  demand_trend: { label: 'Demand trend', icon: TrendingUp },
}

export function AttentionList({ items }: AttentionListProps) {
  return (
    <ol className={styles.list}>
      {items.map((item) => {
        const shown = KIND[item.kind]
        const Icon = shown.icon
        return (
          <li key={`${item.kind}-${item.rank}-${item.date_from}`} className={styles.item}>
            <div className={styles.header}>
              <Icon size={18} aria-hidden="true" className={styles.icon} />
              <h3 className={styles.title}>{shown.label}</h3>
              <Badge tone="neutral">{`Rank ${item.rank}`}</Badge>
            </div>

            <p className={styles.explanation}>{item.observation}</p>
            {item.comparison === null ? null : (
              <p className={styles.explanation}>{item.comparison}</p>
            )}

            <p className={styles.meta}>
              <span className={styles.type}>{item.measure}</span>
              <span className={styles.dates}>
                {formatDate(item.date_from)} &ndash; {formatDate(item.date_to)}
              </span>
            </p>

            <p className={styles.meta}>{item.limitation}</p>

            {item.figures.length === 0 ? null : (
              <dl className={styles.metrics}>
                {item.figures.map((figure) => (
                  <div key={figure.name} className={styles.metric}>
                    <dt className={styles.metricName}>{figure.name}</dt>
                    <dd className={styles.metricValue}>
                      {figure.value} <span className={styles.type}>{figure.unit}</span>
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </li>
        )
      })}
    </ol>
  )
}
