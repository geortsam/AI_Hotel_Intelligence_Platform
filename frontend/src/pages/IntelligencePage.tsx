import { useId, useState, type ReactNode } from 'react'
import { AlertTriangle, Building2, Lightbulb, Radar, WifiOff } from 'lucide-react'

import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { StateMessage } from '@/features/dashboard/StateMessage'
import type { PeriodId } from '@/features/dashboard/period'
import { ForecastPerformanceSection } from '@/features/forecastPerformance/ForecastPerformanceSection'
import { AnomalyList } from '@/features/intelligence/AnomalyList'
import { ForecastChart, type ForecastChartPoint } from '@/features/intelligence/ForecastChart'
import { InsightList } from '@/features/intelligence/InsightList'
import { TrendSummary } from '@/features/intelligence/TrendSummary'
import {
  useIntelligence,
  type Resource,
} from '@/features/intelligence/useIntelligence'
import { formatCount, formatDate, formatMoney } from '@/lib/format'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useHotelContext } from '@/session/HotelProvider'
import {
  DEFAULT_HORIZON_DAYS,
  DEFAULT_TRAINING_DAYS,
  type ModelMetadata,
} from '@/types/intelligence'

import styles from './IntelligencePage.module.css'

/** Every option is inside the routers' own bounds, so none can produce a 422. */
const OBSERVATION_OPTIONS = [30, 60, 90, 180] as const
const HORIZON_OPTIONS = [7, 14, 30, 60] as const
const TRAINING_OPTIONS = [14, 30, 90, 180, 365] as const

type TabId = 'occupancy' | 'revenue'

const TABS: readonly { readonly id: TabId; readonly label: string }[] = [
  { id: 'occupancy', label: 'Occupancy' },
  { id: 'revenue', label: 'Revenue' },
]

/**
 * Failure copy.
 *
 * There is **no `forbidden` override** and that is deliberate: these endpoints require
 * membership and no role beyond it, and a caller who is not a member gets a 404 with the same
 * message as a hotel that does not exist. A 403 is therefore not a state this screen can
 * reach through the documented contract, and writing copy for one would describe a permission
 * system the API does not have.
 */
const FAILURE_COPY = {
  notFound: {
    title: 'Intelligence is not available for this property',
    detail:
      'The property could not be found, or your access to it has been removed. The API answers the same way for both, so there is nothing more specific to say.',
    canRetry: false,
  },
  serverFault: {
    title: 'Intelligence is temporarily unavailable',
    detail: 'The models could not be reached. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

/**
 * Intelligence &mdash; forecasts, demand trend, anomalies and structured findings.
 *
 * ## Predictions and facts are kept apart, everywhere
 *
 * A hotel already knows part of its future: next month's bookings exist today. Every forecast
 * day therefore shows the room nights (or revenue) **already on the books** beside the
 * model's estimate, as two distinct series that are never summed, averaged or blended. That
 * is the single most important property of this screen, and it is the reason the forecast
 * needs its own chart rather than the dashboard's single-series one.
 *
 * ## Nothing on this page is computed in the browser
 *
 * No forecast, no trend classification, no anomaly detection, no percentage change, no
 * severity. Every figure, every threshold and every verdict came from the server, and the
 * thresholds are displayed beside the judgements they produced so an operator can check the
 * arithmetic rather than trust it.
 *
 * ## There is no language model here, and the page does not imply one
 *
 * The insight explanations are deterministic templates filled from the numbers each insight
 * carries. The same data always produces the same sentence. The section is headed "findings"
 * and says as much, because calling this "AI insight" would claim something the system does
 * not do.
 *
 * ## Why the route carries no hotel segment
 *
 * The API path does (`/hotels/{id}/intelligence/...`) because that is where tenant isolation
 * is established. The *frontend* route is flat, like every other page in this application —
 * the hotel comes from the shared picker in the shell, so that switching property re-reads
 * every screen consistently instead of this one page owning a second mechanism.
 */
export function IntelligencePage() {
  const hotelContext = useHotelContext()
  const selected = hotelContext.selected

  const [observationDays, setObservationDays] = useState<number>(90)
  const [horizonDays, setHorizonDays] = useState<number>(DEFAULT_HORIZON_DAYS)
  const [trainingDays, setTrainingDays] = useState<number>(DEFAULT_TRAINING_DAYS)
  const [tab, setTab] = useState<TabId>('occupancy')
  /*
   * The measured-performance window, which is NOT the observation window above.
   *
   * `last90` rather than the shared default: a prediction is only scored once its target date
   * has cleared the 28-day settlement lag, so a seven-day window would measure nothing and the
   * panel would open on an empty result for every property.
   */
  const [performancePeriod, setPerformancePeriod] = useState<PeriodId>('last90')

  const observationId = useId()
  const horizonId = useId()
  const trainingId = useId()

  const intelligence = useIntelligence(
    selected?.public_id ?? null,
    selected?.timezone ?? 'UTC',
    { observationDays, horizonDays, trainingDays },
    // The revenue forecast is fetched only once its tab is opened: an operator who never
    // looks at it never pays for it, and it is a separate endpoint.
    { occupancy: tab === 'occupancy', revenue: tab === 'revenue' },
  )

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Intelligence is scoped to a property, and this account is not yet a member of one."
        />
      </Frame>
    )
  }

  if (hotelContext.status === 'error') {
    return (
      <Frame>
        <StateMessage
          icon={WifiOff}
          tone="alert"
          title="Could not load your hotels"
          detail="The list of properties you have access to could not be retrieved. Check your connection and try again."
          onRetry={hotelContext.retry}
        />
      </Frame>
    )
  }

  const occupancyPoints: ForecastChartPoint[] =
    intelligence.occupancy.data?.points.map((point) => ({
      date: point.date,
      // `Number()` at the plotting boundary only -- the same convention the dashboard's
      // chart uses. The label below keeps the server's own string.
      actual: point.on_the_books_room_nights,
      actualLabel: formatCount(point.on_the_books_room_nights),
      predicted: point.predicted_room_nights === null ? null : Number(point.predicted_room_nights),
      predictedLabel: point.predicted_room_nights ?? '',
      lower: point.interval_lower === null ? null : Number(point.interval_lower),
      upper: point.interval_upper === null ? null : Number(point.interval_upper),
      method: point.method,
    })) ?? []

  const confidenceOf = (value: string | null | undefined): string | null =>
    value === null || value === undefined ? null : `${value} confidence`

  return (
    <Frame>
      <form className={styles.controls} aria-label="Intelligence parameters">
        <div className={styles.control}>
          <label className={styles.controlLabel} htmlFor={observationId}>
            Observation window
          </label>
          <select
            id={observationId}
            className={styles.select}
            value={observationDays}
            onChange={(event) => {
              setObservationDays(Number(event.target.value))
            }}
          >
            {OBSERVATION_OPTIONS.map((days) => (
              <option key={days} value={days}>
                {`Last ${days} days`}
              </option>
            ))}
          </select>
          <span className={styles.hint}>
            The past span trend and anomaly detection look at, ending today.
          </span>
        </div>

        <div className={styles.control}>
          <label className={styles.controlLabel} htmlFor={horizonId}>
            Forecast horizon
          </label>
          <select
            id={horizonId}
            className={styles.select}
            value={horizonDays}
            onChange={(event) => {
              setHorizonDays(Number(event.target.value))
            }}
          >
            {HORIZON_OPTIONS.map((days) => (
              <option key={days} value={days}>
                {`Next ${days} days`}
              </option>
            ))}
          </select>
          <span className={styles.hint}>Starts tomorrow. The API allows at most 90 days.</span>
        </div>

        <div className={styles.control}>
          <label className={styles.controlLabel} htmlFor={trainingId}>
            Training history
          </label>
          <select
            id={trainingId}
            className={styles.select}
            value={trainingDays}
            onChange={(event) => {
              setTrainingDays(Number(event.target.value))
            }}
          >
            {TRAINING_OPTIONS.map((days) => (
              <option key={days} value={days}>
                {`${days} days`}
              </option>
            ))}
          </select>
          <span className={styles.hint}>
            History immediately before the horizon that the model may learn from. It always
            ends the day before the forecast starts, which is what stops a forecast seeing the
            period it predicts.
          </span>
        </div>
      </form>

      {/* --- forecast ------------------------------------------------------------------- */}

      <section className={styles.block} aria-labelledby="intelligence-forecast">
        <div className={styles.blockHeader}>
          <div>
            <h2 className={styles.blockTitle} id="intelligence-forecast">
              Forecast
            </h2>
            <p className={styles.blockNote}>
              {formatDate(intelligence.horizon.dateFrom)} &ndash;{' '}
              {formatDate(intelligence.horizon.dateTo)}. What is already on the books is shown
              beside what the model expects. The two are never combined: one is a confirmed
              reservation, the other an estimate.
            </p>
          </div>
        </div>

        <div className={styles.tabs} role="tablist" aria-label="Forecast metric">
          {TABS.map((option) => (
            <button
              key={option.id}
              type="button"
              role="tab"
              id={`tab-${option.id}`}
              aria-selected={tab === option.id}
              aria-controls={`panel-${option.id}`}
              className={`${styles.tab} ${tab === option.id ? styles.tabCurrent : ''}`}
              onClick={() => {
                setTab(option.id)
              }}
            >
              {option.label}
            </button>
          ))}
        </div>

        <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
          {tab === 'occupancy' ? (
            <Gate
              resource={intelligence.occupancy}
              label="Loading the occupancy forecast"
              onRetry={intelligence.reload}
            >
              {(forecast) => (
                <>
                  <ForecastChart
                    metric="Occupied room nights"
                    points={occupancyPoints}
                    formatTick={(value) => formatCount(Math.round(value))}
                    unit="room nights"
                    confidenceLabel={confidenceOf(forecast.points[0]?.confidence_level)}
                    emptyMessage="No forecast data available for the selected horizon."
                  />
                  <Provenance model={forecast.model} />
                  <p className={styles.training}>
                    Trained on {formatCount(forecast.training_window.days)} days of history,{' '}
                    {formatDate(forecast.training_window.date_from)} &ndash;{' '}
                    {formatDate(forecast.training_window.date_to)}, from{' '}
                    {formatCount(forecast.training_window.observations)} observations.
                  </p>
                </>
              )}
            </Gate>
          ) : (
            <Gate
              resource={intelligence.revenue}
              label="Loading the revenue forecast"
              onRetry={intelligence.reload}
            >
              {(forecast) =>
                forecast.currencies.length === 0 ? (
                  <StateMessage
                    icon={Radar}
                    tone="status"
                    title="No revenue history to forecast from"
                    detail="This property has no recorded room revenue in the training window, so no currency could be forecast. A currency the hotel has never traded in is absent rather than predicted at zero."
                  />
                ) : (
                  <>
                    {forecast.is_multi_currency ? (
                      <p className={styles.currencyNote} role="status">
                        This property trades in more than one currency. Each is forecast
                        independently and shown separately &mdash; they are never converted or
                        combined, and there is no exchange rate anywhere in this system.
                      </p>
                    ) : null}
                    {forecast.currencies.map((bucket) => (
                      <div key={bucket.currency} className={styles.currencyBlock}>
                        <h3 className={styles.currencyTitle}>{bucket.currency}</h3>
                        <ForecastChart
                          metric={`Room revenue (${bucket.currency})`}
                          points={bucket.points.map((point) => ({
                            date: point.date,
                            actual: Number(point.on_the_books_room_revenue),
                            actualLabel: formatMoney(
                              point.on_the_books_room_revenue,
                              bucket.currency,
                            ),
                            predicted:
                              point.predicted_room_revenue === null
                                ? null
                                : Number(point.predicted_room_revenue),
                            predictedLabel:
                              point.predicted_room_revenue === null
                                ? ''
                                : formatMoney(point.predicted_room_revenue, bucket.currency),
                            lower: point.interval_lower === null ? null : Number(point.interval_lower),
                            upper: point.interval_upper === null ? null : Number(point.interval_upper),
                            method: point.method,
                          }))}
                          formatTick={(value) => formatCount(Math.round(value))}
                          unit={bucket.currency}
                          confidenceLabel={confidenceOf(bucket.points[0]?.confidence_level)}
                          emptyMessage="No forecast data available for the selected horizon."
                        />
                      </div>
                    ))}
                    <Provenance model={forecast.model} />
                  </>
                )
              }
            </Gate>
          )}
        </div>
      </section>

      {/* --- demand trend --------------------------------------------------------------- */}

      <section className={styles.block} aria-labelledby="intelligence-trend">
        <h2 className={styles.blockTitle} id="intelligence-trend">
          Booking demand
        </h2>
        <p className={styles.blockNote}>
          {formatDate(intelligence.window.dateFrom)} &ndash;{' '}
          {formatDate(intelligence.window.dateTo)}. Counted by when a booking was taken, which
          is what a demand trend is about &mdash; stay dates answer a different question.
        </p>
        <Gate
          resource={intelligence.trend}
          label="Loading the demand trend"
          onRetry={intelligence.reload}
        >
          {(trend) => (
            <>
              <TrendSummary trend={trend} />
              <Provenance model={trend.model} />
            </>
          )}
        </Gate>
      </section>

      {/* --- anomalies ------------------------------------------------------------------ */}

      <section className={styles.block} aria-labelledby="intelligence-anomalies">
        <h2 className={styles.blockTitle} id="intelligence-anomalies">
          Anomalies
        </h2>
        <p className={styles.blockNote}>
          Days whose value sits further from the window median than the threshold allows,
          measured by modified z-score on the median absolute deviation.
        </p>
        <Gate
          resource={intelligence.anomalies}
          label="Loading anomalies"
          onRetry={intelligence.reload}
        >
          {(report) => (
            <>
              {report.anomalies.length === 0 ? (
                <StateMessage
                  icon={Radar}
                  tone="status"
                  title="Nothing unusual in this window"
                  detail={`${formatCount(report.metrics_scanned.length)} ${
                    report.metrics_scanned.length === 1 ? 'metric was' : 'metrics were'
                  } scanned — ${report.metrics_scanned.join(', ')} — and no day exceeded the threshold.`}
                />
              ) : (
                <>
                  <p className={styles.count} role="status">
                    {formatCount(report.anomalies.length)}{' '}
                    {report.anomalies.length === 1 ? 'day' : 'days'} flagged across{' '}
                    {formatCount(report.metrics_scanned.length)}{' '}
                    {report.metrics_scanned.length === 1 ? 'metric' : 'metrics'}:{' '}
                    {report.metrics_scanned.join(', ')}.
                  </p>
                  <AnomalyList anomalies={report.anomalies} />
                </>
              )}
              <Provenance model={report.model} />
            </>
          )}
        </Gate>
      </section>

      {/* --- insights ------------------------------------------------------------------- */}

      <section className={styles.block} aria-labelledby="intelligence-insights">
        <h2 className={styles.blockTitle} id="intelligence-insights">
          Findings
        </h2>
        <p className={styles.blockNote}>
          Assembled from the trend, anomaly and forecast results above. Each explanation is a
          fixed template filled from the numbers it carries &mdash; the same data always
          produces the same sentence. Nothing here is generated text.
        </p>
        <Gate
          resource={intelligence.insights}
          label="Loading findings"
          onRetry={intelligence.reload}
        >
          {(report) =>
            report.insights.length === 0 ? (
              <StateMessage
                icon={Lightbulb}
                tone="status"
                title="No findings for this window"
                detail="Nothing in the observed period met the criteria for a finding. This is a result, not a failure to analyse."
              />
            ) : (
              <>
                <InsightList insights={report.insights} />
                <Provenance model={report.model} />
              </>
            )
          }
        </Gate>
      </section>

      {/*
        Stage 7.4. The TRAINED demand model's measured performance -- a different subsystem
        from everything above, and placed last so the page reads statistical-first.

        The separation is deliberate and is the same one the API makes. The four sections above
        serve V1's seasonal day-of-week medians, computed per request from the analytics
        series. This one is about an artifact fitted offline, versioned and checksummed, whose
        production accuracy is NOT established. Sharing a chart or a figure between them would
        imply a lineage they do not have, so they share neither: this section fetches its own
        data, names the model version it is about, and states its own claim boundary.
      */}
      <section className={styles.block} aria-labelledby="intelligence-model-performance">
        <h2 className={styles.blockTitle} id="intelligence-model-performance">
          Trained model &mdash; measured performance
        </h2>
        <p className={styles.blockNote}>
          A separate subsystem from the statistical forecasts above: an offline-fitted model,
          not a per-request median. These figures measure predictions the property was actually
          served, under a protocol fixed and checksummed before any number was computed.{' '}
          <strong>They do not establish that the model is accurate.</strong>
        </p>
        <ForecastPerformanceSection
          hotel={selected}
          period={performancePeriod}
          onPeriodChange={setPerformancePeriod}
        />
      </section>
    </Frame>
  )
}

/**
 * Loading, failure and success for one resource, so one endpoint's failure does not blank
 * the page.
 *
 * Each of the five reads is independent, and an operator who can see three of them should
 * see three of them.
 */
function Gate<T>({
  resource,
  label,
  onRetry,
  children,
}: {
  resource: Resource<T>
  label: string
  onRetry: () => void
  children: (data: T) => ReactNode
}) {
  if (resource.error !== null) {
    const failure = describeFailure(resource.error, FAILURE_COPY)
    return (
      <StateMessage
        icon={resource.error.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
        tone="alert"
        title={failure.title}
        detail={failure.detail}
        {...(failure.canRetry ? { onRetry } : {})}
      />
    )
  }
  if (resource.status !== 'ready' || resource.data === null) {
    return (
      <div className={styles.skeleton} role="status" aria-busy="true">
        <Skeleton height="12rem" label={label} />
      </div>
    )
  }
  return <>{children(resource.data)}</>
}

/**
 * Where a number came from.
 *
 * Printed under every result rather than once on the page, because each response carries its
 * own metadata and a reader looking at one chart should not have to scroll to learn which
 * model drew it. The methodology sentence is the server's own.
 */
function Provenance({ model }: { model: ModelMetadata }) {
  return (
    <p className={styles.provenance}>
      <span className={styles.modelName}>
        {model.model_name} v{model.model_version}
      </span>
      {model.methodology}
    </p>
  )
}

/** The page heading, shared by every state so the frame never jumps. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Intelligence"
        description="Statistical forecasts, demand trend, anomalies and structured findings for this property. Every figure comes from the server; nothing on this page is calculated in your browser, and no text here is generated."
      />
      <div className={styles.page}>{children}</div>
    </PageContainer>
  )
}
