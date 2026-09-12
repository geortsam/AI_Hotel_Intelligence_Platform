import type { DecimalString } from '@/types/analytics'
import type { DateRange } from '@/types/analytics'

/**
 * The revenue and expense ledger contracts, transcribed from `app.schemas.finance` and
 * `app.schemas.analytics`, then confirmed against the live API with real requests.
 *
 * Five facts that check established, each of which shaped the UI rather than merely being
 * recorded here:
 *
 * 1. **Neither ledger row has an identifier.** `RevenueResponse` and `ExpenseResponse` carry
 *    no `public_id` and no internal key -- the schema's own docstring says two byte-identical
 *    rows coexist happily. There is therefore no single-entry URL, and no way to name one row
 *    to the server. Verified: `GET /hotels/{h}/revenue/1` is a 404, `PATCH` and `DELETE` on
 *    the collection are **405**.
 * 2. **`amount` is signed.** Neither table carries a positivity CHECK, deliberately: a
 *    mistake is corrected by posting a compensating negative line, which keeps the journal
 *    additive. Verified -- `-25.00` and `0.00` are both accepted with a 201. `tax_amount` is
 *    the opposite and refuses a negative with a 422.
 * 3. **Currency is not tied to the hotel's.** The schema says so explicitly, and a USD line
 *    on a EUR hotel was accepted with a 201. So a currency column is shown on every row and
 *    figures in different currencies are never brought together.
 * 4. **Only revenue has a booking link.** `expenses` has no `booking_id` column at all;
 *    sending `booking_public_id` on an expense is a 422 from `extra="forbid"`.
 * 5. **Every `Decimal` is a JSON string**, in both directions. The request schema accepts a
 *    string, so a figure typed by a person reaches `NUMERIC(14,2)` without ever having been
 *    a JavaScript number.
 */

export type { DecimalString }

/** `Literal["monthly", "quarterly", "annual"]` on `ExpenseCreate`. */
export type RecurrenceInterval = 'monthly' | 'quarterly' | 'annual'

/**
 * One posted revenue line, as the API returns it.
 *
 * No identifier: see fact 1 in the module docstring. Rows are keyed in React by index within
 * the page, because the server offers nothing else and inventing a key would imply an
 * identity the record does not have.
 */
export interface RevenueEntry {
  readonly hotel_public_id: string
  readonly category_code: string
  /** The booking this line was earned against, when there is one. */
  readonly booking_public_id: string | null
  /** `YYYY-MM-DD`, hotel-local. The date the revenue belongs to, not when it was posted. */
  readonly revenue_date: string
  /** Signed. A negative line is a correction of an earlier one. */
  readonly amount: DecimalString
  /** Never negative -- `ck_revenue_tax_amount_non_negative`. */
  readonly tax_amount: DecimalString
  readonly currency: string
  readonly description: string | null
  /** Free text, at most 200 characters. Not an idempotency key: repeats are allowed. */
  readonly reference: string | null
  readonly created_at: string
  readonly updated_at: string
}

/** One posted expense line. Has no booking field, and the column does not exist either. */
export interface ExpenseEntry {
  readonly hotel_public_id: string
  readonly category_code: string
  readonly expense_date: string
  /** Signed. A negative line is a credit note. */
  readonly amount: DecimalString
  readonly tax_amount: DecimalString
  readonly currency: string
  readonly description: string | null
  readonly vendor: string | null
  readonly invoice_reference: string | null
  readonly is_recurring: boolean
  /** Present exactly when `is_recurring`; the server enforces the biconditional. */
  readonly recurrence_interval: RecurrenceInterval | null
  readonly created_at: string
  readonly updated_at: string
}

/**
 * `POST /hotels/{h}/revenue` -- the `RevenueCreate` payload.
 *
 * Required: `category_code`, `revenue_date`, `amount`, `currency`. `tax_amount` defaults to
 * `0` server-side. `extra="forbid"` makes any other field a 422, verified with a stray
 * `rate`.
 */
export interface RevenueCreateRequest {
  readonly category_code: string
  readonly revenue_date: string
  /** A decimal STRING, and it may carry a leading `-`. */
  readonly amount: DecimalString
  readonly currency: string
  readonly tax_amount?: DecimalString
  readonly booking_public_id?: string
  readonly description?: string
  readonly reference?: string
}

/**
 * `POST /hotels/{h}/expenses` -- the `ExpenseCreate` payload.
 *
 * `is_recurring` and `recurrence_interval` are bound by a `model_validator`: each is a 422
 * without the other. Verified both ways.
 */
export interface ExpenseCreateRequest {
  readonly category_code: string
  readonly expense_date: string
  readonly amount: DecimalString
  readonly currency: string
  readonly tax_amount?: DecimalString
  readonly description?: string
  readonly vendor?: string
  readonly invoice_reference?: string
  readonly is_recurring?: boolean
  readonly recurrence_interval?: RecurrenceInterval
}

/**
 * A revenue category, from the **global** catalogue.
 *
 * Not hotel-scoped: `/revenue-categories` sits outside the hotel segment and every
 * authenticated user may read it. Creating, amending or retiring one requires a platform
 * grant -- verified live, a hotel owner receives 403 -- so this application reads the
 * catalogue and offers no management UI for it.
 */
export interface RevenueCategory {
  readonly code: string
  readonly name: string
  /**
   * Whether lines in this category are room revenue.
   *
   * Carried through to the breakdown so a consumer can apply the schema's exclusion rule
   * without re-deriving it from a name. It is **shown**, not acted on: the overview keeps
   * room revenue and ledger revenue apart precisely so neither is folded into the other, and
   * folding them here would be the double count that separation exists to prevent.
   */
  readonly is_room_revenue: boolean
  readonly is_active: boolean
}

/** An expense category, from the same kind of global catalogue. */
export interface ExpenseCategory {
  readonly code: string
  readonly name: string
  readonly is_fixed_cost: boolean
  readonly is_active: boolean
}

/**
 * One category's total **within one currency**, as the server computed it.
 *
 * The currency is part of the grouping key, not a label on a combined figure: the backend
 * groups by `(category_code, currency)` and orders by both. So a category with lines in two
 * currencies arrives as two buckets, and this application shows two rows. Adding them would
 * produce a number that is not money.
 */
export interface RevenueBucket {
  readonly category_code: string
  readonly is_room_revenue: boolean
  readonly currency: string
  readonly amount: DecimalString
  readonly tax_amount: DecimalString
  readonly entry_count: number
}

export interface ExpenseBucket {
  readonly category_code: string
  readonly is_fixed_cost: boolean
  readonly currency: string
  readonly amount: DecimalString
  readonly tax_amount: DecimalString
  readonly entry_count: number
}

/**
 * `GET /hotels/{h}/analytics/revenue-by-category`.
 *
 * **The only sanctioned source of a revenue total in this application.** Both `date_from`
 * and `date_to` are required -- omitting either is a 422, verified -- and `range` echoes back
 * what the server actually used, which is what the UI labels the figures with.
 */
export interface RevenueBreakdownResponse {
  readonly hotel_public_id: string
  readonly range: DateRange
  readonly categories: readonly RevenueBucket[]
}

/** `GET /hotels/{h}/analytics/expenses-by-category`. Same shape, same rules. */
export interface ExpenseBreakdownResponse {
  readonly hotel_public_id: string
  readonly range: DateRange
  readonly categories: readonly ExpenseBucket[]
}
