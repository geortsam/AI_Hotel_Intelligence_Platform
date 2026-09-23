import type { ForecastChartPoint } from '@/features/intelligence/ForecastChart'
import { formatCount } from '@/lib/format'
import type { DailyMetricsRow } from '@/types/analytics'
import type { StoredDemandPrediction } from '@/types/mlPerformance'

/**
 * Putting what happened beside what was predicted, for one hotel, day by day.
 *
 * ## Why this is a join and not a calculation
 *
 * Everything below is lookup and alignment. **No metric is computed here** — no error, no
 * difference, no total, no average, no percentage. The error figures on this screen come from
 * `/ml/forecast-accuracy`, which computes them under a checksummed protocol; if this module
 * subtracted one series from the other it would be a second, unversioned definition of
 * accuracy sitting in a browser, disagreeing with the first the moment either changed.
 *
 * The two values are carried side by side and drawn as two marks. They are never summed,
 * averaged, differenced or blended.
 *
 * ## Where the two series come from, and why not from one endpoint
 *
 * There is no endpoint that returns them paired. Stage 6.9 decided against it in as many
 * words — "shipping the paired series would make it an export instead" — and Stage 7.3
 * inherited that, so `/ml/forecast-accuracy` returns aggregate error with no dates in it at
 * all. The two halves are therefore read from where they already live:
 *
 * | Series | Source | Field |
 * |---|---|---|
 * | actual | `/analytics/daily` | `occupied_room_nights` |
 * | predicted | `/ml/demand-predictions` | `predicted_room_nights` |
 *
 * `occupied_room_nights` is the same definition the accuracy protocol scores against — the
 * service reads it through `MlDemandRepository.demand_by_date`, described there as "the
 * EXISTING definition". So the actual line on this chart is the same ground truth the measured
 * error was computed from, not a second opinion about what occupancy means.
 *
 * ## A date with two predictions is left undrawn, deliberately
 *
 * `uq_demand_predictions_identity` includes `feature_digest`, so one `target_date` can carry
 * several stored rows: a booking recorded late moves `demand_lag_7`, and the new prediction is
 * written *beside* the old one rather than instead of it. Choosing between them is a rule —
 * the accuracy protocol's rule is "earliest `generated_at`, lowest `id`", executed by the
 * database in one `DISTINCT ON`.
 *
 * This module does not reimplement that rule. Where a date carries exactly one prediction it
 * is plotted; where it carries more than one, the predicted value is `null`, which
 * `ForecastChart` renders as a gap in the line and "Not shown" in its table. Picking one here
 * would be reconstructing a server-side selection in the browser, and picking silently would
 * be worse: the chart would assert a single prediction existed when two did. The count of such
 * days is returned so the screen can say so out loud.
 */

export interface PairedSeries {
  readonly points: readonly ForecastChartPoint[]
  /** Days whose predicted value was withheld because the history holds more than one. */
  readonly ambiguousDays: number
  /** Days in the window that carry an actual but no stored prediction at all. */
  readonly unpredictedDays: number
}

/** Indexes the stored rows by target date, counting rather than choosing. */
function byTargetDate(
  predictions: readonly StoredDemandPrediction[],
): Map<string, readonly StoredDemandPrediction[]> {
  const index = new Map<string, StoredDemandPrediction[]>()
  for (const prediction of predictions) {
    const existing = index.get(prediction.target_date)
    if (existing) {
      existing.push(prediction)
      continue
    }
    index.set(prediction.target_date, [prediction])
  }
  return index
}

/**
 * One chart point per day of the analytics window, in the order the server sent the days.
 *
 * The actual series drives the x-axis: it is the record of what happened, it is dense, and
 * every day in the window has one. A prediction with no corresponding day would be a
 * prediction for a date outside the window, which is not this chart's subject.
 */
export function pairActualsWithPredictions(
  days: readonly DailyMetricsRow[],
  predictions: readonly StoredDemandPrediction[],
): PairedSeries {
  const index = byTargetDate(predictions)
  const storedFor = (date: string): readonly StoredDemandPrediction[] => index.get(date) ?? []

  /* Counted by filtering rather than by accumulating. These are disclosure counts -- how many
     days this screen declined to draw, and why -- and they are derived from the data in one
     pass each, with no running total to drift out of step with the points below. */
  const unpredictedDays = days.filter((day) => storedFor(day.date).length === 0).length
  const ambiguousDays = days.filter((day) => storedFor(day.date).length > 1).length

  const points = days.map((day): ForecastChartPoint => {
    const stored = storedFor(day.date)
    const only = stored.length === 1 ? stored[0]! : null

    return {
      date: day.date,
      /* Already a JSON number from the API, and a count of room nights rather than money.
         Passed through untouched — no rounding, no scaling, no conversion. */
      actual: day.occupied_room_nights,
      actualLabel: formatCount(day.occupied_room_nights),
      predicted: only === null ? null : only.predicted_room_nights,
      predictedLabel: only === null ? '' : formatCount(only.predicted_room_nights),
      /* No prediction interval exists. The served model is a point forecaster and the Stage
         7.3 protocol produces no bounds, so drawing a band would be inventing one. */
      lower: null,
      upper: null,
      method:
        only === null
          ? stored.length === 0
            ? 'No stored prediction'
            : `${stored.length} stored predictions`
          : only.model_version,
    }
  })

  return { points, ambiguousDays, unpredictedDays }
}
