import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import {
  DEFAULT_PAGE_SIZE,
  type BreakdownRange,
  type RevenueQuery,
} from '@/services/finance/financeService'
import type { DateRange } from '@/types/analytics'
import type { Page } from '@/types/api'

/**
 * One financial journal: a page of it, the server's totals over it, and posting a line to it.
 *
 * ## Nothing financial is computed here, and the absences are deliberate
 *
 * This hook holds rows the server sent and buckets the server aggregated. It contains no
 * `reduce`, no `+` on a monetary value, no `parseFloat`, and no `Number()` on an amount. It
 * does not:
 *
 * * add up the amounts on a page to show a total -- the page is one of many, so that figure
 *   would be a partial sum wearing the name of a whole one;
 * * add up the buckets to show an overall figure -- the breakdown endpoint groups by
 *   `(category_code, currency)` and reports no grand total, and producing one would mean
 *   either summing across currencies, which is not money, or inventing a per-currency
 *   subtotal the server never stated;
 * * derive profit from revenue minus expenses -- no endpoint reports that, the two journals
 *   are aggregated separately, and their currencies need not even match.
 *
 * The only arithmetic in this file is on `page` numbers.
 *
 * ## Two requests per view, and one for the catalogue
 *
 * A filter change issues exactly two: the journal page, and the breakdown for the range.
 * Nothing is fetched per row -- a ledger row is complete as it arrives -- so there is no
 * N+1 here. The category catalogue is global and static, so it is fetched once per mount and
 * re-read only after a posting, never per keystroke.
 *
 * ## Append-only, so no optimistic row and no automatic retry
 *
 * A successful posting re-reads the journal and the breakdown rather than pushing the
 * returned object into the local array: the list is server-ordered and paginated, and the
 * totals must be recomputed by the database that owns them. A **failed** posting appends
 * nothing, and is never retried automatically -- the journal has no uniqueness constraint, so
 * a retry that the first attempt actually reached would post the line twice. Whether to try
 * again is the operator's decision, taken after reading what happened.
 *
 * `inFlight` is a ref rather than state because two clicks in the same tick both read the
 * same stale `false` from a state variable, and both fire.
 */

export type LedgerStatus = 'idle' | 'loading' | 'ready' | 'error'

/** A breakdown response, in the shape both kinds share. */
export interface Breakdown<TBucket> {
  readonly range: DateRange
  readonly categories: readonly TBucket[]
}

/** The four calls a journal needs, supplied by the caller so this core serves both kinds. */
export interface LedgerSource<TEntry, TBucket, TCreate, TCategory> {
  list(hotel: string, query: RevenueQuery, signal?: AbortSignal): Promise<Page<TEntry>>
  create(hotel: string, payload: TCreate, signal?: AbortSignal): Promise<TEntry>
  breakdown(hotel: string, range: BreakdownRange, signal?: AbortSignal): Promise<Breakdown<TBucket>>
  categories(signal?: AbortSignal): Promise<Page<TCategory>>
}

/** What narrows the journal. The range comes from the period and is always present. */
export interface LedgerFilters {
  /** A category code, or `''` for every category. */
  readonly categoryCode: string
  /** A booking UUID, or `''`. Revenue only; the expense journal never sets it. */
  readonly bookingPublicId: string
}

export const NO_FILTERS: LedgerFilters = { categoryCode: '', bookingPublicId: '' }

export function hasActiveFilters(filters: LedgerFilters): boolean {
  return filters.categoryCode !== '' || filters.bookingPublicId.trim() !== ''
}

/** The confirmation shown after the server accepted a line. */
export interface LedgerPosted<TEntry> {
  /** Copy written here, never the backend's message. */
  readonly message: string
  /** The row the server created, so the confirmation can restate what was recorded. */
  readonly entry: TEntry
  /**
   * Whether the posted line falls outside the period currently on screen.
   *
   * A date comparison on two `YYYY-MM-DD` strings, which sort lexically -- not a calculation
   * over money. Without it, posting a line dated last month while viewing "Last 7 days"
   * succeeds and the journal below does not change, which reads as a failure.
   */
  readonly outsideRange: boolean
}

export interface LedgerState<TEntry, TBucket, TCreate, TCategory> {
  readonly status: LedgerStatus
  readonly entries: readonly TEntry[]
  /** The server's row count across all pages. A count of lines, never a sum of money. */
  readonly total: number
  readonly pages: number
  readonly page: number
  /** Rows requested per page. Every option offered is inside the router's own 1..100. */
  readonly pageSize: number
  readonly error: ApiError | null

  /** The server's totals for the range. `null` until loaded, or when it failed. */
  readonly breakdown: Breakdown<TBucket> | null
  /** Kept apart from `error`: the journal can load when the aggregation does not. */
  readonly breakdownError: ApiError | null

  /** The global catalogue, for the category control and the posting form. */
  readonly categories: readonly TCategory[]
  /**
   * Whether the catalogue could not be read.
   *
   * Distinct from an empty catalogue, and the difference is visible: a failed read leaves the
   * posting form with a typed code and says why, while an empty one would be a platform with
   * no categories configured. Silently showing an empty dropdown -- which is what this did
   * until a browser check found the category request returning 422 -- makes posting
   * impossible with nothing on screen to explain it.
   */
  readonly categoriesUnavailable: boolean

  readonly filters: LedgerFilters
  readonly pending: boolean
  readonly posted: LedgerPosted<TEntry> | null
  /** The last refusal. Rendered through `describeFailure`, never raw. */
  readonly postError: ApiError | null

  readonly setPage: (page: number) => void
  readonly setPageSize: (pageSize: number) => void
  readonly setFilters: (filters: LedgerFilters) => void
  readonly reload: () => void
  readonly post: (payload: TCreate) => Promise<boolean>
  readonly dismiss: () => void
}

export interface UseLedgerOptions<TEntry, TBucket, TCreate, TCategory> {
  readonly hotelPublicId: string | null
  /** The reporting period, resolved in the hotel's own zone by the page. */
  readonly range: BreakdownRange
  readonly source: LedgerSource<TEntry, TBucket, TCreate, TCategory>
  /** The date field on a created row, so a posting can be placed against the range. */
  readonly entryDate: (entry: TEntry) => string
  /** Copy for the confirmation banner. */
  readonly postedMessage: string
}

export function useLedger<TEntry, TBucket, TCreate, TCategory>({
  hotelPublicId,
  range,
  source,
  entryDate,
  postedMessage,
}: UseLedgerOptions<TEntry, TBucket, TCreate, TCategory>): LedgerState<
  TEntry,
  TBucket,
  TCreate,
  TCategory
> {
  const [status, setStatus] = useState<LedgerStatus>('idle')
  const [entries, setEntries] = useState<readonly TEntry[]>([])
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(DEFAULT_PAGE_SIZE)
  const [error, setError] = useState<ApiError | null>(null)

  const [breakdown, setBreakdown] = useState<Breakdown<TBucket> | null>(null)
  const [breakdownError, setBreakdownError] = useState<ApiError | null>(null)
  const [categories, setCategories] = useState<readonly TCategory[]>([])
  const [categoriesUnavailable, setCategoriesUnavailable] = useState(false)

  const [filters, setFiltersState] = useState<LedgerFilters>(NO_FILTERS)
  const [attempt, setAttempt] = useState(0)

  const [pending, setPending] = useState(false)
  const [posted, setPosted] = useState<LedgerPosted<TEntry> | null>(null)
  const [postError, setPostError] = useState<ApiError | null>(null)

  const inFlight = useRef(false)

  const { dateFrom, dateTo } = range
  const { categoryCode, bookingPublicId } = filters

  /* --- the journal page ---------------------------------------------------------------- */

  useEffect(() => {
    if (hotelPublicId === null) {
      setStatus('idle')
      setEntries([])
      setTotal(0)
      setPages(0)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    const booking = bookingPublicId.trim()
    source
      .list(
        hotelPublicId,
        {
          page,
          pageSize,
          dateFrom,
          dateTo,
          ...(categoryCode !== '' ? { categoryCode } : {}),
          ...(booking !== '' ? { bookingPublicId: booking } : {}),
        },
        controller.signal,
      )
      .then((result) => {
        if (cancelled) {
          return
        }
        /*
         * A 200 is not proof of the documented shape. The client validates nothing at
         * runtime, so a proxy or a moved route can answer 200 with something else -- and
         * `entries.map` on that would take the page down from a render, past this `catch`.
         * An unreadable answer is a failure, never an empty journal. This exact class of
         * bug took the dashboard down in Stage 5.4.
         */
        if (!Array.isArray((result as { items?: unknown }).items)) {
          setEntries([])
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The ledger response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setEntries(result.items)
        setTotal(result.total)
        setPages(result.pages)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setEntries([])
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The ledger could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, dateFrom, dateTo, categoryCode, bookingPublicId, page, pageSize, attempt, source])

  /* --- the server's totals for the range ----------------------------------------------- */

  useEffect(() => {
    if (hotelPublicId === null) {
      setBreakdown(null)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setBreakdownError(null)

    source
      .breakdown(hotelPublicId, { dateFrom, dateTo }, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        if (!Array.isArray((result as { categories?: unknown }).categories)) {
          setBreakdown(null)
          setBreakdownError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The totals response was not in the expected format.',
            ),
          )
          return
        }
        setBreakdown(result)
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setBreakdown(null)
        setBreakdownError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The totals could not be loaded.'),
        )
      })

    return () => {
      cancelled = true
      controller.abort()
    }
    // Deliberately NOT keyed on the filters: the aggregation endpoint accepts a date range
    // and nothing else, so a category filter cannot narrow it. Refetching on a filter change
    // would issue an identical request and imply the totals had followed the filter.
  }, [hotelPublicId, dateFrom, dateTo, attempt, source])

  /* --- the global category catalogue --------------------------------------------------- */

  useEffect(() => {
    const controller = new AbortController()
    let cancelled = false

    source
      .categories(controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        if (Array.isArray((result as { items?: unknown }).items)) {
          setCategories(result.items)
          setCategoriesUnavailable(false)
        } else {
          setCategories([])
          setCategoriesUnavailable(true)
        }
      })
      .catch(() => {
        // A missing catalogue is not a failed page: the journal renders the codes the server
        // sent on each row, and posting falls back to a typed code. But it is not nothing
        // either, so it is reported rather than swallowed.
        if (!cancelled) {
          setCategories([])
          setCategoriesUnavailable(true)
        }
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [source])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const setPageSize = useCallback((next: number) => {
    setPageSizeState(next)
    // Page 4 of a 20-row view is past the end of a 100-row one. The backend answers a
    // too-high page with an empty 200 rather than an error, so this would be a blank screen.
    setPage(1)
  }, [])

  const setFilters = useCallback((next: LedgerFilters) => {
    setFiltersState(next)
    // A narrower journal has fewer pages; staying on page 4 of a one-page result shows an
    // empty screen that looks like "no lines" rather than "no such page".
    setPage(1)
  }, [])

  const post = useCallback(
    async (payload: TCreate): Promise<boolean> => {
      if (inFlight.current || hotelPublicId === null) {
        return false
      }
      inFlight.current = true
      setPending(true)
      setPostError(null)
      setPosted(null)

      try {
        const entry = await source.create(hotelPublicId, payload)
        const date = entryDate(entry)
        if (typeof date !== 'string' || date === '') {
          throw new ApiError(
            201,
            ApiError.MALFORMED_CODE,
            'The posting response was not in the expected format.',
          )
        }
        setPosted({
          message: postedMessage,
          entry,
          // `YYYY-MM-DD` sorts lexically, so this needs no date parsing and no arithmetic.
          outsideRange: date < dateFrom || date > dateTo,
        })
        setAttempt((n) => n + 1)
        return true
      } catch (cause: unknown) {
        setPostError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The line could not be posted.'),
        )
        return false
      } finally {
        inFlight.current = false
        setPending(false)
      }
    },
    [hotelPublicId, source, entryDate, postedMessage, dateFrom, dateTo],
  )

  const dismiss = useCallback(() => {
    setPosted(null)
    setPostError(null)
  }, [])

  return {
    status,
    entries,
    total,
    pages,
    page,
    pageSize,
    error,
    breakdown,
    breakdownError,
    categories,
    categoriesUnavailable,
    filters,
    pending,
    posted,
    postError,
    setPage,
    setPageSize,
    setFilters,
    reload,
    post,
    dismiss,
  }
}
