import { AlertTriangle, Info, OctagonAlert } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { formatDate, formatMoney } from '@/lib/format'
import type { Insight, InsightSeverity } from '@/types/intelligence'

import styles from './InsightList.module.css'

export interface InsightListProps {
  readonly insights: readonly Insight[]
}

/**
 * The server's structured findings.
 *
 * ## These are not generated sentences, and the page says so
 *
 * `explanation` is a deterministic template filled from the numbers in `supporting_metrics`.
 * The same data always produces the same sentence, there is no language model anywhere in
 * this stage, and presenting these as "AI insights" would be a claim the system does not
 * make. The heading above this list calls them findings and states that they are templates.
 *
 * ## Severity is the server's, and it is a word before it is a colour
 *
 * `severity` is a real field here — unlike on an anomaly, which has none. It is rendered as a
 * badge whose text carries the meaning; the icon repeats it in shape and the tone repeats it
 * in colour, so no reader depends on hue.
 *
 * ## Supporting metrics are printed, not recombined
 *
 * Each is a name and a string value, with a currency where the metric is monetary. A monetary
 * value is formatted with its own currency and nothing else — no total across metrics, no
 * conversion, no arithmetic of any kind.
 */
const SEVERITY: Readonly<
  Record<InsightSeverity, { label: string; icon: typeof Info; tone: 'neutral' | 'warning' | 'danger' }>
> = {
  info: { label: 'Information', icon: Info, tone: 'neutral' },
  warning: { label: 'Warning', icon: AlertTriangle, tone: 'warning' },
  critical: { label: 'Critical', icon: OctagonAlert, tone: 'danger' },
}

export function InsightList({ insights }: InsightListProps) {
  return (
    <ul className={styles.list}>
      {insights.map((insight, index) => {
        const shown = SEVERITY[insight.severity]
        const Icon = shown.icon
        return (
          <li key={`${insight.type}-${insight.date_from}-${index}`} className={styles.item}>
            <div className={styles.header}>
              <Icon size={18} aria-hidden="true" className={styles.icon} />
              <h3 className={styles.title}>{insight.title}</h3>
              <Badge tone={shown.tone}>{shown.label}</Badge>
            </div>

            <p className={styles.explanation}>{insight.explanation}</p>

            <p className={styles.meta}>
              <span className={styles.type}>{insight.type}</span>
              <span className={styles.dates}>
                {formatDate(insight.date_from)} &ndash; {formatDate(insight.date_to)}
              </span>
              {insight.confidence === null ? null : (
                <span className={styles.confidence}>
                  Model confidence {insight.confidence}
                </span>
              )}
            </p>

            {insight.supporting_metrics.length === 0 ? null : (
              <dl className={styles.metrics}>
                {insight.supporting_metrics.map((metric) => (
                  <div key={metric.name} className={styles.metric}>
                    <dt className={styles.metricName}>{metric.name}</dt>
                    <dd className={styles.metricValue}>
                      {/* Formatted with its own currency where there is one, and never
                          combined with another metric's. */}
                      {metric.currency ? formatMoney(metric.value, metric.currency) : metric.value}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </li>
        )
      })}
    </ul>
  )
}
