import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { DEFAULT_PAGE_SIZE, platformService } from '@/services/platform/platformService'
import type { ExpenseCategory, RevenueCategory } from '@/types/finance'
import type { Amenity, CatalogueKind } from '@/types/platform'

/**
 * One shared catalogue, and the three writes that maintain it.
 *
 * ## One hook for three resources, because the contract is the same shape three times
 *
 * Amenities, revenue categories and expense categories are each a globally unique `code`, a
 * name, and one or two booleans; each has the same page surface, the same
 * `require_platform_admin` on its writes, and the same 409 on deleting something still
 * referenced. Three hooks would be the same code three times with different strings in it.
 *
 * The **one** real difference is carried explicitly rather than smoothed over: the category
 * lists accept `is_active` and the amenity list does not, because `amenities` has no such
 * column. `supportsActiveFilter` says which, and the page only renders the control when it
 * is true.
 *
 * ## Nothing here decides authorization
 *
 * Writing a shared catalogue needs a platform administrator grant, which is not a hotel role
 * and is not ranked against one. The frontend is told neither the grant nor the role, so it
 * offers the documented control and renders the server's 403 -- the pattern every stage since
 * 5.8 has used, for the same reason: a client-side guess would be wrong for exactly the
 * users it mattered for.
 *
 * ## Writes re-read rather than patch in place
 *
 * Every list is ordered by code, so where a new entry sorts -- and whether it lands on this
 * page at all -- is the server's to decide. A delete likewise changes what the page contains.
 */

export type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

/** Which write failed, so the page can say the right thing about it. */
export type FailedWrite = 'create' | 'update' | 'delete'

/** A row of any of the three catalogues, seen through what they share. */
export interface CatalogueRow {
  readonly code: string
  readonly name: string
  /** The amenity catalogue's free-text grouping. Absent on the category catalogues. */
  readonly category?: string | null
  /** `is_active` on the categories. Amenities have no such column, so this is undefined. */
  readonly isActive?: boolean
  /** `is_room_revenue` or `is_fixed_cost`, whichever this catalogue has. */
  readonly flag?: boolean
}

export interface CatalogueState {
  readonly rows: readonly CatalogueRow[]
  readonly status: LoadStatus
  readonly error: ApiError | null
  readonly total: number
  readonly pages: number
  readonly page: number
  readonly pageSize: number
  /** Whether this catalogue's list route accepts `is_active`. */
  readonly supportsActiveFilter: boolean
  readonly activeFilter: boolean | undefined

  readonly pending: FailedWrite | null
  readonly pendingCode: string | null
  readonly saved: string | null
  readonly writeError: ApiError | null
  readonly failedWrite: FailedWrite | null

  readonly setPage: (page: number) => void
  readonly setPageSize: (pageSize: number) => void
  readonly setActiveFilter: (value: boolean | undefined) => void
  readonly reload: () => void
  readonly create: (payload: Record<string, unknown>) => Promise<boolean>
  readonly update: (code: string, payload: Record<string, unknown>) => Promise<boolean>
  readonly remove: (code: string) => Promise<boolean>
  readonly dismiss: () => void
}

/** What each catalogue is called and which service calls belong to it. */
const CATALOGUES = {
  amenities: {
    supportsActiveFilter: false,
    list: (page: number, pageSize: number, _active: boolean | undefined, signal: AbortSignal) =>
      platformService.listAmenities({ page, pageSize }, signal),
    create: (payload: Record<string, unknown>) => platformService.createAmenity(payload as never),
    update: (code: string, payload: Record<string, unknown>) =>
      platformService.updateAmenity(code, payload as never),
    remove: (code: string) => platformService.deleteAmenity(code),
    toRow: (row: Amenity): CatalogueRow => ({
      code: row.code,
      name: row.name,
      category: row.category,
    }),
  },
  'revenue-categories': {
    supportsActiveFilter: true,
    list: (page: number, pageSize: number, isActive: boolean | undefined, signal: AbortSignal) =>
      platformService.listRevenueCategories(
        { page, pageSize, ...(isActive === undefined ? {} : { isActive }) },
        signal,
      ),
    create: (payload: Record<string, unknown>) =>
      platformService.createRevenueCategory(payload as never),
    update: (code: string, payload: Record<string, unknown>) =>
      platformService.updateRevenueCategory(code, payload as never),
    remove: (code: string) => platformService.deleteRevenueCategory(code),
    toRow: (row: RevenueCategory): CatalogueRow => ({
      code: row.code,
      name: row.name,
      isActive: row.is_active,
      flag: row.is_room_revenue,
    }),
  },
  'expense-categories': {
    supportsActiveFilter: true,
    list: (page: number, pageSize: number, isActive: boolean | undefined, signal: AbortSignal) =>
      platformService.listExpenseCategories(
        { page, pageSize, ...(isActive === undefined ? {} : { isActive }) },
        signal,
      ),
    create: (payload: Record<string, unknown>) =>
      platformService.createExpenseCategory(payload as never),
    update: (code: string, payload: Record<string, unknown>) =>
      platformService.updateExpenseCategory(code, payload as never),
    remove: (code: string) => platformService.deleteExpenseCategory(code),
    toRow: (row: ExpenseCategory): CatalogueRow => ({
      code: row.code,
      name: row.name,
      isActive: row.is_active,
      flag: row.is_fixed_cost,
    }),
  },
} as const

export function useCatalogue(kind: CatalogueKind): CatalogueState {
  const catalogue = CATALOGUES[kind]

  const [rows, setRows] = useState<readonly CatalogueRow[]>([])
  const [status, setStatus] = useState<LoadStatus>('idle')
  const [error, setError] = useState<ApiError | null>(null)
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(DEFAULT_PAGE_SIZE)
  const [activeFilter, setActiveFilterState] = useState<boolean | undefined>(undefined)
  const [attempt, setAttempt] = useState(0)

  const [pending, setPending] = useState<FailedWrite | null>(null)
  const [pendingCode, setPendingCode] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const [writeError, setWriteError] = useState<ApiError | null>(null)
  const [failedWrite, setFailedWrite] = useState<FailedWrite | null>(null)

  const inFlight = useRef(false)

  /* Switching catalogue resets the page and the filter: page 3 of amenities is not page 3 of
   * expense categories, and a filter one route has is one the other may not. */
  useEffect(() => {
    setPage(1)
    setActiveFilterState(undefined)
    setSaved(null)
    setWriteError(null)
    setFailedWrite(null)
  }, [kind])

  useEffect(() => {
    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    catalogue
      .list(page, pageSize, catalogue.supportsActiveFilter ? activeFilter : undefined, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        /*
         * A 200 is not proof of the documented shape. `rows.map` on something else would take
         * the page down from a render, past this `catch`. An unreadable answer is a failure,
         * never an empty catalogue -- and an empty catalogue is a real state here: the demo
         * installation has no amenities at all.
         */
        if (!Array.isArray((result as { items?: unknown }).items)) {
          setRows([])
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The catalogue response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setRows(result.items.map((row) => catalogue.toRow(row as never)))
        setTotal(result.total)
        setPages(result.pages)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setRows([])
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The catalogue could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [kind, page, pageSize, activeFilter, attempt])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const setPageSize = useCallback((next: number) => {
    setPageSizeState(next)
    setPage(1)
  }, [])

  const setActiveFilter = useCallback((next: boolean | undefined) => {
    setActiveFilterState(next)
    setPage(1)
  }, [])

  /** Runs one write, keeping the in-flight guard and the failure bookkeeping in one place. */
  const run = useCallback(
    async (
      failure: FailedWrite,
      code: string | null,
      call: () => Promise<void>,
      message: string,
    ): Promise<boolean> => {
      if (inFlight.current) {
        return false
      }
      inFlight.current = true
      setPending(failure)
      setPendingCode(code)
      setWriteError(null)
      setFailedWrite(null)
      setSaved(null)

      try {
        await call()
        setSaved(message)
        return true
      } catch (cause: unknown) {
        setFailedWrite(failure)
        setWriteError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The change could not be saved.'),
        )
        return false
      } finally {
        inFlight.current = false
        setPending(null)
        setPendingCode(null)
      }
    },
    [],
  )

  const create = useCallback(
    (payload: Record<string, unknown>) =>
      run(
        'create',
        null,
        async () => {
          await catalogue.create(payload)
          // Re-read: the list is ordered by code, and where a new entry sorts is the
          // server's to decide -- as is whether it lands on this page at all.
          setAttempt((n) => n + 1)
        },
        'Entry created.',
      ),
    [run, catalogue],
  )

  const update = useCallback(
    (code: string, payload: Record<string, unknown>) =>
      run(
        'update',
        code,
        async () => {
          await catalogue.update(code, payload)
          setAttempt((n) => n + 1)
        },
        'Entry updated.',
      ),
    [run, catalogue],
  )

  const remove = useCallback(
    (code: string) =>
      run(
        'delete',
        code,
        async () => {
          await catalogue.remove(code)
          setAttempt((n) => n + 1)
        },
        'Entry deleted.',
      ),
    [run, catalogue],
  )

  const dismiss = useCallback(() => {
    setSaved(null)
    setWriteError(null)
    setFailedWrite(null)
  }, [])

  return {
    rows,
    status,
    error,
    total,
    pages,
    page,
    pageSize,
    supportsActiveFilter: catalogue.supportsActiveFilter,
    activeFilter,
    pending,
    pendingCode,
    saved,
    writeError,
    failedWrite,
    setPage,
    setPageSize,
    setActiveFilter,
    reload,
    create,
    update,
    remove,
    dismiss,
  }
}
