import { useCallback, useEffect, useRef, useState } from 'react'

import { isGuest } from '@/features/guests/useGuestList'
import { ApiError } from '@/services/api/ApiError'
import { guestService } from '@/services/guests/guestService'
import type { Guest, GuestUpdateRequest } from '@/types/guest'

/**
 * One guest, and the two mutations the API offers on them.
 *
 * ## Nothing is optimistic
 *
 * A successful `PATCH` adopts **the server's response**, which is the updated row -- not the
 * payload that was sent. The two are not the same: `country_code` and `preferred_language`
 * are normalised server-side (`gb` becomes `GB`, `EN` becomes `en`, both verified live), and
 * `updated_at` moves. Echoing the request back would show the operator what they typed rather
 * than what was stored, and the difference is exactly where a silent divergence starts.
 *
 * A **failed** mutation changes nothing on screen, and nothing is retried automatically.
 *
 * ## Delete is separated from update, because its refusals differ
 *
 * `DELETE` needs the **manager** role where `PATCH` needs staff, and it has a 409 that update
 * does not: a guest with reservations or reviews cannot be removed. Both outcomes are the
 * database's, and the hook reports which mutation failed so the page can say the right thing.
 *
 * `inFlight` is a ref rather than state: two clicks in the same tick both read the same stale
 * `false` from a state variable, and on a delete that is not a cosmetic bug.
 */

export type GuestDetailStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface GuestDetailState {
  readonly status: GuestDetailStatus
  readonly guest: Guest | null
  readonly error: ApiError | null

  /** Which mutation is in flight, or null. */
  readonly pending: 'update' | 'delete' | null
  /** Copy written here, never the backend's message. Cleared when a new attempt starts. */
  readonly saved: string | null
  /** The last refusal, with the mutation it came from so the copy can match. */
  readonly mutationError: ApiError | null
  readonly failedMutation: 'update' | 'delete' | null
  /** Set once the server has confirmed the guest is gone. */
  readonly deleted: boolean

  readonly reload: () => void
  readonly update: (payload: GuestUpdateRequest) => Promise<boolean>
  readonly remove: () => Promise<boolean>
  readonly dismiss: () => void
}

export interface UseGuestDetailOptions {
  readonly hotelPublicId: string | null
  readonly guestPublicId: string | undefined
}

export function useGuestDetail({
  hotelPublicId,
  guestPublicId,
}: UseGuestDetailOptions): GuestDetailState {
  const [status, setStatus] = useState<GuestDetailStatus>('idle')
  const [guest, setGuest] = useState<Guest | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [attempt, setAttempt] = useState(0)

  const [pending, setPending] = useState<'update' | 'delete' | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const [mutationError, setMutationError] = useState<ApiError | null>(null)
  const [failedMutation, setFailedMutation] = useState<'update' | 'delete' | null>(null)
  const [deleted, setDeleted] = useState(false)

  const inFlight = useRef(false)

  useEffect(() => {
    if (hotelPublicId === null || guestPublicId === undefined || deleted) {
      // A deleted guest is not re-fetched: the row is gone, and asking for it would replace
      // the confirmation with a 404 the operator did not cause.
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    guestService
      .get(hotelPublicId, guestPublicId, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        if (!isGuest(result)) {
          setGuest(null)
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The guest response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setGuest(result)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setGuest(null)
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The guest could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, guestPublicId, attempt, deleted])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const update = useCallback(
    async (payload: GuestUpdateRequest): Promise<boolean> => {
      if (inFlight.current || hotelPublicId === null || guestPublicId === undefined) {
        return false
      }
      inFlight.current = true
      setPending('update')
      setMutationError(null)
      setFailedMutation(null)
      setSaved(null)

      try {
        const result = await guestService.update(hotelPublicId, guestPublicId, payload)
        if (!isGuest(result)) {
          throw new ApiError(
            200,
            ApiError.MALFORMED_CODE,
            'The response to the update was not in the expected format.',
          )
        }
        // The server's row, not the payload: see the module docstring.
        setGuest(result)
        setSaved('Guest updated.')
        return true
      } catch (cause: unknown) {
        setFailedMutation('update')
        setMutationError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The guest could not be updated.'),
        )
        return false
      } finally {
        inFlight.current = false
        setPending(null)
      }
    },
    [hotelPublicId, guestPublicId],
  )

  const remove = useCallback(async (): Promise<boolean> => {
    if (inFlight.current || hotelPublicId === null || guestPublicId === undefined) {
      return false
    }
    inFlight.current = true
    setPending('delete')
    setMutationError(null)
    setFailedMutation(null)
    setSaved(null)

    try {
      await guestService.remove(hotelPublicId, guestPublicId)
      // Only now, after a 204. Nothing about the guest is removed from the screen before the
      // server has agreed to remove it from the database.
      setDeleted(true)
      return true
    } catch (cause: unknown) {
      setFailedMutation('delete')
      setMutationError(
        cause instanceof ApiError
          ? cause
          : new ApiError(0, ApiError.NETWORK_CODE, 'The guest could not be deleted.'),
      )
      return false
    } finally {
      inFlight.current = false
      setPending(null)
    }
  }, [hotelPublicId, guestPublicId])

  const dismiss = useCallback(() => {
    setSaved(null)
    setMutationError(null)
    setFailedMutation(null)
  }, [])

  return {
    status,
    guest,
    error,
    pending,
    saved,
    mutationError,
    failedMutation,
    deleted,
    reload,
    update,
    remove,
    dismiss,
  }
}
