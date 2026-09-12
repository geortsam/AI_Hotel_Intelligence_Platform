import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { availabilityService, type AvailabilitySearch } from '@/services/availability/availabilityService'
import { roomService } from '@/services/rooms/roomService'
import type { AvailabilityResult } from '@/types/availability'
import type { RoomType } from '@/types/room'

/**
 * One hotel's room-type catalogue, and the availability searches run against it.
 *
 * ## This hook computes no availability, and that is the whole point
 *
 * It holds a request and the answer to it. There is no date arithmetic beyond counting
 * nights for a local bound check, no comparison of counts, no overlap logic and no
 * inspection of bookings. `sufficient`, `available_rooms` and every `available_count` arrive
 * computed by a query that mirrors the database's exclusion constraint, and are stored
 * exactly as they arrive.
 *
 * In particular it does **not** derive the verdict from the result list. A mixed request for
 * two Deluxe and ninety-nine Standard came back with both types listed, sixteen rooms free
 * and `sufficient: false` -- so "we got some rooms back" and "the request can be met" are
 * different facts, and only the server knows the second.
 *
 * ## Nothing is searched until someone asks
 *
 * The catalogue loads on mount, because the search form needs it. **The search itself runs
 * only on submit**: an availability question has no sensible default answer, and firing one
 * on arrival would put a result on screen for dates nobody chose.
 *
 * ## One catalogue request, one request per search
 *
 * The room types are fetched once and reused for every search and every result row -- the
 * result already names each type with its own `name`, occupancy and price, so nothing is
 * resolved per result either. There is no request per room, per type or per result.
 *
 * ## A search supersedes the one before it
 *
 * Each submit aborts the request in flight. Without that, a slow first search could land
 * after a fast second and show the wrong dates' answer under the new ones.
 */

export type AvailabilityStatus = 'idle' | 'searching' | 'ready' | 'error'

export interface AvailabilityState {
  /** The hotel's room types, for the form and for naming what comes back. */
  readonly types: readonly RoomType[]
  readonly typesError: ApiError | null

  readonly status: AvailabilityStatus
  /** The server's answer to the last search, or null before one has been run. */
  readonly result: AvailabilityResult | null
  /** The search that produced `result`, so the screen can restate what was asked. */
  readonly lastSearch: AvailabilitySearch | null
  readonly error: ApiError | null

  readonly search: (request: AvailabilitySearch) => Promise<void>
  readonly reset: () => void
}

export function useAvailability(hotelPublicId: string | null): AvailabilityState {
  const [types, setTypes] = useState<readonly RoomType[]>([])
  const [typesError, setTypesError] = useState<ApiError | null>(null)

  const [status, setStatus] = useState<AvailabilityStatus>('idle')
  const [result, setResult] = useState<AvailabilityResult | null>(null)
  const [lastSearch, setLastSearch] = useState<AvailabilitySearch | null>(null)
  const [error, setError] = useState<ApiError | null>(null)

  const inFlight = useRef<AbortController | null>(null)

  /* --- the catalogue, once ---------------------------------------------------------------- */

  useEffect(() => {
    if (hotelPublicId === null) {
      setTypes([])
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setTypesError(null)

    roomService
      .listTypes(hotelPublicId, controller.signal)
      .then((page) => {
        if (cancelled) {
          return
        }
        if (!Array.isArray((page as { items?: unknown }).items)) {
          setTypes([])
          setTypesError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The room type response was not in the expected format.',
            ),
          )
          return
        }
        setTypes(page.items)
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setTypes([])
        setTypesError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The room types could not be loaded.'),
        )
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId])

  /* --- clear everything when the property changes ------------------------------------------ */

  useEffect(() => {
    // An answer is about one property's inventory on one set of dates. Carrying it across a
    // hotel switch would show the wrong hotel's rooms under the new hotel's name.
    inFlight.current?.abort()
    inFlight.current = null
    setStatus('idle')
    setResult(null)
    setLastSearch(null)
    setError(null)
  }, [hotelPublicId])

  useEffect(() => () => inFlight.current?.abort(), [])

  const search = useCallback(
    async (request: AvailabilitySearch): Promise<void> => {
      if (hotelPublicId === null) {
        return
      }
      // The previous search is abandoned rather than raced: see the module docstring.
      inFlight.current?.abort()
      const controller = new AbortController()
      inFlight.current = controller

      setStatus('searching')
      setError(null)

      try {
        const answer = await availabilityService.search(hotelPublicId, request, controller.signal)
        if (controller.signal.aborted) {
          return
        }
        /*
         * A 200 is not proof of the documented shape. `room_types.map` on something else
         * would take the page down from a render, past this `catch` -- and an unreadable
         * answer must never be mistaken for "nothing is free", which is the one wrong
         * conclusion this screen could draw.
         */
        if (!isAvailabilityResult(answer)) {
          setResult(null)
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The availability response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setResult(answer)
        setLastSearch(request)
        setStatus('ready')
      } catch (cause: unknown) {
        if (controller.signal.aborted) {
          return
        }
        setResult(null)
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The search could not be completed.'),
        )
        setStatus('error')
      } finally {
        if (inFlight.current === controller) {
          inFlight.current = null
        }
      }
    },
    [hotelPublicId],
  )

  const reset = useCallback(() => {
    inFlight.current?.abort()
    inFlight.current = null
    setStatus('idle')
    setResult(null)
    setLastSearch(null)
    setError(null)
  }, [])

  return { types, typesError, status, result, lastSearch, error, search, reset }
}

/**
 * Whether a 200 body is actually the documented result.
 *
 * `sufficient` is checked for being a **boolean** specifically: it is the field the whole
 * screen turns on, and a missing one arriving as `undefined` would read as "not sufficient"
 * and quietly report that a hotel with rooms free has none.
 */
export function isAvailabilityResult(value: unknown): value is AvailabilityResult {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as {
    sufficient?: unknown
    available_rooms?: unknown
    nights?: unknown
    room_types?: unknown
  }
  return (
    typeof candidate.sufficient === 'boolean' &&
    typeof candidate.available_rooms === 'number' &&
    typeof candidate.nights === 'number' &&
    Array.isArray(candidate.room_types)
  )
}
