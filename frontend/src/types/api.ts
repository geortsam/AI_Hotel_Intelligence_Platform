/**
 * The envelopes every backend endpoint shares.
 *
 * These mirror `app.schemas.common` on the backend -- `ErrorDetail`, `ErrorBody`,
 * `ErrorResponse` and `Page` -- and nothing else. They are the shapes the API genuinely
 * returns, transcribed rather than invented; per-domain response types (bookings, guests,
 * rooms) belong with the services that fetch them and are not part of this foundation.
 */

/** One field-level problem. Present on validation failures. */
export interface ErrorDetail {
  /** Path to the offending value, e.g. `['body', 'check_in_date']`. */
  readonly location: readonly string[]
  /** What is wrong with this value. */
  readonly message: string
  /** Machine-readable validation failure type. */
  readonly type: string
}

/** The contents of an error response. */
export interface ErrorBody {
  /** Stable, machine-readable error code. */
  readonly code: string
  /** Human-readable summary. The backend guarantees this is safe to display. */
  readonly message: string
  /** Field-level detail; empty for non-validation errors. */
  readonly details: readonly ErrorDetail[]
}

/** The single error shape returned by every failing endpoint. */
export interface ErrorResponse {
  readonly error: ErrorBody
}

/** One page of results, with everything needed to size a pager without probing for the end. */
export interface Page<ItemT> {
  /** The rows on this page. */
  readonly items: readonly ItemT[]
  /** Total rows matching the query, across all pages. */
  readonly total: number
  /** 1-based index of this page. */
  readonly page: number
  /** Maximum rows per page. */
  readonly page_size: number
  /** Total number of pages; 0 when there are no rows. */
  readonly pages: number
}
