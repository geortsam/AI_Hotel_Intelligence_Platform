/**
 * Formatting primitives shared by every feature.
 *
 * These were written for the dashboard in Stage 5.4 and lived inside it. Bookings needs the
 * same money rules -- same currency-code display, same refusal to do arithmetic on a
 * `Decimal` -- so they moved here rather than being written a second time. A second
 * `formatMoney` is how one screen starts showing `€1,240` while another shows `EUR 1240.00`
 * for the same column, and how a rounding rule ends up applied in one place and not the
 * other. `features/dashboard/format.ts` re-exports these, so nothing that already imported
 * them had to change.
 *
 * ## The rule these exist to keep
 *
 * **A backend `Decimal` arrives as a JSON string and stays one.** Money is `NUMERIC(14,2)`
 * in PostgreSQL, and the whole point of that column type is that it is not a binary float.
 * Nothing here stores a parsed number or returns one: a value is parsed at the last possible
 * moment, handed to `Intl.NumberFormat`, and discarded.
 *
 * The parse is safe for the range in play, which is worth stating rather than assuming:
 * `NUMERIC(14,2)` tops out at 999,999,999,999.99, and every value with two decimal places
 * below 9.007e15 is exactly representable as a double. A `Decimal` this application merely
 * *displays* therefore survives the round trip unchanged. One it *calculated with* would
 * not, which is why it calculates with none.
 */

/** Rendered in place of a value the backend reported as undefined, absent or null. */
export const UNAVAILABLE = '—'

/**
 * Money, in its own currency, with the code shown.
 *
 * `currencyDisplay: 'code'` rather than a symbol, deliberately. A hotel's data can hold
 * several currencies at once, and `$1,240` beside `$980` is unreadable when one is USD and
 * the other AUD. `EUR 1,240.00` cannot be misread.
 *
 * Note what this does **not** accept: two amounts. There is no FX rate anywhere in this
 * system, and adding two currencies would produce a number that is not money.
 */
export function formatMoney(amount: string, currency: string, locale = 'en-GB'): string {
  const value = Number(amount)
  if (!Number.isFinite(value)) {
    return UNAVAILABLE
  }
  try {
    return new Intl.NumberFormat(locale, {
      style: 'currency',
      currency,
      currencyDisplay: 'code',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value)
  } catch {
    // An unknown ISO code makes Intl throw. The figure still matters, so it is shown with
    // the code the backend sent rather than swallowed.
    return `${currency} ${value.toFixed(2)}`
  }
}

/** A plain count, grouped. */
export function formatCount(value: number, locale = 'en-GB'): string {
  return new Intl.NumberFormat(locale).format(value)
}

/**
 * A backend `DATE` (`YYYY-MM-DD`) as a readable day.
 *
 * Parsed as a UTC instant and formatted in UTC, which is not a shortcut: a calendar date has
 * no time and no zone. `new Date('2026-09-17')` is midnight UTC, and formatting that in a
 * browser set to UTC-8 renders "16 September" -- a check-in date silently a day early. The
 * fix is to keep both ends of the conversion in the same zone, so the string that goes in is
 * the day that comes out.
 */
export function formatDate(isoDate: string, locale = 'en-GB'): string {
  const parts = isoDate.split('-').map(Number)
  if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) {
    return UNAVAILABLE
  }
  const [year, month, day] = parts as [number, number, number]
  return new Intl.DateTimeFormat(locale, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, month - 1, day)))
}

/** The same date without its year, for a range whose year is stated once. */
export function formatDayAndMonth(isoDate: string, locale = 'en-GB'): string {
  const parts = isoDate.split('-').map(Number)
  if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) {
    return UNAVAILABLE
  }
  const [year, month, day] = parts as [number, number, number]
  return new Intl.DateTimeFormat(locale, {
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, month - 1, day)))
}

/**
 * A backend timestamp, rendered in the **hotel's** time zone.
 *
 * `booked_at`, `created_at` and `cancelled_at` are `timestamptz`: real instants, unlike the
 * stay dates above. An instant has to be shown in some zone, and the hotel's is the only one
 * that makes an operations record consistent between the front desk and a regional manager
 * reading it from another country. The zone is named in the output for the same reason --
 * "00:47" means nothing without it.
 */
export function formatDateTime(isoDateTime: string, timeZone: string, locale = 'en-GB'): string {
  const instant = new Date(isoDateTime)
  if (Number.isNaN(instant.getTime())) {
    return UNAVAILABLE
  }
  try {
    return new Intl.DateTimeFormat(locale, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone,
    }).format(instant)
  } catch {
    // An unknown zone makes Intl throw. UTC is stated explicitly rather than silently
    // substituted, so nobody reads a UTC time as a local one.
    return `${new Intl.DateTimeFormat(locale, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'UTC',
    }).format(instant)} UTC`
  }
}

/**
 * Whole days between two `YYYY-MM-DD` dates, end-exclusive.
 *
 * End-exclusive because that is what a stay is: `check_in 17th, check_out 18th` is **one**
 * night, and the database's own CHECK requires `stay_date < check_out_date`. This is used
 * only to cross-check the nights the backend already reports -- never to replace them.
 */
export function nightsBetween(checkIn: string, checkOut: string): number | null {
  const start = Date.parse(`${checkIn}T00:00:00Z`)
  const end = Date.parse(`${checkOut}T00:00:00Z`)
  if (Number.isNaN(start) || Number.isNaN(end)) {
    return null
  }
  return Math.round((end - start) / 86_400_000)
}

/**
 * Show enough of a contact detail to recognise it, and no more.
 *
 * A booking list is read over a receptionist's shoulder by whoever is standing at the desk.
 * The operational need is "is this the right person?", which a masked value answers; the
 * full address is the guest's, not the room's. `a****@example.com` keeps the domain, which
 * is what actually distinguishes two guests with the same name.
 */
export function maskEmail(email: string): string {
  const at = email.indexOf('@')
  if (at <= 0) {
    return '•••'
  }
  const name = email.slice(0, at)
  const domain = email.slice(at)
  const head = name.slice(0, 1)
  return `${head}${'•'.repeat(Math.max(3, Math.min(name.length - 1, 6)))}${domain}`
}

/** The last four digits of a phone number, which is what a desk uses to confirm identity. */
export function maskPhone(phone: string): string {
  const digits = phone.replace(/\D/g, '')
  if (digits.length <= 4) {
    return '•'.repeat(digits.length)
  }
  return `••• ${digits.slice(-4)}`
}
