import type { DecimalString } from '@/types/booking'

/**
 * The payment contracts, transcribed from `app.schemas.payment`.
 *
 * Confirmed against the live API -- `/openapi.json` plus real requests -- rather than read
 * from source alone. Four facts that check established, each of which shaped the UI:
 *
 * 1. **`amount` is always positive, for both kinds.** Direction lives in `kind`, never in the
 *    sign; that is the frozen schema's choice and `ck_payments_amount_positive` enforces it.
 *    Nothing in this application negates a refund.
 * 2. **Every `Decimal` is a JSON string**, on the way in as well as out. The request schema
 *    accepts a string for `amount`, so a figure typed by a user reaches PostgreSQL without
 *    ever being a JavaScript number.
 * 3. **`kind` is not accepted on either request.** The endpoint determines it -- a charge
 *    must not name a parent and a refund must, and one payload able to express both would be
 *    a payload able to express an invalid combination. Sending `kind` is a 422.
 * 4. **No internal key appears anywhere**: not `payments.id`, not `booking_id`, not
 *    `hotel_id`, not `refunded_payment_id`. `public_id` is the identity throughout.
 */

/** Mirrors `ck_payments_method_valid`. */
export type PaymentMethod =
  | 'card'
  | 'cash'
  | 'bank_transfer'
  | 'online_gateway'
  | 'ota_collect'
  | 'voucher'

/**
 * Mirrors `ck_payments_status_valid`.
 *
 * Seven states, and they are the *posting's* own status -- not the booking's financial
 * position. The booking-level answer ("unpaid", "partially_paid", "paid", "overpaid") is a
 * different vocabulary, derived by reconciliation, and the two must not be confused.
 */
export type PaymentStatus =
  | 'pending'
  | 'authorized'
  | 'captured'
  | 'failed'
  | 'refunded'
  | 'partially_refunded'
  | 'cancelled'

/** Mirrors `ck_payments_kind_valid`. Charges and refunds are one table split by this. */
export type PaymentKind = 'charge' | 'refund'

/** One posting against a booking, as the API returns it. */
export interface Payment {
  readonly hotel_public_id: string
  readonly booking_public_id: string
  readonly public_id: string
  readonly kind: PaymentKind
  /** Always positive. `NUMERIC(14,2)` on the wire as a string. */
  readonly amount: DecimalString
  /** The posting's own currency. A booking may carry postings in more than one. */
  readonly currency: string
  readonly method: PaymentMethod
  readonly status: PaymentStatus
  /** An instant. Required by `ck_payments_captured_has_paid_at` when status is `captured`. */
  readonly paid_at: string | null
  readonly provider: string | null
  /**
   * The processor's own reference.
   *
   * Half of the idempotency key: a partial unique index over
   * `(provider, transaction_reference) WHERE transaction_reference IS NOT NULL` means a
   * repeated webhook cannot create a second row. Verified live -- the same pair twice is a
   * 409 even when the amount differs, and omitting it entirely lets a second posting through,
   * which is what "partial" means.
   */
  readonly transaction_reference: string | null
  /** Present only on a refund; null on a charge, mirroring the biconditional CHECK. */
  readonly refunds_public_id: string | null
  readonly failure_reason: string | null
  /**
   * The last four digits, and the schema stores nothing else about a card.
   *
   * No PAN, no CVV, no expiry, no cardholder name -- there is no field for any of them, and
   * `additionalProperties: false` makes that enforceable: sending `card_number` is a 422,
   * verified live.
   */
  readonly card_last_four: string | null
  readonly created_at: string
  readonly updated_at: string
}

/**
 * `POST .../payments` -- the `ChargeCreate` payload.
 *
 * Required: `amount`, `currency`, `method`. Everything else is optional, and `status`
 * defaults to `pending` server-side.
 */
export interface ChargeRequest {
  /** A decimal STRING. Never a number: see the module docstring. */
  readonly amount: DecimalString
  readonly currency: string
  readonly method: PaymentMethod
  readonly status?: PaymentStatus
  /** ISO instant. Required by the server when `status` is `captured`. */
  readonly paid_at?: string
  readonly provider?: string
  readonly transaction_reference?: string
  readonly card_last_four?: string
}

/**
 * `POST .../payments/refunds` -- the `RefundCreate` payload.
 *
 * Identical to a charge plus `refunds_public_id`, which names the payment being reversed.
 * The server resolves that id **through the booking**, so a refund can only ever reverse a
 * payment the caller already had access to -- a payment on another booking is a 404, which is
 * also what stops this endpoint being used to probe for one.
 */
export interface RefundRequest {
  readonly amount: DecimalString
  /** Must equal the parent's currency; the server refuses anything else with a 409. */
  readonly currency: string
  readonly method: PaymentMethod
  readonly refunds_public_id: string
  readonly status?: PaymentStatus
  readonly paid_at?: string
  readonly provider?: string
  readonly transaction_reference?: string
}
