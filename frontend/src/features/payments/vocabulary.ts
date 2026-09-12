import type { BadgeTone } from '@/components/ui/Badge'
import type { PaymentMethod, PaymentStatus } from '@/types/payment'

/**
 * How the payment vocabularies are shown, and the one rule about refundability.
 *
 * ## Colour never carries the meaning
 *
 * Every status renders as a word plus a tone, and a longer description reaches a screen
 * reader. Removing the colour changes nothing that can be read -- which matters more here
 * than anywhere else in the application, because these labels describe money.
 *
 * ## What this file does NOT compute
 *
 * No total, no balance, no refundable amount. The only judgement it makes is
 * {@link movedNoMoney}, and that is a two-element set restated from the backend, used to
 * decide whether to *offer* a control -- never to decide an amount.
 */

export interface StatusPresentation {
  readonly label: string
  readonly tone: BadgeTone
  /** Announced after the label, so the state is understood rather than merely named. */
  readonly description: string
}

/**
 * The seven statuses a posting can hold.
 *
 * These describe the **posting**, not the booking's financial position. The booking-level
 * answer -- unpaid, partially paid, paid, overpaid -- is a different vocabulary that
 * reconciliation derives, and conflating the two is how a screen ends up saying a booking is
 * "refunded" because one of its five payments is.
 */
const STATUS: Readonly<Record<PaymentStatus, StatusPresentation>> = {
  pending: {
    label: 'Pending',
    tone: 'warning',
    description: 'Pending — recorded but not yet settled',
  },
  authorized: {
    label: 'Authorized',
    tone: 'info',
    description: 'Authorized — funds are held but not captured',
  },
  captured: {
    label: 'Captured',
    tone: 'success',
    description: 'Captured — the money has been taken',
  },
  failed: {
    label: 'Failed',
    tone: 'danger',
    description: 'Failed — no money moved, and nothing can be refunded against it',
  },
  refunded: {
    label: 'Refunded',
    tone: 'neutral',
    description: 'Refunded — this posting has been reversed in full',
  },
  partially_refunded: {
    label: 'Partly refunded',
    tone: 'neutral',
    description: 'Partly refunded — some of this posting has been reversed',
  },
  cancelled: {
    label: 'Cancelled',
    tone: 'danger',
    description: 'Cancelled — no money moved, and nothing can be refunded against it',
  },
}

export function statusPresentation(status: PaymentStatus): StatusPresentation {
  return (
    STATUS[status] ?? {
      label: status,
      tone: 'neutral' as BadgeTone,
      description: `Status reported by the server as "${status}"`,
    }
  )
}

/**
 * The statuses the backend treats as having moved no money.
 *
 * Restated from `VOIDED_PAYMENT_STATUSES`, which is exactly `('failed', 'cancelled')`. The
 * backend uses it in two places, both verified live: a refund against such a payment is a
 * 409 saying it "moved no money", and `refunded_total` excludes rows in these states from the
 * already-refunded sum.
 *
 * **This is used to decide whether to offer a control, never to decide an amount.** The
 * server still refuses; not offering a button whose only outcome is a 409 is presentation.
 * `vocabulary.test.ts` pins the set so a backend change that this copy has not followed
 * fails a test.
 */
const VOIDED: readonly PaymentStatus[] = ['failed', 'cancelled']

export function movedNoMoney(status: PaymentStatus): boolean {
  return VOIDED.includes(status)
}

/** The six methods, as they read in an interface. */
const METHOD: Readonly<Record<PaymentMethod, string>> = {
  card: 'Card',
  cash: 'Cash',
  bank_transfer: 'Bank transfer',
  online_gateway: 'Online gateway',
  ota_collect: 'OTA collect',
  voucher: 'Voucher',
}

export function methodLabel(method: PaymentMethod): string {
  return METHOD[method] ?? method
}

/** Every method, for a form control. Ordered as a front desk would reach for them. */
export const PAYMENT_METHODS: readonly PaymentMethod[] = [
  'card',
  'cash',
  'bank_transfer',
  'online_gateway',
  'ota_collect',
  'voucher',
]

/**
 * The statuses a person may post a payment *as*.
 *
 * A deliberate subset of the seven. `refunded` and `partially_refunded` describe what has
 * happened to a posting through other postings, so recording one directly would be asserting
 * a history that does not exist; `authorized` is a gateway's word about a hold this platform
 * does not place. What is left is what a member of staff can actually witness at the desk.
 *
 * The API accepts all seven. This is a UI restraint, not a rule, and it is stated as one.
 */
export const POSTABLE_STATUSES: readonly PaymentStatus[] = [
  'pending',
  'captured',
  'failed',
  'cancelled',
]
