/**
 * Analytics-specific formatting: ratios, ratings, currency buckets and period deltas.
 *
 * The generic money and count primitives moved to `@/lib/format` when Stage 5.5 needed the
 * same rules for bookings; they are re-exported below so every existing importer of this
 * module keeps working unchanged. What stays here is what only analytics means:
 * `occupancy_rate` is a fraction, a rating is normalised to 0..1, money arrives in
 * per-currency buckets, and an absent bucket means "undefined" rather than "zero".
 *
 * ## What is not computed here
 *
 * Occupancy, ADR and RevPAR. All three are defined by the backend -- transcribed from
 * `daily_hotel_metrics`'s own generated columns -- and arrive ready. This module scales a
 * fraction to a percentage for display and adds a `%`; it does not divide anything by
 * anything. A second definition of ADR living in a React application is exactly the drift
 * that makes a dashboard disagree with the report it is supposed to summarise.
 */

import { formatCount, formatMoney, UNAVAILABLE } from '@/lib/format'
import type { DecimalString, MoneyByCurrency } from '@/types/analytics'

export { formatCount, formatMoney, UNAVAILABLE }

/**
 * A large money figure, shortened for a KPI tile.
 *
 * Only above a million, and only to three significant figures, so `EUR 1.24M` never stands
 * in for something a reader would expect to see in full. Below the threshold the exact
 * amount is returned unchanged -- an operations figure of `EUR 9,750.00` should be legible
 * to the cent, and rounding it would be shortening for its own sake.
 */
export function formatMoneyCompact(
  amount: DecimalString,
  currency: string,
  locale = 'en-GB',
): string {
  const value = Number(amount)
  if (!Number.isFinite(value) || Math.abs(value) < 1_000_000) {
    return formatMoney(amount, currency, locale)
  }
  try {
    const compact = new Intl.NumberFormat(locale, {
      notation: 'compact',
      maximumSignificantDigits: 3,
    }).format(value)
    return `${currency} ${compact}`
  } catch {
    return formatMoney(amount, currency, locale)
  }
}

/**
 * A backend ratio in [0, 1] as a percentage.
 *
 * The scaling is presentation, not arithmetic on a metric: `occupancy_rate` is
 * `NUMERIC(5,4)` and `"0.5804"` means 58.04%. One decimal place, because the fourth decimal
 * of a ratio is noise at the scale a hotel operates on.
 */
export function formatRatioAsPercent(ratio: DecimalString | null, locale = 'en-GB'): string {
  if (ratio === null) {
    return UNAVAILABLE
  }
  const value = Number(ratio)
  if (!Number.isFinite(value)) {
    return UNAVAILABLE
  }
  return `${new Intl.NumberFormat(locale, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(value * 100)}%`
}

/**
 * A normalized rating (0..1) on the five-point scale people actually use.
 *
 * `average_rating_normalized` is `rating / rating_scale`, which is the only average
 * comparable across sources that score out of five and out of ten. Multiplying by five
 * restores the familiar scale without picking a winner between the two source scales.
 */
export function formatRating(normalized: DecimalString | null, locale = 'en-GB'): string {
  if (normalized === null) {
    return UNAVAILABLE
  }
  const value = Number(normalized)
  if (!Number.isFinite(value)) {
    return UNAVAILABLE
  }
  return `${new Intl.NumberFormat(locale, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(value * 5)} / 5`
}

/**
 * The bucket for one currency, or null when the metric has none.
 *
 * An **empty array is the backend saying "undefined", not "zero"**, and the difference is
 * the whole reason this returns null rather than a synthetic zero bucket. A range with no
 * occupied nights comes back with `room_revenue: []`; showing `EUR 0.00` there would assert
 * that the hotel earned nothing, when what the API said is that there is nothing to divide.
 */
export function bucketFor<T extends { readonly currency: string }>(
  buckets: readonly T[],
  currency: string,
): T | null {
  return buckets.find((bucket) => bucket.currency === currency) ?? null
}

/**
 * Every currency appearing across the metrics given, in the backend's own order.
 *
 * The backend sorts each bucket list by currency code so responses are deterministic; this
 * preserves that rather than re-sorting, so what the UI lists is the order the API chose.
 */
export function currenciesIn(
  // Not generic: a type parameter would be inferred once across every argument, so mixing
  // `RoomRevenueByCurrency[]` with `MoneyByCurrency[]` -- which is the entire point of this
  // function -- would not compile. The structural minimum is all it needs.
  ...groups: readonly (readonly { readonly currency: string }[])[]
): string[] {
  const seen: string[] = []
  for (const group of groups) {
    for (const { currency } of group) {
      if (!seen.includes(currency)) {
        seen.push(currency)
      }
    }
  }
  return seen
}

/** A period-over-period change, as a proportion. `null` when it cannot be stated honestly. */
export interface Delta {
  /** (current - previous) / |previous|. Positive means the figure rose. */
  readonly change: number
  /** The direction, so nothing depends on reading the sign of a number or on colour. */
  readonly direction: 'up' | 'down' | 'flat'
}

/**
 * Compare two figures the backend produced for two adjacent, equal-length windows.
 *
 * **This is the only derived number in the dashboard, and it is derived from two
 * authoritative values rather than recomputed from parts.** Both operands come from the same
 * endpoint with the same metric definition; the windows are the same length and adjacent
 * (see `previousRange`). Nothing is inferred about what happened inside either window.
 *
 * It returns null -- and the UI then shows no comparison at all -- when:
 *
 * * either side is `null`, i.e. the backend called the metric undefined for that window.
 *   An undefined ADR is not zero, so a change against it would be fabricated;
 * * the previous value is zero, which makes the proportional change undefined rather than
 *   infinite. "Up 100%" from a base of nothing is a statement about dividing by zero, not
 *   about the hotel.
 *
 * A change below 0.05% reports as `flat` rather than as a rounding artefact with a direction.
 */
export function computeDelta(
  current: DecimalString | null,
  previous: DecimalString | null,
): Delta | null {
  if (current === null || previous === null) {
    return null
  }
  const now = Number(current)
  const before = Number(previous)
  if (!Number.isFinite(now) || !Number.isFinite(before) || before === 0) {
    return null
  }
  const change = (now - before) / Math.abs(before)
  if (Math.abs(change) < 0.0005) {
    return { change, direction: 'flat' }
  }
  return { change, direction: change > 0 ? 'up' : 'down' }
}

/** A delta as text, always signed, e.g. `+12.4%`. */
export function formatDelta(delta: Delta, locale = 'en-GB'): string {
  if (delta.direction === 'flat') {
    return 'no change'
  }
  const percent = new Intl.NumberFormat(locale, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
    signDisplay: 'always',
  }).format(delta.change * 100)
  return `${percent}%`
}

/** The amount for one currency out of a money bucket list, formatted, or the dash. */
export function formatBucket(
  buckets: readonly MoneyByCurrency[],
  currency: string,
  locale = 'en-GB',
): string {
  const bucket = bucketFor(buckets, currency)
  return bucket === null ? UNAVAILABLE : formatMoney(bucket.amount, currency, locale)
}
