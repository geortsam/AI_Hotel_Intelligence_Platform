import type { AnalyticsRange } from '@/services/analytics/analyticsService'

/**
 * Turning "last 7 days" into the exact range the backend expects.
 *
 * ## The rule this file exists to enforce
 *
 * **Analytics dates are hotel-local calendar dates, and the browser's clock is irrelevant
 * to them.** Every date column the backend groups by -- `stay_date`, `check_in_date`,
 * `revenue_date`, `expense_date`, `review_date` -- is a `DATE`, not a timestamp. A night of
 * 2026-09-10 is that hotel's 10th of September. It does not shift because the person
 * looking at the report is in London, and it must not shift because their laptop is set to
 * UTC-8.
 *
 * So "today" is resolved in the hotel's own IANA zone, taken from `hotels.timezone`. A
 * receptionist in Piraeus and a regional manager in London ask for the same range and get
 * the same numbers. Using `new Date()` directly -- the obvious implementation -- would give
 * them different dashboards for the same hotel, and the difference would be invisible: both
 * would look plausible.
 *
 * ## Both bounds are inclusive
 *
 * The backend says so explicitly and the day count is `date_to - date_from + 1`. "Last 7
 * days" is therefore `today - 6 .. today`, not `today - 7 .. today`; the latter is eight
 * days and would quietly inflate every total by one day's trading.
 */

/** The periods the dashboard offers. Each maps to an exact, inclusive backend range. */
export type PeriodId = 'today' | 'last7' | 'last30' | 'last90'

export interface PeriodOption {
  readonly id: PeriodId
  readonly label: string
  /** Days in the window, inclusive of both ends. */
  readonly days: number
}

/**
 * Only periods that can be represented exactly.
 *
 * Every one is a fixed number of whole days ending on the hotel's today, which is precisely
 * what `date_from`/`date_to` express. Deliberately absent: "this month" and "month to date",
 * which are representable but whose *comparison* window is not a fixed length -- comparing a
 * 30-day month against a 31-day one is the kind of chart that lies quietly. Also absent:
 * anything over `MAX_RANGE_DAYS` (366), which the API rejects.
 */
export const PERIOD_OPTIONS: readonly PeriodOption[] = [
  { id: 'today', label: 'Today', days: 1 },
  { id: 'last7', label: 'Last 7 days', days: 7 },
  { id: 'last30', label: 'Last 30 days', days: 30 },
  { id: 'last90', label: 'Last 90 days', days: 90 },
]

export const DEFAULT_PERIOD: PeriodId = 'last7'

export function periodOption(id: PeriodId): PeriodOption {
  // The union type makes a miss impossible; the fallback exists so this returns a value
  // rather than `undefined` under `noUncheckedIndexedAccess`-style scrutiny.
  return PERIOD_OPTIONS.find((option) => option.id === id) ?? PERIOD_OPTIONS[1]!
}

/**
 * Today's date in a given IANA time zone, as `YYYY-MM-DD`.
 *
 * `en-CA` is used because its short date format *is* ISO 8601 -- no parsing of localised
 * month names, no ambiguity between `03/04` and `04/03`. An unknown zone makes
 * `Intl.DateTimeFormat` throw, so a hotel configured with a bad zone falls back to UTC
 * rather than taking the dashboard down; the figures are then a day out at most, and only
 * for a hotel whose stored configuration is already wrong.
 */
export function todayInZone(timeZone: string, now: Date = new Date()): string {
  try {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(now)
  } catch {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: 'UTC',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(now)
  }
}

/**
 * Shift a `YYYY-MM-DD` by whole days.
 *
 * The date is parsed as a UTC instant purely so the arithmetic has no local offset to trip
 * over -- there is no hour in a calendar date, and midday-UTC anchoring would be a
 * workaround for a problem that does not exist here. `Date.UTC` handles month and year
 * boundaries and leap days, which hand-rolled arithmetic gets wrong exactly once a year.
 */
export function shiftDate(isoDate: string, days: number): string {
  const [year, month, day] = isoDate.split('-').map(Number) as [number, number, number]
  const shifted = new Date(Date.UTC(year, month - 1, day + days))
  return shifted.toISOString().slice(0, 10)
}

/** The inclusive range for a period, ending on the hotel's today. */
export function rangeFor(period: PeriodId, timeZone: string, now?: Date): AnalyticsRange {
  const dateTo = todayInZone(timeZone, now)
  const { days } = periodOption(period)
  // days - 1, because both bounds count. See the file docstring.
  return { dateFrom: shiftDate(dateTo, -(days - 1)), dateTo }
}

/**
 * The window immediately before `range`, of identical length.
 *
 * This is the entire basis of every "vs previous period" figure the dashboard shows, so the
 * derivation is stated rather than implied:
 *
 *     previous.date_to   = range.date_from - 1 day
 *     previous.date_from = previous.date_to - (days - 1)
 *
 * The two windows are the same number of days, adjacent, and non-overlapping. Both are
 * fetched from the same endpoint with the same semantics, so a comparison is between two
 * figures the backend computed the same way -- not between a backend number and one this
 * application worked out for itself.
 *
 * Equal length is what makes the comparison honest. Comparing seven days against a
 * thirty-one-day month would produce a "change" that is mostly an artefact of the window.
 */
export function previousRange(range: AnalyticsRange, days: number): AnalyticsRange {
  const dateTo = shiftDate(range.dateFrom, -1)
  return { dateFrom: shiftDate(dateTo, -(days - 1)), dateTo }
}

/**
 * How a range reads in the interface, e.g. `4 – 10 Sep 2026`.
 *
 * A single-day range collapses to one date rather than repeating it. Formatted in `en-GB`
 * for an unambiguous day-month order; the underlying value is never reformatted for the
 * wire, which always uses the ISO strings above.
 */
export function describeRange({ dateFrom, dateTo }: AnalyticsRange): string {
  const format = (iso: string, withYear: boolean) => {
    const [year, month, day] = iso.split('-').map(Number) as [number, number, number]
    return new Intl.DateTimeFormat('en-GB', {
      day: 'numeric',
      month: 'short',
      ...(withYear ? { year: 'numeric' } : {}),
      timeZone: 'UTC',
    }).format(new Date(Date.UTC(year, month - 1, day)))
  }

  if (dateFrom === dateTo) {
    return format(dateTo, true)
  }
  const sameYear = dateFrom.slice(0, 4) === dateTo.slice(0, 4)
  return `${format(dateFrom, !sameYear)} – ${format(dateTo, true)}`
}
