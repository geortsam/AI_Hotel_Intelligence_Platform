import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { hotelService } from '@/services/hotels/hotelService'
import { DEFAULT_PAGE_SIZE, roomTypeService } from '@/services/roomTypes/roomTypeService'
import type { Hotel, HotelUpdateRequest } from '@/types/hotel'
import type { RoomType } from '@/types/room'
import type { RoomTypeCreateRequest, RoomTypeUpdateRequest } from '@/types/roomType'

/**
 * One property's own record, and the room types belonging to it.
 *
 * ## Why both live in one hook
 *
 * They are one screen's worth of state and they are genuinely coupled: a room type is
 * addressed through its hotel, and the hotel's `currency` is the one a new room type's price
 * is quoted in. Splitting them would mean two hooks that have to agree about which hotel is
 * selected.
 *
 * ## Nothing is derived
 *
 * Prices, occupancies and counts are shown as the server sent them. No availability is
 * calculated, no occupancy is inferred, no financial figure is computed -- this screen
 * manages definitions, and every number on it is a stored column.
 *
 * ## Two requests per view, and nothing per row
 *
 * The hotel record and one page of its room types. A room type row arrives complete, so
 * nothing is fetched per row.
 *
 * ## Mutations adopt the server's answer
 *
 * A successful hotel `PATCH` stores the response rather than the payload -- `country_code`
 * and `currency` are upper-cased server-side, and `updated_at` moves. A room-type create or
 * delete re-reads the list, because where a new code sorts and whether it lands on this page
 * are the server's to decide. A failure changes nothing, and nothing is retried.
 *
 * `inFlight` is a ref rather than state: two clicks in the same tick both read the same
 * stale `false` from a state variable.
 */

export type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

/** Which write failed, so the page can say the right thing about it. */
export type FailedWrite = 'hotel-update' | 'type-create' | 'type-update' | 'type-delete'

export interface PropertyAdminState {
  /** The hotel's own record, re-read from `GET /hotels/{id}` rather than reused from context. */
  readonly hotel: Hotel | null
  readonly hotelStatus: LoadStatus
  readonly hotelError: ApiError | null

  readonly types: readonly RoomType[]
  readonly typesStatus: LoadStatus
  readonly typesError: ApiError | null
  readonly total: number
  readonly pages: number
  readonly page: number
  readonly pageSize: number

  /** Which mutation is in flight, or null. */
  readonly pending: FailedWrite | null
  /** Copy written here, never the backend's message. */
  readonly saved: string | null
  readonly writeError: ApiError | null
  readonly failedWrite: FailedWrite | null

  readonly setPage: (page: number) => void
  readonly setPageSize: (pageSize: number) => void
  readonly reload: () => void
  readonly updateHotel: (payload: HotelUpdateRequest) => Promise<boolean>
  readonly createType: (payload: RoomTypeCreateRequest) => Promise<boolean>
  readonly updateType: (code: string, payload: RoomTypeUpdateRequest) => Promise<boolean>
  readonly deleteType: (code: string) => Promise<boolean>
  readonly dismiss: () => void
}

export function usePropertyAdmin(hotelPublicId: string | null): PropertyAdminState {
  const [hotel, setHotel] = useState<Hotel | null>(null)
  const [hotelStatus, setHotelStatus] = useState<LoadStatus>('idle')
  const [hotelError, setHotelError] = useState<ApiError | null>(null)

  const [types, setTypes] = useState<readonly RoomType[]>([])
  const [typesStatus, setTypesStatus] = useState<LoadStatus>('idle')
  const [typesError, setTypesError] = useState<ApiError | null>(null)
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(DEFAULT_PAGE_SIZE)
  const [attempt, setAttempt] = useState(0)

  const [pending, setPending] = useState<FailedWrite | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const [writeError, setWriteError] = useState<ApiError | null>(null)
  const [failedWrite, setFailedWrite] = useState<FailedWrite | null>(null)

  const inFlight = useRef(false)

  /* --- the hotel's own record -------------------------------------------------------------- */

  useEffect(() => {
    if (hotelPublicId === null) {
      setHotelStatus('idle')
      setHotel(null)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setHotelStatus('loading')
    setHotelError(null)

    hotelService
      .get(hotelPublicId, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        if (!isHotel(result)) {
          setHotel(null)
          setHotelError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The hotel response was not in the expected format.',
            ),
          )
          setHotelStatus('error')
          return
        }
        setHotel(result)
        setHotelStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setHotel(null)
        setHotelError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The property could not be loaded.'),
        )
        setHotelStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, attempt])

  /* --- its room types ----------------------------------------------------------------------- */

  useEffect(() => {
    if (hotelPublicId === null) {
      setTypesStatus('idle')
      setTypes([])
      setTotal(0)
      setPages(0)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setTypesStatus('loading')
    setTypesError(null)

    roomTypeService
      .list(hotelPublicId, { page, pageSize }, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        /*
         * A 200 is not proof of the documented shape. `types.map` on something else would
         * take the page down from a render, past this `catch`. An unreadable answer is a
         * failure, never a property with no room types.
         */
        if (!Array.isArray((result as { items?: unknown }).items)) {
          setTypes([])
          setTypesError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The room type response was not in the expected format.',
            ),
          )
          setTypesStatus('error')
          return
        }
        setTypes(result.items)
        setTotal(result.total)
        setPages(result.pages)
        setTypesStatus('ready')
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
        setTypesStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, page, pageSize, attempt])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const setPageSize = useCallback((next: number) => {
    setPageSizeState(next)
    setPage(1)
  }, [])

  /** Runs one write, keeping the in-flight guard and the failure bookkeeping in one place. */
  const run = useCallback(
    async (kind: FailedWrite, call: () => Promise<void>, message: string): Promise<boolean> => {
      if (inFlight.current || hotelPublicId === null) {
        return false
      }
      inFlight.current = true
      setPending(kind)
      setWriteError(null)
      setFailedWrite(null)
      setSaved(null)

      try {
        await call()
        setSaved(message)
        return true
      } catch (cause: unknown) {
        setFailedWrite(kind)
        setWriteError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The change could not be saved.'),
        )
        return false
      } finally {
        inFlight.current = false
        setPending(null)
      }
    },
    [hotelPublicId],
  )

  const updateHotel = useCallback(
    (payload: HotelUpdateRequest) =>
      run(
        'hotel-update',
        async () => {
          const result = await hotelService.update(hotelPublicId!, payload)
          if (!isHotel(result)) {
            throw new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The response to the update was not in the expected format.',
            )
          }
          // The server's row, not the payload: codes are canonicalised and `updated_at` moves.
          setHotel(result)
        },
        'Property updated.',
      ),
    [run, hotelPublicId],
  )

  const createType = useCallback(
    (payload: RoomTypeCreateRequest) =>
      run(
        'type-create',
        async () => {
          await roomTypeService.create(hotelPublicId!, payload)
          // Re-read: the list is ordered by code, and where a new one sorts is the server's
          // to decide -- as is whether it lands on this page at all.
          setAttempt((n) => n + 1)
        },
        'Room type created.',
      ),
    [run, hotelPublicId],
  )

  const updateType = useCallback(
    (code: string, payload: RoomTypeUpdateRequest) =>
      run(
        'type-update',
        async () => {
          await roomTypeService.update(hotelPublicId!, code, payload)
          setAttempt((n) => n + 1)
        },
        'Room type updated.',
      ),
    [run, hotelPublicId],
  )

  const deleteType = useCallback(
    (code: string) =>
      run(
        'type-delete',
        async () => {
          await roomTypeService.remove(hotelPublicId!, code)
          setAttempt((n) => n + 1)
        },
        'Room type deleted.',
      ),
    [run, hotelPublicId],
  )

  const dismiss = useCallback(() => {
    setSaved(null)
    setWriteError(null)
    setFailedWrite(null)
  }, [])

  return {
    hotel,
    hotelStatus,
    hotelError,
    types,
    typesStatus,
    typesError,
    total,
    pages,
    page,
    pageSize,
    pending,
    saved,
    writeError,
    failedWrite,
    setPage,
    setPageSize,
    reload,
    updateHotel,
    createType,
    updateType,
    deleteType,
    dismiss,
  }
}

/** Whether a body is actually the documented hotel shape. */
function isHotel(value: unknown): value is Hotel {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as { public_id?: unknown; slug?: unknown; name?: unknown }
  return (
    typeof candidate.public_id === 'string' &&
    typeof candidate.slug === 'string' &&
    typeof candidate.name === 'string'
  )
}
