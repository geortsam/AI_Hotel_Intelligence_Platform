import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { hotelService } from '@/services/hotels/hotelService'
import { useAuth } from '@/session/AuthProvider'
import type { Hotel } from '@/types/hotel'

/**
 * Which hotel the application is looking at.
 *
 * ## Why this exists at all
 *
 * Every analytics route is `/hotels/{hotel_public_id}/analytics/...`. There is no
 * portfolio-wide route, by design -- the hotel segment is where the backend establishes
 * tenant isolation. So a dashboard cannot be built without first answering "which hotel?",
 * and that answer has to come from the server.
 *
 * ## Where the answer comes from
 *
 * `GET /hotels`, which the backend filters to the caller's own memberships. This is not a
 * selector invented on the client: the list is a membership query, the frontend cannot
 * widen it, and picking an entry grants nothing. Authorization is re-decided on every
 * hotel-scoped request from `user_hotels`, so a tampered selection produces a 404, not
 * access. Stage 5.2's static "No hotel selected" chip is replaced by this because the
 * mechanism turned out to exist; what does **not** exist is any way to name a hotel the
 * server did not already return.
 *
 * ## What is deliberately not here
 *
 * No role, and no permission. `GET /auth/me` returns identity only -- "who the caller is,
 * not what they may access" -- and `GET /hotels` returns hotel records, not the caller's
 * role at each. So this context can say *which* properties are reachable and nothing about
 * what may be done inside one. Deriving a permission from it would be inventing
 * authorization the API never granted.
 */

export type HotelContextStatus = 'idle' | 'loading' | 'ready' | 'empty' | 'error'

export interface HotelContextValue {
  readonly status: HotelContextStatus
  /** The hotels this user is a member of. Empty unless `status` is `ready`. */
  readonly hotels: readonly Hotel[]
  /** The hotel being viewed, or null in every non-ready state. */
  readonly selected: Hotel | null
  /**
   * True when the account holds more memberships than one page returned.
   *
   * Surfaced rather than hidden: a selector that silently shows 25 of 40 properties is worse
   * than one that admits it is partial.
   */
  readonly hasMore: boolean
  /** Choose a hotel. Ignores an id the server did not return. */
  readonly select: (publicId: string) => void
  /** Re-run the membership query after a failure. */
  readonly retry: () => void
}

const HotelContext = createContext<HotelContextValue | null>(null)

/** Remembers the choice for this tab only. Same reasoning as the token: see `tokenStorage`. */
const SELECTION_KEY = 'ahip.selected_hotel'

function readSelection(): string | null {
  try {
    return window.sessionStorage.getItem(SELECTION_KEY)
  } catch {
    // Storage can throw outright, not merely return null -- a browser set to block site
    // data does exactly that. A remembered selection is a convenience; losing it must not
    // take the dashboard down with it.
    return null
  }
}

function writeSelection(publicId: string): void {
  try {
    window.sessionStorage.setItem(SELECTION_KEY, publicId)
  } catch {
    /* See readSelection. */
  }
}

/**
 * Whether a 200 response is actually the `Page<Hotel>` it was typed as.
 *
 * The API client deliberately performs no runtime validation -- its own docstring says the
 * response type is "the caller's claim" -- which is the ordinary trade-off for a typed
 * client. The consequence is that a 200 carrying the wrong shape arrives here as a value
 * TypeScript believes in and the runtime does not.
 *
 * That is not hypothetical. A response of the right status and the wrong body is exactly
 * what a misconfigured proxy, a captive portal, or an endpoint that has moved will produce,
 * and reaching for `.items` on it takes down the whole application from a `useMemo` -- past
 * the effect's own `catch`, which never sees it. So the shape is checked once, here, at the
 * point the untyped value enters typed code, and anything else is treated as the failure it
 * is rather than as an empty list. "No hotels" and "unreadable answer" must not look alike.
 */
function isHotelPage(value: unknown): value is { items: Hotel[]; total: number } {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const page = value as { items?: unknown; total?: unknown }
  return Array.isArray(page.items) && typeof page.total === 'number'
}

export function HotelProvider({ children }: { readonly children: ReactNode }) {
  const { status: authStatus } = useAuth()
  const [status, setStatus] = useState<HotelContextStatus>('idle')
  const [hotels, setHotels] = useState<readonly Hotel[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  const isAuthenticated = authStatus === 'authenticated'

  useEffect(() => {
    if (!isAuthenticated) {
      // Signing out must drop the list as well as the token. Keeping it would leave one
      // user's property names on screen while the next person signs in.
      setStatus('idle')
      setHotels([])
      setSelectedId(null)
      setHasMore(false)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')

    hotelService
      .listMine(controller.signal)
      .then((page) => {
        if (cancelled) {
          return
        }
        if (!isHotelPage(page)) {
          setStatus('error')
          return
        }
        const items = page.items
        setHotels(items)
        setHasMore(page.total > items.length)
        if (items.length === 0) {
          // A real, legitimate state: an account with no membership yet. Not an error, and
          // emphatically not an empty dashboard full of zeroes.
          setStatus('empty')
          return
        }
        const remembered = readSelection()
        const match = items.find((hotel) => hotel.public_id === remembered)
        setSelectedId((match ?? items[0]!).public_id)
        setStatus('ready')
      })
      .catch(() => {
        if (cancelled) {
          return
        }
        // A 401 has already been handled by the API client's bridge, which ends the session;
        // this branch only has to avoid claiming the user has no hotels when the request
        // simply failed. The two are different states and the UI renders them differently.
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [isAuthenticated, attempt])

  const select = useCallback(
    (publicId: string) => {
      // Only an id the server returned. A value from anywhere else is not a shortcut to a
      // hotel -- the backend would refuse it -- but accepting one here would put the UI in a
      // state its own data does not describe.
      if (!hotels.some((hotel) => hotel.public_id === publicId)) {
        return
      }
      setSelectedId(publicId)
      writeSelection(publicId)
    },
    [hotels],
  )

  const retry = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const selected = useMemo(
    () => hotels.find((hotel) => hotel.public_id === selectedId) ?? null,
    [hotels, selectedId],
  )

  const value = useMemo<HotelContextValue>(
    () => ({ status, hotels, selected, hasMore, select, retry }),
    [status, hotels, selected, hasMore, select, retry],
  )

  return <HotelContext.Provider value={value}>{children}</HotelContext.Provider>
}

/** Read the hotel context. Throws outside a provider, for the reasons `useAuth` does. */
export function useHotelContext(): HotelContextValue {
  const context = useContext(HotelContext)
  if (context === null) {
    throw new Error('useHotelContext must be used within a HotelProvider')
  }
  return context
}
