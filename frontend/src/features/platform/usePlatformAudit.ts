import { useCallback, useEffect, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { DEFAULT_PAGE_SIZE, platformService } from '@/services/platform/platformService'
import type { PlatformAuditEvent } from '@/types/platform'

/**
 * The platform audit trail: read-only, and the only read on this screen that can be refused.
 *
 * Every catalogue *read* needs a session and nothing more. This one needs the platform
 * administrator grant, so a 403 here is ordinary rather than exceptional and the page treats
 * it as a state to explain, not a failure to retry.
 *
 * ## Nothing is derived
 *
 * No counting, no grouping, no "who changed most", no reconstruction of a before-and-after
 * from two rows. The trail is a list of what the server recorded, shown as recorded. The
 * `details` object is rendered as text because it is written by the backend to be safe, and
 * because guessing at its shape per action would be inventing a schema the API does not
 * publish.
 *
 * ## It is append-only, so there is nothing to offer but reading
 *
 * No endpoint writes, edits or deletes an event, which is what makes the trail worth reading.
 * The page therefore has no control that could change one.
 */

export type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface PlatformAuditState {
  readonly events: readonly PlatformAuditEvent[]
  readonly status: LoadStatus
  readonly error: ApiError | null
  readonly total: number
  readonly pages: number
  readonly page: number
  readonly pageSize: number
  readonly action: string | undefined
  readonly resourceType: string | undefined

  readonly setPage: (page: number) => void
  readonly setPageSize: (pageSize: number) => void
  readonly setAction: (action: string | undefined) => void
  readonly setResourceType: (resourceType: string | undefined) => void
  readonly reload: () => void
}

export function usePlatformAudit(enabled: boolean): PlatformAuditState {
  const [events, setEvents] = useState<readonly PlatformAuditEvent[]>([])
  const [status, setStatus] = useState<LoadStatus>('idle')
  const [error, setError] = useState<ApiError | null>(null)
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(DEFAULT_PAGE_SIZE)
  const [action, setActionState] = useState<string | undefined>(undefined)
  const [resourceType, setResourceTypeState] = useState<string | undefined>(undefined)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!enabled) {
      // The trail is on a tab. Not fetching until it is opened keeps a 403 out of the
      // console for the majority of operators, who have no grant and no reason to want one.
      setStatus('idle')
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    platformService
      .listPlatformAuditEvents(
        {
          page,
          pageSize,
          ...(action ? { action } : {}),
          ...(resourceType ? { resourceType } : {}),
        },
        controller.signal,
      )
      .then((result) => {
        if (cancelled) {
          return
        }
        if (!Array.isArray((result as { items?: unknown }).items)) {
          setEvents([])
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The audit response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setEvents(result.items)
        setTotal(result.total)
        setPages(result.pages)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setEvents([])
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The audit trail could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [enabled, page, pageSize, action, resourceType, attempt])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const setPageSize = useCallback((next: number) => {
    setPageSizeState(next)
    setPage(1)
  }, [])

  const setAction = useCallback((next: string | undefined) => {
    setActionState(next)
    setPage(1)
  }, [])

  const setResourceType = useCallback((next: string | undefined) => {
    setResourceTypeState(next)
    setPage(1)
  }, [])

  return {
    events,
    status,
    error,
    total,
    pages,
    page,
    pageSize,
    action,
    resourceType,
    setPage,
    setPageSize,
    setAction,
    setResourceType,
    reload,
  }
}
