import { useCallback, useEffect, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { bookingService } from '@/services/bookings/bookingService'
import type { Page } from '@/types/api'
import type { Booking } from '@/types/booking'

/**
 * One page of a hotel's bookings.
 *
 * **One request per (hotel, page, page size), and no more.** Each item the list endpoint
 * returns is already a complete booking -- allocations and nightly rates included -- so
 * nothing is fetched per row. That is the N+1 this hook exists to not have: a "fetch the
 * guest for each booking" loop would be twenty extra requests for a default page, and it is
 * the obvious thing to reach for, because `BookingResponse` carries only `guest_public_id`.
 *
 * A superseded request is aborted rather than left to race. Paging quickly through a list is
 * exactly how a stale response arrives after a fresh one and overwrites it.
 */

export type BookingListStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface BookingListState {
  readonly status: BookingListStatus
  readonly page: Page<Booking> | null
  /** The failure, when `status` is `error`. Never rendered raw. */
  readonly error: ApiError | null
  readonly retry: () => void
}

export function useBookingList(
  hotelPublicId: string | null,
  page: number,
  pageSize: number,
): BookingListState {
  const [status, setStatus] = useState<BookingListStatus>('idle')
  const [result, setResult] = useState<Page<Booking> | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (hotelPublicId === null) {
      setStatus('idle')
      setResult(null)
      setError(null)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    bookingService
      .listForHotel(hotelPublicId, { page, pageSize }, controller.signal)
      .then((fetched) => {
        if (cancelled) {
          return
        }
        /*
         * A 200 is not proof of the documented shape.
         *
         * The API client does no runtime validation -- the response type is the caller's
         * claim -- so a proxy or a moved endpoint can hand back a 200 whose body has no
         * `items`. Reaching for `.items.map` on that takes the page down from a render, past
         * this `catch`, which is how Stage 5.4's hotel provider fell over. The check is here
         * so a malformed success is treated as the failure it is, and never as "no bookings".
         */
        if (!Array.isArray((fetched as { items?: unknown }).items)) {
          setResult(null)
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The bookings response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setResult(fetched)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          // An abort is this component tidying up after itself, not a failure to report.
          // Letting it set `error` would flash a failure on every page change.
          return
        }
        setResult(null)
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The bookings could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, page, pageSize, attempt])

  const retry = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  return { status, page: result, error, retry }
}
