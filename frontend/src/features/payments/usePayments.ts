import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { DEFAULT_PAGE_SIZE, paymentService } from '@/services/payments/paymentService'
import type { ChargeRequest, Payment, RefundRequest } from '@/types/payment'

/**
 * The payment ledger for one booking, and the two postings that can be added to it.
 *
 * ## Nothing financial is computed here
 *
 * This hook holds a list of rows the server sent and reports what the server said about a
 * posting. It sums nothing. It does not add up charges, does not subtract refunds, does not
 * derive a balance and does not track what remains refundable. Every one of those figures
 * belongs to reconciliation, which the booking page reads from its own endpoint, and the
 * per-payment refundable amount belongs to nobody outside the server -- it is computed under
 * a row lock and no endpoint exposes it.
 *
 * ## No optimistic row, ever
 *
 * A successful posting is followed by a **re-read of the ledger**, not by appending the
 * returned object to the local array. The returned object is correct, but the list is
 * paginated and ordered oldest-first by the server, and inserting a row into a page is a
 * guess about where the server would have put it. More importantly, a posting changes the
 * booking's financial position, and the page must re-read reconciliation anyway -- so the
 * ledger is re-read in the same breath and the two cannot disagree.
 *
 * A **failed** posting appends nothing. There is no state in which a row appears before the
 * server has confirmed it.
 *
 * ## One posting at a time, and no retries
 *
 * `inFlight` is a ref, not state: two clicks in the same tick both read the same stale
 * `false` from a state variable and both fire. On money that is not a cosmetic bug.
 *
 * Nothing is ever retried automatically. The idempotency index means a repeat of the same
 * `(provider, transaction_reference)` is refused rather than duplicated -- but a posting
 * *without* a reference has no such protection, so an automatic retry could take money
 * twice. Retrying is the operator's decision, made after reading what happened.
 */

export type LedgerStatus = 'idle' | 'loading' | 'ready' | 'error'
export type PostingKind = 'charge' | 'refund'

export interface PaymentPosted {
  readonly kind: PostingKind
  /** Copy written here, never the backend's message. */
  readonly message: string
  /** The posting the server created, for the confirmation line. */
  readonly payment: Payment
}

export interface PaymentsState {
  readonly status: LedgerStatus
  readonly payments: readonly Payment[]
  /** The server's own total across all pages. Never derived from the rows held. */
  readonly total: number
  readonly pages: number
  readonly page: number
  readonly error: ApiError | null
  /** Which posting is in flight, or null. */
  readonly pending: PostingKind | null
  /** The last success. Cleared when a new attempt starts. */
  readonly posted: PaymentPosted | null
  /** The last failed posting. Rendered through `describeFailure`, never raw. */
  readonly postError: ApiError | null
  readonly setPage: (page: number) => void
  readonly reload: () => void
  readonly charge: (payload: ChargeRequest) => Promise<boolean>
  readonly refund: (payload: RefundRequest) => Promise<boolean>
  readonly dismiss: () => void
}

export interface UsePaymentsOptions {
  readonly hotelPublicId: string | null
  readonly bookingPublicId: string | undefined
  /**
   * Called after a posting the server accepted.
   *
   * The booking page uses it to re-read reconciliation: a charge or a refund changes
   * `charged_total`, `refunded_total`, `net_paid` and `outstanding_amount`, and every one of
   * those is the server's to recalculate.
   */
  readonly onPosted: () => Promise<void> | void
}

export function usePayments({
  hotelPublicId,
  bookingPublicId,
  onPosted,
}: UsePaymentsOptions): PaymentsState {
  const [status, setStatus] = useState<LedgerStatus>('idle')
  const [payments, setPayments] = useState<readonly Payment[]>([])
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [error, setError] = useState<ApiError | null>(null)
  const [attempt, setAttempt] = useState(0)

  const [pending, setPending] = useState<PostingKind | null>(null)
  const [posted, setPosted] = useState<PaymentPosted | null>(null)
  const [postError, setPostError] = useState<ApiError | null>(null)

  const inFlight = useRef(false)

  useEffect(() => {
    if (hotelPublicId === null || bookingPublicId === undefined) {
      setStatus('idle')
      setPayments([])
      setTotal(0)
      setPages(0)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    paymentService
      .listForBooking(
        hotelPublicId,
        bookingPublicId,
        { page, pageSize: DEFAULT_PAGE_SIZE },
        controller.signal,
      )
      .then((result) => {
        if (cancelled) {
          return
        }
        /*
         * A 200 is not proof of the documented shape. The client performs no runtime
         * validation, so a proxy or a moved endpoint can answer 200 with something else --
         * and `payments.map` on that would take the section down from a render, past this
         * `catch`. An unreadable answer is a failure, never an empty ledger.
         */
        if (!Array.isArray((result as { items?: unknown }).items)) {
          setPayments([])
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The payments response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setPayments(result.items)
        setTotal(result.total)
        setPages(result.pages)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setPayments([])
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The payments could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, bookingPublicId, page, attempt])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  /** Runs one posting. Returns whether the server accepted it, so a form can close itself. */
  const post = useCallback(
    async (
      kind: PostingKind,
      call: (hotel: string, booking: string) => Promise<Payment>,
      message: string,
    ): Promise<boolean> => {
      if (inFlight.current || hotelPublicId === null || bookingPublicId === undefined) {
        return false
      }
      inFlight.current = true
      setPending(kind)
      setPostError(null)
      setPosted(null)

      try {
        const payment = await call(hotelPublicId, bookingPublicId)
        if (!isPayment(payment)) {
          throw new ApiError(
            201,
            ApiError.MALFORMED_CODE,
            'The posting response was not in the expected format.',
          )
        }
        setPosted({ kind, message, payment })
        // Re-read rather than append: see the module docstring. Page 1 shows the ledger's
        // start, and a new posting is at its end -- so the page is left where it is and the
        // current page is refetched, which is what `reload` does.
        setAttempt((n) => n + 1)
        await onPosted()
        return true
      } catch (cause: unknown) {
        setPostError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The posting could not be completed.'),
        )
        return false
      } finally {
        inFlight.current = false
        setPending(null)
      }
    },
    [hotelPublicId, bookingPublicId, onPosted],
  )

  const charge = useCallback(
    (payload: ChargeRequest) =>
      post(
        'charge',
        (hotel, booking) => paymentService.createCharge(hotel, booking, payload),
        'Charge recorded.',
      ),
    [post],
  )

  const refund = useCallback(
    (payload: RefundRequest) =>
      post(
        'refund',
        (hotel, booking) => paymentService.createRefund(hotel, booking, payload),
        'Refund recorded.',
      ),
    [post],
  )

  const dismiss = useCallback(() => {
    setPosted(null)
    setPostError(null)
  }, [])

  return {
    status,
    payments,
    total,
    pages,
    page,
    error,
    pending,
    posted,
    postError,
    setPage,
    reload,
    charge,
    refund,
    dismiss,
  }
}

/** Whether a 201 body is actually the documented payment shape. */
function isPayment(value: unknown): value is Payment {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as { public_id?: unknown; kind?: unknown; amount?: unknown }
  return (
    typeof candidate.public_id === 'string' &&
    (candidate.kind === 'charge' || candidate.kind === 'refund') &&
    typeof candidate.amount === 'string'
  )
}
