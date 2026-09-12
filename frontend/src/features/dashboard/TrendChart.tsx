import { useId } from 'react'

import styles from './TrendChart.module.css'

/** One plotted day. `value` is null where the backend reported the metric as undefined. */
export interface TrendPoint {
  /** `YYYY-MM-DD`, as the backend sent it. */
  readonly date: string
  readonly value: number | null
}

export interface TrendChartProps {
  /** Names the metric, e.g. `Occupancy`. Used in the chart's accessible description. */
  readonly metric: string
  readonly points: readonly TrendPoint[]
  /** Formats a value for an axis tick and for the data table. */
  readonly formatValue: (value: number) => string
  /** What the numbers are, e.g. `%` or `EUR`. Shown on the axis and stated to a reader. */
  readonly unit: string
  /** Message for a series with no plottable points. */
  readonly emptyMessage: string
}

/* The drawing area, in user units. The SVG scales to its container through the viewBox, so
 * these are a coordinate system rather than pixels -- the chart is resolution-independent
 * and reflows without JavaScript measuring anything. */
const WIDTH = 720
const HEIGHT = 220
const PAD_LEFT = 64
const PAD_RIGHT = 16
const PAD_TOP = 16
const PAD_BOTTOM = 32

const PLOT_WIDTH = WIDTH - PAD_LEFT - PAD_RIGHT
const PLOT_HEIGHT = HEIGHT - PAD_TOP - PAD_BOTTOM

/** Short day label for the x axis, e.g. `4 Sep`. */
function axisDate(iso: string): string {
  const [year, month, day] = iso.split('-').map(Number) as [number, number, number]
  return new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' }).format(
    new Date(Date.UTC(year, month - 1, day)),
  )
}

/**
 * A "nice" upper bound and a tick step for a domain.
 *
 * Axis ticks land on 1, 2 or 5 times a power of ten, which is what makes a scale readable at
 * a glance. Deriving the step from the data instead of fixing it is what lets one component
 * draw an occupancy percentage and a six-figure revenue without either looking wrong.
 */
function niceScale(min: number, max: number): { lower: number; upper: number; step: number } {
  // Zero is always in view. A revenue chart whose axis starts at 9,000 exaggerates every
  // wiggle into a cliff, which is the single most common way a truthful series is drawn as
  // a misleading picture.
  const lower = Math.min(0, min)
  const upper = Math.max(0, max)
  const span = upper - lower

  if (span === 0) {
    // A flat series at zero, or a single repeated value. Give it a domain rather than
    // dividing by nothing.
    return { lower, upper: upper === 0 ? 1 : upper * 1.2, step: upper === 0 ? 0.25 : upper * 0.3 }
  }

  const rough = span / 4
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const normalised = rough / magnitude
  const factor = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10
  const step = factor * magnitude

  return {
    lower: Math.floor(lower / step) * step,
    upper: Math.ceil(upper / step) * step,
    step,
  }
}

/**
 * A daily series, drawn as a line.
 *
 * ## Why there is no charting library
 *
 * The project had none, and this needs one line over a gap-free daily series with a zero
 * baseline. Recharts or Chart.js would add a dependency an order of magnitude larger than
 * the code below, and neither would produce the accessible fallback this brief requires
 * without extra work anyway. Hand-drawn SVG also keeps the design system honest: the chart
 * uses the same tokens as everything else rather than a library's palette, which is exactly
 * the "second design system" the stage is meant to avoid.
 *
 * ## What it refuses to do
 *
 * * **It never truncates the y axis.** Zero is always in the domain, so the height of the
 *   line is proportional to the value it represents.
 * * **It does not interpolate across missing days.** The backend guarantees a gap-free
 *   series; a day whose metric is genuinely undefined breaks the line rather than being
 *   drawn through, because a straight segment across a gap asserts data that does not exist.
 * * **A single point is a dot, not a line.** One measurement is not a trend, and a
 *   horizontal line through one value implies a second one.
 *
 * ## Accessibility
 *
 * The SVG carries `role="img"` and a description naming the metric, the range, the low, the
 * high and the latest value -- a summary, because reading ninety coordinates aloud is not
 * comprehension. Underneath it, every plotted value is available exactly, in a real table,
 * behind a disclosure that is keyboard-operable and useful to sighted users too. Colour
 * carries nothing: the line is one series, labelled, with its axis in the same view.
 */
export function TrendChart({ metric, points, formatValue, unit, emptyMessage }: TrendChartProps) {
  const titleId = useId()
  const descId = useId()

  const plottable = points.filter(
    (point): point is TrendPoint & { value: number } => point.value !== null,
  )

  if (points.length === 0 || plottable.length === 0) {
    return (
      <p className={styles.empty} role="status">
        {emptyMessage}
      </p>
    )
  }

  const values = plottable.map((point) => point.value)
  const { lower, upper, step } = niceScale(Math.min(...values), Math.max(...values))
  const domain = upper - lower

  // With one point there is no interval to divide, so it is placed in the middle rather
  // than at x = 0/0.
  const xFor = (index: number) =>
    points.length === 1
      ? PAD_LEFT + PLOT_WIDTH / 2
      : PAD_LEFT + (index / (points.length - 1)) * PLOT_WIDTH
  const yFor = (value: number) => PAD_TOP + PLOT_HEIGHT - ((value - lower) / domain) * PLOT_HEIGHT

  /* Segments, not one path: a break in the data must be a break in the line. */
  const segments: string[] = []
  let current: string[] = []
  points.forEach((point, index) => {
    if (point.value === null) {
      if (current.length > 0) {
        segments.push(current.join(' '))
        current = []
      }
      return
    }
    current.push(`${current.length === 0 ? 'M' : 'L'}${xFor(index)},${yFor(point.value)}`)
  })
  if (current.length > 0) {
    segments.push(current.join(' '))
  }

  const ticks: number[] = []
  for (let value = lower; value <= upper + step / 2; value += step) {
    ticks.push(value)
  }

  const latest = plottable[plottable.length - 1]!
  const lowest = plottable.reduce((a, b) => (b.value < a.value ? b : a))
  const highest = plottable.reduce((a, b) => (b.value > a.value ? b : a))

  const description =
    `${metric} by day, ${axisDate(points[0]!.date)} to ${axisDate(points[points.length - 1]!.date)}. ` +
    `Lowest ${formatValue(lowest.value)} on ${axisDate(lowest.date)}; ` +
    `highest ${formatValue(highest.value)} on ${axisDate(highest.date)}; ` +
    `latest ${formatValue(latest.value)} on ${axisDate(latest.date)}. ` +
    `Measured in ${unit}. Every value is listed in the table below the chart.`

  // Enough labels to read the axis, few enough not to overlap on a phone.
  const labelEvery = Math.max(1, Math.ceil(points.length / 6))

  return (
    <div className={styles.wrapper}>
      <svg
        className={styles.chart}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        /* The default (uniform) scaling, not `preserveAspectRatio="none"`: stretching the
         * viewBox to the container would distort every axis label and stroke. The CSS gives
         * it `width: 100%; height: auto`, so it scales uniformly with the column. */
        role="img"
        /*
         * The title NAMES the chart and the description EXPLAINS it -- two different
         * relationships, so two different attributes. Pointing `aria-labelledby` at both, as
         * this first did, concatenates them into one enormous accessible name and leaves the
         * chart with no description at all: a screen reader then reads the whole summary
         * every time focus passes the image, instead of on request.
         */
        aria-labelledby={titleId}
        aria-describedby={descId}
      >
        <title id={titleId}>{`${metric} trend`}</title>
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
              {formatValue(tick)}
            </text>
          </g>
        ))}

        {points.map((point, index) =>
          index % labelEvery === 0 || index === points.length - 1 ? (
            <text
              key={point.date}
              className={styles.axisLabel}
              x={xFor(index)}
              y={HEIGHT - 10}
              textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}
            >
              {axisDate(point.date)}
            </text>
          ) : null,
        )}

        {segments.map((segment) => (
          <path key={segment} className={styles.line} d={segment} />
        ))}

        {/* One measurement is a dot. Drawn for a lone point, and for any point marooned
         * between two undefined days, which a line cannot show. */}
        {points.map((point, index) => {
          if (point.value === null) {
            return null
          }
          const isolated =
            points.length === 1 ||
            ((points[index - 1]?.value ?? null) === null && (points[index + 1]?.value ?? null) === null)
          return isolated ? (
            <circle
              key={point.date}
              className={styles.point}
              cx={xFor(index)}
              cy={yFor(point.value)}
              r={4}
            />
          ) : null
        })}
      </svg>

      <details className={styles.details}>
        <summary className={styles.summary}>{`Show ${metric.toLowerCase()} values`}</summary>
        <div className={styles.tableScroll}>
          <table className={styles.table}>
            <caption className={styles.caption}>{`${metric} by day, in ${unit}`}</caption>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">{metric}</th>
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.date}>
                  <th scope="row">{axisDate(point.date)}</th>
                  <td>{point.value === null ? 'Not available' : formatValue(point.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}
