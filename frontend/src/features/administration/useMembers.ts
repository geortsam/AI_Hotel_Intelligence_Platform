import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { DEFAULT_PAGE_SIZE, memberService } from '@/services/members/memberService'
import type { Member, MemberCreateRequest, MemberUpdateRequest } from '@/types/member'

/**
 * One hotel's membership list, and the three writes that change it.
 *
 * ## Nothing here decides authorization
 *
 * The frontend is never told the caller's role -- `/auth/me` returns "who the caller is, not
 * what they may access", in the backend's own words. So this offers every documented
 * operation and renders the server's refusal, which is the pattern every stage since 5.8 has
 * followed. A client-side role check would be a guess layered over the real decision, and
 * the guess would be wrong for exactly the users it mattered for.
 *
 * ## Reads and writes adopt the server's answer
 *
 * `add` and `changeRole` return the written row, but the list is re-read rather than patched
 * in place: it is ordered by email across pages, so where a new member lands -- and whether
 * they land on *this* page at all -- is the server's to decide. `remove` returns 204 and has
 * nothing to adopt.
 *
 * ## The last-owner rule is the server's
 *
 * A hotel must always keep at least one owner, enforced under a row lock the service holds
 * until it commits. This hook does not attempt to predict it: it would have to count owners
 * across every page, and a count read here is a guess about a number another administrator
 * may already be changing. The 409 is the answer, and the page explains it.
 *
 * `inFlight` is a ref rather than state: two clicks in the same tick both read the same
 * stale `false` from a state variable.
 */

export type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

/** Which write failed, so the page can say the right thing about it. */
export type FailedWrite = 'member-add' | 'member-role' | 'member-remove'

export interface MembersState {
  readonly members: readonly Member[]
  readonly status: LoadStatus
  readonly error: ApiError | null
  readonly total: number
  readonly pages: number
  readonly page: number
  readonly pageSize: number

  /** Which mutation is in flight, or null. */
  readonly pending: FailedWrite | null
  /** Which member a per-row mutation is acting on, so only their controls go busy. */
  readonly pendingMember: string | null
  /** Copy written here, never the backend's message. */
  readonly saved: string | null
  readonly writeError: ApiError | null
  readonly failedWrite: FailedWrite | null

  readonly setPage: (page: number) => void
  readonly setPageSize: (pageSize: number) => void
  readonly reload: () => void
  readonly addMember: (payload: MemberCreateRequest) => Promise<boolean>
  readonly changeRole: (userPublicId: string, payload: MemberUpdateRequest) => Promise<boolean>
  readonly removeMember: (userPublicId: string) => Promise<boolean>
  readonly dismiss: () => void
}

export function useMembers(hotelPublicId: string | null): MembersState {
  const [members, setMembers] = useState<readonly Member[]>([])
  const [status, setStatus] = useState<LoadStatus>('idle')
  const [error, setError] = useState<ApiError | null>(null)
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(DEFAULT_PAGE_SIZE)
  const [attempt, setAttempt] = useState(0)

  const [pending, setPending] = useState<FailedWrite | null>(null)
  const [pendingMember, setPendingMember] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const [writeError, setWriteError] = useState<ApiError | null>(null)
  const [failedWrite, setFailedWrite] = useState<FailedWrite | null>(null)

  const inFlight = useRef(false)

  useEffect(() => {
    if (hotelPublicId === null) {
      setStatus('idle')
      setMembers([])
      setTotal(0)
      setPages(0)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    memberService
      .list(hotelPublicId, { page, pageSize }, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        /*
         * A 200 is not proof of the documented shape. `members.map` on something else would
         * take the page down from a render, past this `catch`. An unreadable answer is a
         * failure, never a hotel with no members -- and a hotel with no members is not a
         * state the backend can even produce, since the last owner cannot be removed.
         */
        if (!Array.isArray((result as { items?: unknown }).items)) {
          setMembers([])
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The member response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setMembers(result.items)
        setTotal(result.total)
        setPages(result.pages)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setMembers([])
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The member list could not be loaded.'),
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
    setPage(1)
  }, [])

  /** Runs one write, keeping the in-flight guard and the failure bookkeeping in one place. */
  const run = useCallback(
    async (
      kind: FailedWrite,
      subject: string | null,
      call: () => Promise<void>,
      message: string,
    ): Promise<boolean> => {
      if (inFlight.current || hotelPublicId === null) {
        return false
      }
      inFlight.current = true
      setPending(kind)
      setPendingMember(subject)
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
        setPendingMember(null)
      }
    },
    [hotelPublicId],
  )

  const addMember = useCallback(
    (payload: MemberCreateRequest) =>
      run(
        'member-add',
        null,
        async () => {
          await memberService.add(hotelPublicId!, payload)
          // Re-read: the list is ordered by email, so where a new member sorts -- and
          // whether they land on this page -- is the server's to decide.
          setAttempt((n) => n + 1)
        },
        'Member added.',
      ),
    [run, hotelPublicId],
  )

  const changeRole = useCallback(
    (userPublicId: string, payload: MemberUpdateRequest) =>
      run(
        'member-role',
        userPublicId,
        async () => {
          await memberService.changeRole(hotelPublicId!, userPublicId, payload)
          setAttempt((n) => n + 1)
        },
        'Role changed.',
      ),
    [run, hotelPublicId],
  )

  const removeMember = useCallback(
    (userPublicId: string) =>
      run(
        'member-remove',
        userPublicId,
        async () => {
          await memberService.remove(hotelPublicId!, userPublicId)
          setAttempt((n) => n + 1)
        },
        'Member removed. Their account still exists; only their access to this property was revoked.',
      ),
    [run, hotelPublicId],
  )

  const dismiss = useCallback(() => {
    setSaved(null)
    setWriteError(null)
    setFailedWrite(null)
  }, [])

  return {
    members,
    status,
    error,
    total,
    pages,
    page,
    pageSize,
    pending,
    pendingMember,
    saved,
    writeError,
    failedWrite,
    setPage,
    setPageSize,
    reload,
    addMember,
    changeRole,
    removeMember,
    dismiss,
  }
}
