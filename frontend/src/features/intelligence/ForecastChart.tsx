import { useId } from 'react'

import { formatDayAndMonth } from '@/lib/format'

import styles from './ForecastChart.module.css'

/**
 * One forecast day, already reduced to what a chart can draw.
 *
 * The strings are kept alongside the numbers deliberately: the table prints the string the
 * server sent, at the scale it sent it, while the plot uses the number. Nothing re-formats a
 * decimal by going through a float.
 */
export interface ForecastChartPoint {
  readonly date: string
  /** **Actual.** Already on the books for this day. */
  readonly actual: number
  readonly actualLabel: string
  /** **Predicted.** Null where the model had no usable history. */
  readonly predicted: number | null
  readonly predictedLabel: string
  readonly lower: number | null
  readonly upper: number | null
  /** Which model produced this point, for the table. */
  readonly method: string
}

export interface ForecastChartProps {
  /** Names the series, e.g. `Occupied room nights`. Used in the accessible description. */
  readonly metric: string
  readonly points: readonly ForecastChartPoint[]
  /** Formats a value for an axis tick. Never used for a table cell — those print the server's string. */
  readonly formatTick: (value: number) => string
  /** What the numbers are, e.g. `room nights` or `EUR`. */
  readonly unit: string
  /** The stated confidence, e.g. `95%`, or null when the response carried none. */
  readonly confidenceLabel: string | null
  readonly emptyMessage: string
}

/* The drawing area in user units, matching the dashboard's chart so the two read as one
 * system. The SVG scales through its viewBox, so these are a coordinate system, not pixels. */
const WIDTH = 720
const HEIGHT = 240
const PAD_LEFT = 64
const PAD_RIGHT = 16
const PAD_TOP = 16
const PAD_BOTTOM = 44

const PLOT_WIDTH = WIDTH - PAD_LEFT - PAD_RIGHT
const PLOT_HEIGHT = HEIGHT - PAD_TOP - PAD_BOTTOM

/** A "nice" domain and tick step, so an axis lands on 1, 2 or 5 times a power of ten. */
function niceScale(max: number): { upper: number; step: number } {
  // Zero is always in view. An axis that starts above zero exaggerates every wiggle into a
  // cliff, which is the commonest way a truthful series is drawn as a misleading picture.
  if (max <= 0) {
    return { upper: 1, step: 0.25 }
  }
  const rough = max / 4
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const normalised = rough / magnitude
  const factor = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10
  const step = factor * magnitude
  return { upper: Math.ceil(max / step) * step, step }
}

/**
 * A forecast: what is already booked, what the model expects, and how wide its interval is.
 *
 * ## Why this is not the dashboard's `TrendChart`
 *
 * That component draws **one** series of `value | null` over past days. A forecast is three
 * things at once — an actual, a prediction and an interval band — and the whole point of the
 * contract is that the first two are **never blended**. Passing a forecast through a
 * single-series chart would require choosing one of them to show, or averaging them, and
 * averaging a confirmed reservation with a guess is precisely the mistake the API's shape
 * exists to prevent. `TrendChart` is also a locked Stage 5.4 component; widening it to carry
 * a band would reopen that stage for a need it was never built for.
 *
 * The conventions are deliberately identical: same viewBox geometry, same design tokens,
 * same zero baseline, same `role="img"` with a separate title and description, same
 * disclosure-plus-table fallback.
 *
 * ## What it refuses to do
 *
 * * **It never blends the two series.** They are drawn as two distinct marks, named in the
 *   legend, and listed in separate table columns.
 * * **It never invents a prediction.** A day whose `predicted` is null is a gap in the
 *   predicted line and reads "No prediction" in the table — it is not drawn at zero, and the
 *   line is not interpolated across it, because a segment across a gap asserts data that
 *   does not exist.
 * * **It never invents an interval.** The band is drawn only where the server supplied both
 *   bounds.
 * * **It never derives a figure.** `Number()` appears only where a value becomes a
 *   coordinate — the same boundary the dashboard's chart uses — and no total, average or
 *   percentage change is computed anywhere.
 *
 * ## Accessibility
 *
 * Colour carries nothing on its own: the actual series is a solid line with round markers,
 * the prediction a dashed line with diamond markers, and both are named in a legend that
 * repeats the shape. The description states the span, the confidence and where predictions
 * are missing. Every value is available exactly, in a real table, behind a keyboard-operable
 * disclosure.
 */
export function ForecastChart({
  metric,
  points,
  formatTick,
  unit,
  confidenceLabel,
  emptyMessage,
}: ForecastChartProps) {
  const titleId = useId()
  const descId = useId()

  if (points.length === 0) {
    return (
      <p className={styles.empty} role="status">
        {emptyMessage}
      </p>
    )
  }

  const candidates = points.flatMap((point) =>
    [point.actual, point.predicted, point.upper].filter(
      (value): value is number => value !== null && Number.isFinite(value),
    ),
  )
  const { upper, step } = niceScale(candidates.length === 0 ? 0 : Math.max(...candidates))

  const xFor = (index: number) =>
    points.length === 1
      ? PAD_LEFT + PLOT_WIDTH / 2
      : PAD_LEFT + (index / (points.length - 1)) * PLOT_WIDTH
  const yFor = (value: number) => PAD_TOP + PLOT_HEIGHT - (value / upper) * PLOT_HEIGHT

  /** Segments rather than one path, so a gap in the data is a gap in the line. */
  const pathOf = (pick: (point: ForecastChartPoint) => number | null): string[] => {
    const segments: string[] = []
    let current: string[] = []
    points.forEach((point, index) => {
      const value = pick(point)
      if (value === null) {
        if (current.length > 0) {
          segments.push(current.join(' '))
          current = []
        }
        return
      }
      current.push(`${current.length === 0 ? 'M' : 'L'}${xFor(index)},${yFor(value)}`)
    })
    if (current.length > 0) {
      segments.push(current.join(' '))
    }
    return segments
  }

  const actualSegments = pathOf((point) => point.actual)
  const predictedSegments = pathOf((point) => point.predicted)

  /* The interval band: one polygon per run of consecutive days that carry both bounds. */
  const bands: string[] = []
  let run: ForecastChartPoint[] = []
  let runStart = 0
  const flush = () => {
    if (run.length < 2) {
      run = []
      return
    }
    const top = run.map((point, offset) => `${xFor(runStart + offset)},${yFor(point.upper!)}`)
    const bottom = run
      .map((point, offset) => `${xFor(runStart + offset)},${yFor(point.lower!)}`)
      .reverse()
    bands.push([...top, ...bottom].join(' '))
    run = []
  }
  points.forEach((point, index) => {
    if (point.lower === null || point.upper === null) {
      flush()
      return
    }
    if (run.length === 0) {
      runStart = index
    }
    run.push(point)
  })
  flush()

  const ticks: number[] = []
  for (let value = 0; value <= upper + step / 2; value += step) {
    ticks.push(value)
  }

  const missing = points.filter((point) => point.predicted === null).length
  const description =
    `${metric} by day, ${formatDayAndMonth(points[0]!.date)} to ` +
    `${formatDayAndMonth(points[points.length - 1]!.date)}, measured in ${unit}. ` +
    `Two series: room nights already on the books, and the model's prediction — they are ` +
    `shown separately and are never combined. ` +
    (confidenceLabel === null
      ? 'The response carried no prediction interval. '
      : `The shaded band is the ${confidenceLabel} prediction interval. `) +
    (missing === 0
      ? ''
      : `${missing} of ${points.length} days have no prediction, because the model had too little history. `) +
    'Every value is listed in the table below the chart.'

  const labelEvery = Math.max(1, Math.ceil(points.length / 6))

  return (
    <div className={styles.wrapper}>
      <svg
        className={styles.chart}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-labelledby={titleId}
        aria-describedby={descId}
      >
        <title id={titleId}>{`${metric} forecast`}</title>
        <desc id={descId}>{description}</desc>

        {ticks.map((tick) => (
          <g key={tick}>
            <line
              className={tick === 0 ? styles.baseline : styles.gridline}
              x1={PAD_LEFT}
              x2={WIDTH - PAD_RIGHT}
              y1={yFor(tick)}
              y2={yFor(tick)}
            />
            <text className={styles.axisLabel} x={PAD_LEFT - 8} y={yFor(tick) + 4} textAnchor="end">
              {formatTick(tick)}
            </text>
          </g>
        ))}

        {bands.map((band) => (
          <polygon key={band} className={styles.band} points={band} />
        ))}

        {points.map((point, index) =>
          index % labelEvery === 0 || index === points.length - 1 ? (
            <text
              key={point.date}
              className={styles.axisLabel}
              x={xFor(index)}
              y={HEIGHT - 22}
              textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}
            >
              {formatDayAndMonth(point.date)}
            </text>
          ) : null,
        )}

        {predictedSegments.map((segment) => (
          <path key={`p${segment}`} className={styles.predictedLine} d={segment} />
        ))}
        {actualSegments.map((segment) => (
          <path key={`a${segment}`} className={styles.actualLine} d={segment} />
        ))}

        {/* Markers, so the two series are told apart by shape and not only by colour. */}
        {points.map((point, index) => (
          <circle
            key={`ca${point.date}`}
            className={styles.actualPoint}
            cx={xFor(index)}
            cy={yFor(point.actual)}
            r={3}
          />
        ))}
        {points.map((point, index) =>
          point.predicted === null ? null : (
            <rect
              key={`cp${point.date}`}
              className={styles.predictedPoint}
              x={xFor(index) - 3}
              y={yFor(point.predicted) - 3}
              width={6}
              height={6}
              transform={`rotate(45 ${xFor(index)} ${yFor(point.predicted)})`}
            />
          ),
        )}
      </svg>

      <p className={styles.legend}>
        <span className={styles.legendItem}>
          <svg className={styles.swatch} viewBox="0 0 24 12" aria-hidden="true">
            <line className={styles.actualLine} x1="1" y1="6" x2="23" y2="6" />
            <circle className={styles.actualPoint} cx="12" cy="6" r="3" />
          </svg>
          On the books &mdash; actual
        </span>
        <span className={styles.legendItem}>
          <svg className={styles.swatch} viewBox="0 0 24 12" aria-hidden="true">
            <line className={styles.predictedLine} x1="1" y1="6" x2="23" y2="6" />
            <rect
              className={styles.predictedPoint}
              x="9"
              y="3"
              width="6"
              height="6"
              transform="rotate(45 12 6)"
            />
          </svg>
          Predicted by the model
        </span>
        {confidenceLabel === null ? null : (
          <span className={styles.legendItem}>
            <svg className={styles.swatch} viewBox="0 0 24 12" aria-hidden="true">
              <rect className={styles.band} x="1" y="2" width="22" height="8" />
            </svg>
            {confidenceLabel} prediction interval
          </span>
        )}
      </p>

      <details className={styles.details}>
        <summary className={styles.summary}>{`Show ${metric.toLowerCase()} values`}</summary>
        <div className={styles.tableScroll}>
          <table className={styles.table}>
            <caption className={styles.caption}>
              {`${metric} by day, in ${unit}. On-the-books figures are actual; predicted figures are estimates.`}
            </caption>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">On the books</th>
                <th scope="col">Predicted</th>
                <th scope="col">Method</th>
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.date}>
                  <th scope="row">{formatDayAndMonth(point.date)}</th>
                  {/* The server's own string, at the scale it sent. */}
                  <td>{point.actualLabel}</td>
                  <td>
                    {point.predicted === null ? (
                      <span className={styles.absent}>No prediction</span>
                    ) : (
                      point.predictedLabel
                    )}
                  </td>
                  <td className={styles.method}>{point.method}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}
