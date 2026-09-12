import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { DEFAULT_PAGE_SIZE, roomService } from '@/services/rooms/roomService'
import type { Room, RoomCreateRequest, RoomType } from '@/types/room'

/**
 * A hotel's room types, one type's rooms, and the one action the list offers.
 *
 * ## Why the type comes first
 *
 * There is no hotel-wide room collection: the only list endpoint hangs off a room type. So
 * the catalogue is fetched once, a type is selected, and that type's rooms are paged.
 *
 * The alternative -- fetching every type's rooms to present one flat list -- is a request per
 * type that grows with the property, and it is the thing the brief rules out. It would also
 * be dishonest about paging: each type pages independently, so a merged list could not
 * honour a single page number.
 *
 * ## Two requests, and nothing per room
 *
 * The catalogue once, and one page of rooms per view. A room row arrives complete -- number,
 * type code, floor, status, notes, service state and timestamps are all fields of
 * `RoomResponse` -- so **nothing is fetched per room**. In particular the room type is
 * already named on every row: resolving it per room would be the N+1 the brief rules out,
 * and the response makes it unnecessary.
 *
 * ## Creation re-reads rather than prepends
 *
 * The list is ordered by room number, server-side; where a new room belongs in it is the
 * server's to decide, as is whether it lands on this page. A **failed** create adds nothing,
 * and nothing is retried: a repeat that the first attempt actually reached would be refused
 * as a duplicate number, which is a 409 rather than a second room -- but only because the
 * number is unique, and that is the server's guarantee to make, not this hook's to assume.
 */

export type RoomListStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface RoomListState {
  /** The hotel's room types. Empty until loaded, or when the catalogue failed. */
  readonly types: readonly RoomType[]
  readonly typesError: ApiError | null
  /** The selected type's code, or `''` before the catalogue has arrived. */
  readonly selectedType: string
  readonly setSelectedType: (code: string) => void

  readonly status: RoomListStatus
  readonly rooms: readonly Room[]
  /** The server's count for this type across every page. Never derived from the rows held. */
  readonly total: number
  readonly pages: number
  readonly page: number
  readonly pageSize: number
  readonly error: ApiError | null

  readonly creating: boolean
  readonly created: Room | null
  readonly createError: ApiError | null

  readonly setPage: (page: number) => void
  readonly setPageSize: (pageSize: number) => void
  readonly reload: () => void
  readonly create: (payload: RoomCreateRequest) => Promise<boolean>
  readonly dismiss: () => void
}

export function useRoomList(hotelPublicId: string | null): RoomListState {
  const [types, setTypes] = useState<readonly RoomType[]>([])
  const [typesError, setTypesError] = useState<ApiError | null>(null)
  const [selectedType, setSelectedTypeState] = useState('')

  const [status, setStatus] = useState<RoomListStatus>('idle')
  const [rooms, setRooms] = useState<readonly Room[]>([])
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(DEFAULT_PAGE_SIZE)
  const [error, setError] = useState<ApiError | null>(null)
  const [attempt, setAttempt] = useState(0)

  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<Room | null>(null)
  const [createError, setCreateError] = useState<ApiError | null>(null)

  const inFlight = useRef(false)

  /* --- the catalogue -------------------------------------------------------------------- */

  useEffect(() => {
    if (hotelPublicId === null) {
      setTypes([])
      setSelectedTypeState('')
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setTypesError(null)

    roomService
      .listTypes(hotelPublicId, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        if (!Array.isArray((result as { items?: unknown }).items)) {
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
        setTypes(result.items)
        // Select the first type so the screen has something to show. The order is the
        // server's; nothing is sorted here.
        setSelectedTypeState((current) =>
          current !== '' && result.items.some((t) => t.code === current)
            ? current
            : (result.items[0]?.code ?? ''),
        )
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
  }, [hotelPublicId, attempt])

  /* --- the selected type's rooms --------------------------------------------------------- */

  useEffect(() => {
    if (hotelPublicId === null || selectedType === '') {
      setStatus('idle')
      setRooms([])
      setTotal(0)
      setPages(0)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    roomService
      .list(hotelPublicId, selectedType, { page, pageSize }, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        /*
         * A 200 is not proof of the documented shape. `rooms.map` on something else would
         * take the page down from a render, past this `catch`. An unreadable answer is a
         * failure, never a room type with no rooms.
         */
        if (!Array.isArray((result as { items?: unknown }).items)) {
          setRooms([])
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The room list response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setRooms(result.items)
        setTotal(result.total)
        setPages(result.pages)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setRooms([])
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The rooms could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, selectedType, page, pageSize, attempt])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const setSelectedType = useCallback((code: string) => {
    setSelectedTypeState(code)
    // A different type is a different collection with its own page count; page 3 of one is
    // not page 3 of another, and the backend answers a too-high page with an empty 200.
    setPage(1)
  }, [])

  const setPageSize = useCallback((next: number) => {
    setPageSizeState(next)
    setPage(1)
  }, [])

  const create = useCallback(
    async (payload: RoomCreateRequest): Promise<boolean> => {
      if (inFlight.current || hotelPublicId === null || selectedType === '') {
        return false
      }
      inFlight.current = true
      setCreating(true)
      setCreateError(null)
      setCreated(null)

      try {
        const room = await roomService.create(hotelPublicId, selectedType, payload)
        if (!isRoom(room)) {
          throw new ApiError(
            201,
            ApiError.MALFORMED_CODE,
            'The response to the new room was not in the expected format.',
          )
        }
        setCreated(room)
        setAttempt((n) => n + 1)
        return true
      } catch (cause: unknown) {
        setCreateError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The room could not be created.'),
        )
        return false
      } finally {
        inFlight.current = false
        setCreating(false)
      }
    },
    [hotelPublicId, selectedType],
  )

  const dismiss = useCallback(() => {
    setCreated(null)
    setCreateError(null)
  }, [])

  return {
    types,
    typesError,
    selectedType,
    setSelectedType,
    status,
    rooms,
    total,
    pages,
    page,
    pageSize,
    error,
    creating,
    created,
    createError,
    setPage,
    setPageSize,
    reload,
    create,
    dismiss,
  }
}

/** Whether a body is actually the documented room shape. */
export function isRoom(value: unknown): value is Room {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as {
    room_number?: unknown
    room_type_code?: unknown
    status?: unknown
  }
  return (
    typeof candidate.room_number === 'string' &&
    typeof candidate.room_type_code === 'string' &&
    typeof candidate.status === 'string'
  )
}
