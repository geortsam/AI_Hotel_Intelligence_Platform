import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { DEFAULT_PAGE_SIZE, guestService } from '@/services/guests/guestService'
import type { Guest, GuestCreateRequest } from '@/types/guest'

/**
 * One page of a hotel's guests, and the one action the list itself offers.
 *
 * ## The page is the whole query surface
 *
 * `GET /hotels/{h}/guests` accepts `page` and `page_size` and nothing else -- no search, no
 * filter, no sort. So this hook holds a page number and a size, and there is nothing else for
 * it to hold. It deliberately does **not** fetch every page to filter the union: with a
 * hundred rows per request that is a request per keystroke growing without bound, and it
 * moves a decision the database is built to make into a browser.
 *
 * Nor does it narrow the rows it already has. A guest list is read to answer "is this person
 * already on file" -- and a search box that quietly covered only the twenty rows on screen
 * would answer "no" for someone sitting on page 3. That is the failure mode that matters
 * here, so the capability is absent rather than approximated.
 *
 * ## One request per view, and nothing per row
 *
 * A guest row arrives complete: name, contact details, country, language, consent and
 * timestamps are all fields of `GuestResponse`. **Nothing is fetched per guest** -- no detail
 * request to fill in a column, which would be the N+1 the brief rules out.
 *
 * ## Creation re-reads rather than prepends
 *
 * The list is ordered by surname then forename, server-side. Where a new guest belongs in
 * that order is the server's to decide, as is whether they land on this page at all -- so a
 * successful create re-reads instead of splicing a row in at the top. A **failed** create
 * adds nothing, and nothing is retried automatically: a repeat that the first attempt
 * actually reached would be refused as a duplicate email only if an email was given, and
 * would otherwise create a second guest.
 *
 * `inFlight` is a ref rather than state because two clicks in the same tick both read the
 * same stale `false` from a state variable, and both fire.
 */

export type GuestListStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface GuestListState {
  readonly status: GuestListStatus
  readonly guests: readonly Guest[]
  /** The server's count across every page. Never derived from the rows held. */
  readonly total: number
  readonly pages: number
  readonly page: number
  readonly pageSize: number
  readonly error: ApiError | null

  readonly creating: boolean
  /** The guest the server created, for the confirmation. */
  readonly created: Guest | null
  /** The last refusal. Rendered through `describeFailure`, never raw. */
  readonly createError: ApiError | null

  readonly setPage: (page: number) => void
  readonly setPageSize: (pageSize: number) => void
  readonly reload: () => void
  readonly create: (payload: GuestCreateRequest) => Promise<boolean>
  readonly dismiss: () => void
}

export function useGuestList(hotelPublicId: string | null): GuestListState {
  const [status, setStatus] = useState<GuestListStatus>('idle')
  const [guests, setGuests] = useState<readonly Guest[]>([])
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(DEFAULT_PAGE_SIZE)
  const [error, setError] = useState<ApiError | null>(null)
  const [attempt, setAttempt] = useState(0)

  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<Guest | null>(null)
  const [createError, setCreateError] = useState<ApiError | null>(null)

  const inFlight = useRef(false)

  useEffect(() => {
    if (hotelPublicId === null) {
      setStatus('idle')
      setGuests([])
      setTotal(0)
      setPages(0)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    guestService
      .list(hotelPublicId, { page, pageSize }, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        /*
         * A 200 is not proof of the documented shape. The client validates nothing at
         * runtime, so a proxy or a moved route can answer 200 with something else -- and
         * `guests.map` on that would take the page down from a render, past this `catch`.
         * An unreadable answer is a failure, never a hotel with no guests.
         */
        if (!Array.isArray((result as { items?: unknown }).items)) {
          setGuests([])
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The guest list response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setGuests(result.items)
        setTotal(result.total)
        setPages(result.pages)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setGuests([])
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The guests could not be loaded.'),
        )
        setStatus('error')
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
    // Page 4 of a 20-row view is past the end of a 100-row one, and the backend answers a
    // too-high page with an empty 200 rather than an error -- which would read as "no guests".
    setPage(1)
  }, [])

  const create = useCallback(
    async (payload: GuestCreateRequest): Promise<boolean> => {
      if (inFlight.current || hotelPublicId === null) {
        return false
      }
      inFlight.current = true
      setCreating(true)
      setCreateError(null)
      setCreated(null)

      try {
        const guest = await guestService.create(hotelPublicId, payload)
        if (!isGuest(guest)) {
          throw new ApiError(
            201,
            ApiError.MALFORMED_CODE,
            'The response to the new guest was not in the expected format.',
          )
        }
        setCreated(guest)
        setAttempt((n) => n + 1)
        return true
      } catch (cause: unknown) {
        setCreateError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The guest could not be created.'),
        )
        return false
      } finally {
        inFlight.current = false
        setCreating(false)
      }
    },
    [hotelPublicId],
  )

  const dismiss = useCallback(() => {
    setCreated(null)
    setCreateError(null)
  }, [])

  return {
    status,
    guests,
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

/** Whether a 201 body is actually the documented guest shape. */
export function isGuest(value: unknown): value is Guest {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as {
    public_id?: unknown
    first_name?: unknown
    last_name?: unknown
  }
  return (
    typeof candidate.public_id === 'string' &&
    typeof candidate.first_name === 'string' &&
    typeof candidate.last_name === 'string'
  )
}
