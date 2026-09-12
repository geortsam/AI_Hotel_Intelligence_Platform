import { useCallback, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { bookingMutations } from '@/services/bookings/bookingService'
import type {
  Booking,
  BookingStatus,
  StayExtensionRequest,
  StayModificationRequest,
  StayRepricing,
} from '@/types/booking'

/**
 * Running the three booking mutations, without ever lying about the outcome.
 *
 * ## No optimistic UI, deliberately
 *
 * Nothing here changes what is on screen until the server has answered. That is not caution
 * for its own sake: every one of these operations can be refused for a reason the client
 * cannot see. A status move is judged against the committed status under a row lock. A stay
 * change is judged by the database's exclusion constraint against every other booking. An
 * extension is refused if it does not move the departure strictly later. Painting the new
 * state first and rolling it back on failure would show the user a booking that was
 * confirmed for as long as the round trip took, and confirmations are exactly the thing an
 * operations console must not invent.
 *
 * So the flow is: idle -> submitting -> (the server's answer).
 *
 * ## Which answer is adopted, and the one that is not
 *
 * A status change and an extension return a complete booking, and it is adopted verbatim.
 * **A stay modification is not**: `PATCH .../stay` returns `booking.rooms: []`, measured
 * against the live API, while a GET of the same booking immediately afterwards has the room
 * and its priced nights. That response's `repricing` is complete and is used; its `booking`
 * is discarded and the page re-reads. See `modifyStay` below and `refreshBooking`.
 *
 * ## No retries
 *
 * The client never repeats a mutation. `POST /stay/extension` is deliberately not
 * idempotent -- the backend's own words -- and a repeat of the same target date is a 409
 * rather than a no-op, precisely so a retried request cannot silently extend a stay twice.
 * A retry button that re-sent it would defeat that. Retrying is the user's decision, made
 * after reading what happened.
 *
 * ## One in flight at a time
 *
 * `pending` gates every entry point, and it is checked from a ref rather than from state:
 * two clicks in the same tick both read the same stale `false` from a state variable and
 * both fire. The ref is written synchronously, so the second call returns immediately.
 * Buttons are disabled too -- that is the visible half, and this is the half that holds
 * when a keyboard repeat or a double-tap gets past it.
 */

/** Which operation is in flight, or `null`. Named so the UI can disable precisely. */
export type MutationKind = 'status' | 'modify' | 'extend'

export interface MutationOutcome {
  readonly kind: MutationKind
  /** Copy written here, never the backend's message. */
  readonly message: string
  /** Present for stay changes: what the server said the change did to the money. */
  readonly repricing: StayRepricing | null
}

export interface BookingMutationsState {
  readonly pending: MutationKind | null
  /** The last success, for the page's confirmation region. Cleared when a new attempt starts. */
  readonly outcome: MutationOutcome | null
  /** The last failure. Never rendered raw -- the page runs it through `describeFailure`. */
  readonly error: ApiError | null
  readonly changeStatus: (target: BookingStatus, reason?: string) => Promise<void>
  readonly modifyStay: (payload: StayModificationRequest) => Promise<void>
  readonly extendStay: (payload: StayExtensionRequest) => Promise<void>
  readonly dismiss: () => void
}

export interface BookingMutationsOptions {
  readonly hotelPublicId: string | null
  readonly bookingPublicId: string | undefined
  /** Adopt the booking the server returned. Used where that response is complete. */
  readonly onBooking: (booking: Booking) => void
  /** Re-read the authoritative financial summary. Called after an extension. */
  readonly onStayChanged: () => Promise<void>
  /**
   * Re-read the whole booking.
   *
   * Used by the stay modification alone, because its response is the one that cannot be
   * adopted: `PATCH .../stay` answers with `booking.rooms: []`. See `refreshBooking`.
   */
  readonly onStayReplaced: () => Promise<void>
}

export function useBookingMutations({
  hotelPublicId,
  bookingPublicId,
  onBooking,
  onStayChanged,
  onStayReplaced,
}: BookingMutationsOptions): BookingMutationsState {
  const [pending, setPending] = useState<MutationKind | null>(null)
  const [outcome, setOutcome] = useState<MutationOutcome | null>(null)
  const [error, setError] = useState<ApiError | null>(null)

  /** The duplicate-submission guard. A ref, because state is a render behind. */
  const inFlight = useRef(false)

  const run = useCallback(
    async <T,>(
      kind: MutationKind,
      call: (hotel: string, booking: string) => Promise<T>,
      onSuccess: (result: T) => Promise<void> | void,
    ) => {
      if (inFlight.current || hotelPublicId === null || bookingPublicId === undefined) {
        return
      }
      inFlight.current = true
      setPending(kind)
      setError(null)
      setOutcome(null)

      try {
        const result = await call(hotelPublicId, bookingPublicId)
        await onSuccess(result)
      } catch (cause: unknown) {
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The request could not be completed.'),
        )
      } finally {
        inFlight.current = false
        setPending(null)
      }
    },
    [hotelPublicId, bookingPublicId],
  )

  const changeStatus = useCallback(
    async (target: BookingStatus, reason?: string) => {
      await run(
        'status',
        (hotel, booking) =>
          bookingMutations.updateStatus(hotel, booking, {
            status: target,
            // Sent only when there is one. An empty string is not a reason, and the field is
            // meaningless outside a cancellation.
            ...(reason !== undefined && reason.trim() !== ''
              ? { cancellation_reason: reason.trim() }
              : {}),
          }),
        (updated) => {
          if (!isBooking(updated)) {
            throw new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The update response was not in the expected format.',
            )
          }
          onBooking(updated)
          setOutcome({ kind: 'status', message: 'Booking status updated.', repricing: null })
          // No reconciliation refresh: a status change writes no night row, and this
          // application never sends `total_amount`, so neither total in the financial
          // summary can have moved.
        },
      )
    },
    [run, onBooking],
  )

  const modifyStay = useCallback(
    async (payload: StayModificationRequest) => {
      await run(
        'modify',
        (hotel, booking) => bookingMutations.modifyStay(hotel, booking, payload),
        async (result) => {
          if (!isStayResponse(result)) {
            throw new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The stay response was not in the expected format.',
            )
          }
          /*
           * The response's `booking` is NOT adopted here, and this is the one place that is
           * true. `PATCH .../stay` returns it with an empty `rooms` array -- measured, not
           * assumed -- while a GET a moment later has the room and its priced nights.
           * Adopting it would render "Rooms (0)" for a booking that has one.
           *
           * `repricing` IS used: it is complete, and it is the only authoritative account of
           * what the change did to the money.
           */
          setOutcome({ kind: 'modify', message: 'Stay updated.', repricing: result.repricing })
          await onStayReplaced()
        },
      )
    },
    [run, onStayReplaced],
  )

  const extendStay = useCallback(
    async (payload: StayExtensionRequest) => {
      await run(
        'extend',
        (hotel, booking) => bookingMutations.extendStay(hotel, booking, payload),
        async (result) => {
          if (!isStayResponse(result)) {
            throw new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The extension response was not in the expected format.',
            )
          }
          onBooking(result.booking)
          setOutcome({ kind: 'extend', message: 'Stay extended.', repricing: result.repricing })
          await onStayChanged()
        },
      )
    },
    [run, onBooking, onStayChanged],
  )

  const dismiss = useCallback(() => {
    setOutcome(null)
    setError(null)
  }, [])

  return { pending, outcome, error, changeStatus, modifyStay, extendStay, dismiss }
}

/*
 * A 200 is not proof of the documented shape.
 *
 * The API client performs no runtime validation -- the response type is the caller's claim --
 * so a proxy, a captive portal or a moved endpoint can answer 200 with something else. On a
 * mutation that matters more than on a read: adopting a malformed object as "the booking as
 * it now stands" would replace a correct page with nonsense *and* report success.
 */
function isBooking(value: unknown): value is Booking {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as { public_id?: unknown; status?: unknown; rooms?: unknown }
  return (
    typeof candidate.public_id === 'string' &&
    typeof candidate.status === 'string' &&
    Array.isArray(candidate.rooms)
  )
}

function isStayResponse(
  value: unknown,
): value is { booking: Booking; repricing: StayRepricing } {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as { booking?: unknown; repricing?: unknown }
  if (!isBooking(candidate.booking)) {
    return false
  }
  const repricing = candidate.repricing as { new_total?: unknown; adjustment?: unknown } | null
  return (
    typeof repricing === 'object' &&
    repricing !== null &&
    typeof repricing.new_total === 'string' &&
    typeof repricing.adjustment === 'string'
  )
}
