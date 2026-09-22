import { Info } from 'lucide-react'

import type { ForecastOutcome } from './useAnalyticsReport'
import styles from './DemandForecastPanel.module.css'

export interface DemandForecastPanelProps {
  /** Null while the request is in flight. */
  readonly outcome: ForecastOutcome | null
  readonly isLoading: boolean
}

/**
 * The served demand model's estimate, and everything a reader needs to weigh it.
 *
 * This is the first screen in the application to call the trained model at all. It has been
 * served since Stage 6.6 and packaged into the API image since 6.7; nothing in the browser had
 * asked it anything until now.
 *
 * ## The separation this component exists to make
 *
 * Every other figure on the analytics screen is **recorded**: it happened, the database has the
 * rows. This one is **modelled**: it did not happen, and may not. Mixing the two would be the
 * single most misleading thing this screen could do, so the separation is structural rather
 * than a caption — its own panel, its own visual treatment, its own heading that says
 * "estimate", and the word "forecast" never used as a synonym for a fact.
 *
 * ## What is deliberately not shown
 *
 * * **No confidence interval.** The served model is a gradient-boosted point forecaster. It
 *   produces one number and no distribution, and drawing a band around it would be inventing
 *   uncertainty rather than reporting it. A band needs quantile regression or conformal
 *   prediction and a backtest measuring its coverage; none exists.
 * * **No accuracy claim.** The model card records `production_accuracy_established: No`, and
 *   the API's own metadata reports `production_ready: false`. Both are surfaced here, not
 *   hidden. Stage 6.9 measures error under a declared protocol and that measurement has no
 *   endpoint yet — until it does, this screen states what the model is and stops.
 * * **No up/down indicator against the recorded period.** The estimate is for a single future
 *   day and the KPIs above cover a past window; comparing them would be comparing a point to
 *   an aggregate over a different span.
 *
 * ## A refusal is an answer
 *
 * `422 INSUFFICIENT_HISTORY` is the model declining because the days its lag features read hold
 * no occupancy — which is the serving stage working exactly as designed, and is what a new
 * property will see. It renders as an explanation, not as a fault, and never as a zero.
 */
export function DemandForecastPanel({ outcome, isLoading }: DemandForecastPanelProps) {
  if (isLoading || outcome === null) {
    return (
      <div className={styles.panel} aria-busy="true">
        <p className={styles.eyebrow}>Modelled estimate</p>
        <p className={styles.pending}>Asking the model…</p>
      </div>
    )
  }

  if (outcome.kind === 'unavailable') {
    return (
      <div className={styles.panel}>
        <p className={styles.eyebrow}>Modelled estimate</p>
        <p className={styles.unavailableTitle}>No estimate for tomorrow</p>
        <p className={styles.unavailableDetail}>{outcome.detail}</p>
        <p className={styles.note}>
          {outcome.code === 'INSUFFICIENT_HISTORY'
            ? 'The model reads recorded occupancy from three earlier dates. Where those days hold no rows it declines rather than guessing.'
            : 'The model did not answer. Nothing is estimated in its place.'}
        </p>
      </div>
    )
  }

  const { prediction } = outcome
  const { model } = prediction

  return (
    <div className={styles.panel}>
      <p className={styles.eyebrow}>Modelled estimate — not a recorded figure</p>

      <p className={styles.targetDate}>
        Occupied room nights on <strong>{formatDay(prediction.target_date)}</strong>
      </p>

      {/* One decimal, through `Intl` rather than `toFixed`. The model emits a float at full
          precision and showing all of it would imply a resolution the estimate does not have.
          `Intl` is how every other figure in this application is formatted, and it keeps the
          rounding in one well-understood place. */}
      <p className={styles.value}>{formatRoomNights(prediction.predicted_room_nights)}</p>

      <p className={styles.caption}>
        Forecast {prediction.forecast_horizon_days} days ahead, using demand recorded up to{' '}
        {formatDay(prediction.cutoff_date)}. Nothing booked after that influenced it.
      </p>

      <dl className={styles.provenance}>
        <div className={styles.provenanceRow}>
          <dt>Model</dt>
          <dd>{model.model_version}</dd>
        </div>
        <div className={styles.provenanceRow}>
          <dt>Features</dt>
          <dd>{model.feature_version}</dd>
        </div>
        <div className={styles.provenanceRow}>
          <dt>Dataset</dt>
          <dd>{model.dataset_version}</dd>
        </div>
        <div className={styles.provenanceRow}>
          <dt>Status</dt>
          <dd>{model.status.replace(/_/g, ' ')}</dd>
        </div>
      </dl>

      <p className={styles.boundary}>
        <Info className={styles.boundaryIcon} size={15} aria-hidden="true" />
        <span>
          {model.production_ready
            ? 'This model is marked production ready.'
            : 'This model is not production ready and its accuracy against real hotels has not been established. Treat the number as an estimate to weigh, not a figure to plan against.'}
        </span>
      </p>
    </div>
  )
}

/**
 * The estimate, to one decimal place.
 *
 * `Intl.NumberFormat`, not `toFixed`: the same formatter every other number on the screen goes
 * through, and the only place this value's displayed resolution is decided. The underlying
 * float is never modified, re-parsed or arithmetic'd — it is read once and formatted once.
 */
function formatRoomNights(value: number, locale = 'en-GB'): string {
  return new Intl.NumberFormat(locale, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(value)
}

/** `2026-12-01` → `1 Dec 2026`. UTC, because the server's dates carry no time. */
function formatDay(iso: string): string {
  const [year, month, day] = iso.split('-').map(Number) as [number, number, number]
  return new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, month - 1, day)))
}
