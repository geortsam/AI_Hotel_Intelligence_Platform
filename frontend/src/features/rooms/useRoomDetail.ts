import { useCallback, useEffect, useRef, useState } from 'react'

import { isRoom } from '@/features/rooms/useRoomList'
import { ApiError } from '@/services/api/ApiError'
import { roomService } from '@/services/rooms/roomService'
import type { Room, RoomUpdateRequest } from '@/types/room'

/**
 * One room, and the two mutations the API offers on it.
 *
 * ## The server's row is adopted, never the payload
 *
 * A successful `PATCH` stores the response. That is not the same object that was sent: the
 * room number is canonicalised, `updated_at` moves, and omitted fields come back with their
 * existing values. Echoing the request would show what was typed rather than what was stored.
 *
 * ## Status is a mutation like any other
 *
 * Setting a status is a `PATCH` with `{status}` and nothing else. There is no separate
 * endpoint for it and no transition policy to honour -- every ordering was accepted live --
 * so this hook exposes one `update` and the page decides what to put in it. A state machine
 * here would be an invention.
 *
 * ## Delete waits for the 204
 *
 * Nothing leaves the screen before the server has agreed to remove it, and a 409 -- which is
 * what a room with reservations returns -- leaves the page exactly as it was.
 *
 * `inFlight` is a ref rather than state: two clicks in the same tick both read the same stale
 * `false` from a state variable.
 */

export type RoomDetailStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface RoomDetailState {
  readonly status: RoomDetailStatus
  readonly room: Room | null
  readonly error: ApiError | null

  readonly pending: 'update' | 'delete' | null
  /** Copy written here, never the backend's message. */
  readonly saved: string | null
  readonly mutationError: ApiError | null
  readonly failedMutation: 'update' | 'delete' | null
  readonly deleted: boolean

  readonly reload: () => void
  readonly update: (payload: RoomUpdateRequest, message: string) => Promise<boolean>
  readonly remove: () => Promise<boolean>
  readonly dismiss: () => void
}

export interface UseRoomDetailOptions {
  readonly hotelPublicId: string | null
  readonly roomTypeCode: string | undefined
  readonly roomNumber: string | undefined
}

export function useRoomDetail({
  hotelPublicId,
  roomTypeCode,
  roomNumber,
}: UseRoomDetailOptions): RoomDetailState {
  const [status, setStatus] = useState<RoomDetailStatus>('idle')
  const [room, setRoom] = useState<Room | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [attempt, setAttempt] = useState(0)

  const [pending, setPending] = useState<'update' | 'delete' | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const [mutationError, setMutationError] = useState<ApiError | null>(null)
  const [failedMutation, setFailedMutation] = useState<'update' | 'delete' | null>(null)
  const [deleted, setDeleted] = useState(false)

  const inFlight = useRef(false)

  useEffect(() => {
    if (
      hotelPublicId === null ||
      roomTypeCode === undefined ||
      roomNumber === undefined ||
      deleted
    ) {
      // A deleted room is not re-fetched: it is gone, and asking for it would replace the
      // confirmation with a 404 the operator did not cause.
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    roomService
      .get(hotelPublicId, roomTypeCode, roomNumber, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        if (!isRoom(result)) {
          setRoom(null)
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The room response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setRoom(result)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setRoom(null)
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The room could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, roomTypeCode, roomNumber, attempt, deleted])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const update = useCallback(
    async (payload: RoomUpdateRequest, message: string): Promise<boolean> => {
      if (
        inFlight.current ||
        hotelPublicId === null ||
        roomTypeCode === undefined ||
        roomNumber === undefined
      ) {
        return false
      }
      inFlight.current = true
      setPending('update')
      setMutationError(null)
      setFailedMutation(null)
      setSaved(null)

      try {
        const result = await roomService.update(hotelPublicId, roomTypeCode, roomNumber, payload)
        if (!isRoom(result)) {
          throw new ApiError(
            200,
            ApiError.MALFORMED_CODE,
            'The response to the update was not in the expected format.',
          )
        }
        setRoom(result)
        setSaved(message)
        return true
      } catch (cause: unknown) {
        setFailedMutation('update')
        setMutationError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The room could not be updated.'),
        )
        return false
      } finally {
        inFlight.current = false
        setPending(null)
      }
    },
    [hotelPublicId, roomTypeCode, roomNumber],
  )

  const remove = useCallback(async (): Promise<boolean> => {
    if (
      inFlight.current ||
      hotelPublicId === null ||
      roomTypeCode === undefined ||
      roomNumber === undefined
    ) {
      return false
    }
    inFlight.current = true
    setPending('delete')
    setMutationError(null)
    setFailedMutation(null)
    setSaved(null)

    try {
      await roomService.remove(hotelPublicId, roomTypeCode, roomNumber)
      setDeleted(true)
      return true
    } catch (cause: unknown) {
      setFailedMutation('delete')
      setMutationError(
        cause instanceof ApiError
          ? cause
          : new ApiError(0, ApiError.NETWORK_CODE, 'The room could not be deleted.'),
      )
      return false
    } finally {
      inFlight.current = false
      setPending(null)
    }
  }, [hotelPublicId, roomTypeCode, roomNumber])

  const dismiss = useCallback(() => {
    setSaved(null)
    setMutationError(null)
    setFailedMutation(null)
  }, [])

  return {
    status,
    room,
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
