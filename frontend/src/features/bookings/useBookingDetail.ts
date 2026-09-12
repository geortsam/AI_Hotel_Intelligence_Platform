import { useCallback, useEffect, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { bookingService } from '@/services/bookings/bookingService'
import { guestService } from '@/services/guests/guestService'
import type { Booking, BookingReconciliation } from '@/types/booking'
import type { Guest } from '@/types/guest'

/**
 * Everything one booking's page shows.
 *
 * ## Three requests, and why each is separate
 *
 * 1. `bookings/{id}` -- the booking, its allocations and every nightly rate.
 * 2. `bookings/{id}/reconciliation` -- the **authoritative** money. `booking.total_amount` is
 *    the contracted figure and is explicitly not defined as the sum of the nightly rates, so
 *    the only honest source for "what is this stay worth" is this endpoint.
 * 3. `guests/{guest_public_id}` -- the booker. `BookingResponse` carries only the id.
 *
 * Fixed at three regardless of how many rooms or nights the booking has, which is what makes
 * it a detail fetch rather than an N+1. The first two are issued **concurrently**; the guest
 * request cannot be, because its id is inside the booking. That is a genuine dependency, not
 * a waterfall by accident, and it is the only one.
 *
 * ## Two of the three are allowed to fail on their own
 *
 * The booking is the page. If it fails, the page reports a failure. The reconciliation and
 * the guest are sections *of* that page: if either fails the rest still renders, and the
 * section says it is unavailable rather than the whole screen going blank. A financial
 * summary that could not be fetched must never be mistaken for one that came back zero.
 */

export type BookingDetailStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface BookingDetailData {
  readonly booking: Booking
  /** Null when the reconciliation request failed on its own. Never a fabricated zero. */
  readonly reconciliation: BookingReconciliation | null
  /**
   * Why the financial summary is missing, when it is.
   *
   * Kept because one of its failures is meaningful rather than transient: a booking with
   * payments in more than one currency answers **409**, because the service refuses to add
   * numbers that do not add and performs no conversion. That is a state to explain, not a
   * fault to retry, and it cannot be told from a network blip without the status.
   */
  readonly reconciliationError: ApiError | null
  /** Null when the guest request failed, or when the record has since gone. */
  readonly guest: Guest | null
}

export interface BookingDetailState {
  readonly status: BookingDetailStatus
  readonly data: BookingDetailData | null
  readonly error: ApiError | null
  readonly retry: () => void
  /**
   * Adopt a booking the SERVER returned from a mutation.
   *
   * All three mutation endpoints answer with the booking as it now stands, so there is
   * nothing to guess and nothing to re-read. This exists so the page can take that object
   * verbatim -- the alternative being to patch the local copy with what the UI *asked* for,
   * which is how a screen ends up showing a status the server never applied.
   */
  readonly applyBooking: (booking: Booking) => void
  /**
   * Re-read the financial summary.
   *
   * Needed after a stay change and only then: `accommodation_total` is the sum of the
   * nightly rates, so restating or extending a stay invalidates it. A status change does
   * not touch a single night row, and this application never sends `total_amount`, so
   * neither total can move -- calling this after one would be a request that cannot return
   * anything different.
   */
  readonly refreshReconciliation: () => Promise<void>
  /**
   * Re-read the booking AND its financial summary from the server.
   *
   * Needed because one endpoint's response cannot be adopted. **`PATCH .../stay` returns
   * `booking.rooms: []`** -- measured against the live API, not assumed: immediately after a
   * successful modification the response carried an empty allocation list while a GET of the
   * same booking a moment later returned the room with its three priced nights.
   * `POST .../stay/extension` does not have the problem and returns its rooms.
   *
   * Adopting that response would put "Rooms (0)" on a booking that has one, which is a
   * false statement about the booking rather than merely a missing panel. So the stay
   * editor's success path re-reads instead. The repricing figures from the response are
   * still used -- those are complete and authoritative.
   */
  readonly refreshBooking: () => Promise<void>
}

export function useBookingDetail(
  hotelPublicId: string | null,
  bookingPublicId: string | undefined,
): BookingDetailState {
  const [status, setStatus] = useState<BookingDetailStatus>('idle')
  const [data, setData] = useState<BookingDetailData | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (hotelPublicId === null || bookingPublicId === undefined) {
      setStatus('idle')
      setData(null)
      setError(null)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    const booking = bookingService.get(hotelPublicId, bookingPublicId, controller.signal)
    const reconciliation = bookingService
      .getReconciliation(hotelPublicId, bookingPublicId, controller.signal)
      .then((value) => ({ value, error: null as ApiError | null }))
      .catch((cause: unknown) => ({
        value: null,
        error: cause instanceof ApiError ? cause : null,
      }))

    Promise.all([booking, reconciliation])
      .then(async ([fetched, money]) => {
        if (cancelled) {
          return
        }
        // Same reasoning as the list: a 200 is not proof of the documented shape, and
        // `rooms.map` on a malformed body would take the page down from a render.
        if (!Array.isArray((fetched as { rooms?: unknown }).rooms)) {
          setData(null)
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The booking response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }

        const guest = await guestService
          .get(hotelPublicId, fetched.guest_public_id, controller.signal)
          .catch(() => null)
        if (cancelled) {
          return
        }
        setData({
          booking: fetched,
          reconciliation: money.value,
          reconciliationError: money.error,
          guest,
        })
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setData(null)
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The booking could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, bookingPublicId, attempt])

  const retry = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const applyBooking = useCallback((booking: Booking) => {
    setData((current) => (current === null ? current : { ...current, booking }))
  }, [])

  const refreshReconciliation = useCallback(async () => {
    if (hotelPublicId === null || bookingPublicId === undefined) {
      return
    }
    // A failure here leaves the section reading "could not be loaded" rather than showing a
    // figure from before the change, which would be worse than showing none: it would be a
    // stale number presented as a current one.
    const money = await bookingService
      .getReconciliation(hotelPublicId, bookingPublicId)
      .then((value) => ({ value, error: null as ApiError | null }))
      .catch((cause: unknown) => ({
        value: null,
        error: cause instanceof ApiError ? cause : null,
      }))
    setData((current) =>
      current === null
        ? current
        : { ...current, reconciliation: money.value, reconciliationError: money.error },
    )
  }, [hotelPublicId, bookingPublicId])

  const refreshBooking = useCallback(async () => {
    if (hotelPublicId === null || bookingPublicId === undefined) {
      return
    }
    const [fetched, money] = await Promise.all([
      bookingService.get(hotelPublicId, bookingPublicId).catch(() => null),
      bookingService
        .getReconciliation(hotelPublicId, bookingPublicId)
        .then((value) => ({ value, error: null as ApiError | null }))
        .catch((cause: unknown) => ({
          value: null,
          error: cause instanceof ApiError ? cause : null,
        })),
    ])
    setData((current) => {
      if (current === null) {
        return current
      }
      // A failed re-read leaves the previous booking in place rather than blanking the page:
      // the mutation itself succeeded, and the user needs to see what it did.
      return {
        ...current,
        ...(fetched !== null && Array.isArray(fetched.rooms) ? { booking: fetched } : {}),
        reconciliation: money.value,
        reconciliationError: money.error,
      }
    })
  }, [hotelPublicId, bookingPublicId])

  return {
    status,
    data,
    error,
    retry,
    applyBooking,
    refreshReconciliation,
    refreshBooking,
  }
}
