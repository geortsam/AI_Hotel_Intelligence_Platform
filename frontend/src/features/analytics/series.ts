import type { TrendPoint } from '@/features/dashboard/TrendChart'
import type { DailyMetricsRow } from '@/types/analytics'

/**
 * Turning the server's daily rows into plottable series.
 *
 * ## The one numeric conversion in this feature, and why it is allowed
 *
 * `occupancyPoints` calls `Number()` on `occupancy_rate`. That is the only such call in the
 * analytics feature and it is deliberately confined here so a test can say so.
 *
 * It is permitted because **`occupancy_rate` is a ratio, not money**. Every rule in this
 * codebase about never converting a server decimal exists to keep a 64-bit binary float out of
 * a `NUMERIC(14,2)` money value, where `0.1 + 0.2` is not `0.3` and a cent goes missing. A
 * chart axis has no such property: it is a pixel position, the value is read once, multiplied
 * by one hundred for a percentage scale, and never written back, compared against a money
 * figure, or shown as a currency amount. The displayed KPI percentage does **not** come from
 * here — it comes from `formatRatioAsPercent`, which formats the server's string directly.
 *
 * No money field is passed through this module. The revenue and expense figures on this screen
 * are rendered as the strings the server sent, by `BreakdownTable`, which performs no
 * arithmetic at all.
 *
 * `null` is preserved rather than coerced to zero. The backend reports an undefined occupancy
 * rate as an explicit `null` — a day with no available room nights — and `TrendChart` draws a
 * gap for it. Plotting zero would invent a bad day the property never had.
 */

/** Occupancy as a percentage, for a 0–100 axis. `null` days stay null. */
export function occupancyPoints(days: readonly DailyMetricsRow[]): TrendPoint[] {
  return days.map((day) => ({
    date: day.date,
    value: day.occupancy_rate === null ? null : Number(day.occupancy_rate) * 100,
  }))
}

/**
 * A whole-number count series, straight from the server.
 *
 * No conversion: these fields arrive as JSON integers because they are counts of rows, not
 * money. The value is copied, not computed.
 */
export function countPoints(
  days: readonly DailyMetricsRow[],
  field: 'arrivals' | 'departures' | 'bookings_created' | 'cancellations',
): TrendPoint[] {
  return days.map((day) => ({ date: day.date, value: day[field] }))
}
