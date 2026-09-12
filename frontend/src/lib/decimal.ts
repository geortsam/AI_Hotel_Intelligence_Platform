/**
 * Handling a money figure a person typed, without ever making it a number.
 *
 * ## Why this module exists at all
 *
 * `parseFloat('0.1') + parseFloat('0.2')` is `0.30000000000000004`. That is not a curiosity;
 * it is the reason `payments.amount` is `NUMERIC(14,2)` in PostgreSQL and arrives as a JSON
 * string. The request schema accepts a string too, so there is a path from a keyboard to the
 * database on which the value is never a JavaScript number -- and this module is that path.
 *
 * Everything here works on strings. Nothing calls `parseFloat`, `Number()` or `toFixed` on a
 * monetary value. The only comparison offered is {@link compareDecimalStrings}, which
 * compares digit by digit rather than by converting.
 *
 * The one place a money string legitimately becomes a number is `formatMoney`, at the very
 * edge, to hand it to `Intl.NumberFormat` for display -- and the values displayed are bounded
 * well inside the range a double represents exactly. See `@/lib/format`.
 */

/**
 * A positive money amount with at most two decimal places and at most twelve integer digits.
 *
 * Matched to the column: `NUMERIC(14, 2)` is fourteen significant digits of which two are
 * decimal, so twelve before the point. The backend additionally requires `amount > 0` for
 * both charges and refunds -- direction lives in `kind`, never in the sign -- so a leading
 * `-` is rejected here rather than sent to be refused.
 *
 * Deliberately strict about shape: `1.5` is accepted and stored as `1.50`, `1.555` is not,
 * and `.5` is not. A value the server would reject with a 422 is better caught at the field
 * that produced it, where it can be explained.
 */
const POSITIVE_MONEY = /^(0|[1-9]\d{0,11})(\.\d{1,2})?$/

export function isPositiveMoneyString(value: string): boolean {
  const trimmed = value.trim()
  return POSITIVE_MONEY.test(trimmed) && !isZeroString(trimmed)
}

/** Whether a well-formed money string is zero. `ck_payments_amount_positive` refuses it. */
function isZeroString(value: string): boolean {
  return /^0(\.0{1,2})?$/.test(value)
}

/**
 * The string to send, from the string a person typed.
 *
 * Trims surrounding whitespace and nothing else. **The digits are not touched**: no padding
 * to two decimal places, no stripping of a leading zero, no rounding. The server stores the
 * value at the column's scale, and a client that "helpfully" rewrote `5` as `5.00` before
 * sending would be normalising a figure it does not own. Whitespace is the one thing that is
 * unambiguously not part of the number.
 */
export function toDecimalString(value: string): string {
  return value.trim()
}

/**
 * Compare two decimal strings without converting either to a number.
 *
 * Returns a negative number, zero, or a positive number, like a comparator. Used only for
 * local sanity checks -- "is this refund larger than the payment it reverses?" -- never to
 * compute a balance. The authoritative comparison happens in PostgreSQL under a row lock.
 *
 * Both inputs must already have passed {@link isPositiveMoneyString}; the padding below
 * assumes a well-formed, non-negative value.
 */
export function compareDecimalStrings(left: string, right: string): number {
  const [leftWhole = '0', leftFraction = ''] = left.trim().split('.')
  const [rightWhole = '0', rightFraction = ''] = right.trim().split('.')

  // Compare the integer parts by length first, then lexically -- for equal-length digit
  // strings with no sign, lexical order is numeric order.
  const l = leftWhole.replace(/^0+(?=\d)/, '')
  const r = rightWhole.replace(/^0+(?=\d)/, '')
  if (l.length !== r.length) {
    return l.length - r.length
  }
  if (l !== r) {
    return l < r ? -1 : 1
  }

  // Then the fraction, padded to the same width so `.5` and `.50` compare equal.
  const width = Math.max(leftFraction.length, rightFraction.length)
  const lf = leftFraction.padEnd(width, '0')
  const rf = rightFraction.padEnd(width, '0')
  if (lf === rf) {
    return 0
  }
  return lf < rf ? -1 : 1
}

/**
 * A **signed** money amount, for a ledger line.
 *
 * Revenue and expenses are the one place a negative figure is correct rather than a mistake.
 * Neither `revenue.amount` nor `expenses.amount` carries a positivity CHECK, and the schema
 * says why: a mistake is corrected by posting a compensating line, so the running total stays
 * additive. Verified live -- `-25.00` and `0.00` are both accepted with a 201.
 *
 * `tax_amount` is the opposite: `ck_revenue_tax_amount_non_negative` makes a negative one a
 * 422, so it uses {@link isNonNegativeMoneyString}.
 */
const SIGNED_MONEY = /^-?(0|[1-9]\d{0,11})(\.\d{1,2})?$/

export function isSignedMoneyString(value: string): boolean {
  return SIGNED_MONEY.test(value.trim())
}

/** Zero or more, at most two decimal places. What a tax field accepts. */
export function isNonNegativeMoneyString(value: string): boolean {
  const trimmed = value.trim()
  return SIGNED_MONEY.test(trimmed) && !trimmed.startsWith('-')
}

/** Whether a well-formed signed amount is below zero, for labelling a correcting line. */
export function isNegativeMoneyString(value: string): boolean {
  return value.trim().startsWith('-')
}
