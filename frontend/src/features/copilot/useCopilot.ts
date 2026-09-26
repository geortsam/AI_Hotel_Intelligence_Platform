import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { copilotService } from '@/services/copilot/copilotService'
import type { ConversationSummary } from '@/types/copilot'

import {
  fromAnswer,
  fromStoredTurn,
  fromTurn,
  isAnswer,
  isTranscript,
  isTurnResponse,
  type Exchange,
} from './exchange'

/**
 * The copilot screen's state: a one-off thread, an open conversation, and the caller's list.
 *
 * ## Two modes, and only one of them stores anything
 *
 * * `one-off` asks `POST …/copilot/ask`. The answer lives in this hook's memory and nowhere
 *   else: not in the server, not in browser storage. Leaving the page forgets it.
 * * `conversation` uses the Stage 7.11 endpoints: the first question starts a conversation,
 *   later ones continue it, and an earlier one can be reopened or deleted. The server stores
 *   those turns under its own retention rule.
 *
 * ## The hotel is the boundary, so changing it clears everything
 *
 * Every request is under `/hotels/{h}/…`, and a conversation belongs to one hotel. When the
 * shell's hotel changes, every thread, the open conversation, the list, any failure and any
 * request in flight are discarded — an answer about the previous property arriving late must
 * not appear under the new one. A generation counter makes the late arrival a no-op even when
 * the abort loses the race. The one thing kept is `disabled`: `LLM_DISABLED` describes the
 * deployment, not the hotel, and asking again elsewhere would only spend allowance.
 *
 * ## Nothing here decides authorization
 *
 * No role is known to the frontend and none is inferred. Which tools the model may use is the
 * server's decision, per request; this hook passes on what the response says was used.
 *
 * ## One question at a time
 *
 * Every question is charged against an hourly allowance, so a double click must not ask twice.
 * `inFlight` is a ref rather than state: two submits in the same tick both read the same stale
 * `false` from a state variable.
 */

export type CopilotMode = 'one-off' | 'conversation'

export type PendingAction = 'asking' | 'opening' | 'deleting'

export interface CopilotFailureState {
  readonly error: ApiError
  /** What was being done, which is what a 404 means. */
  readonly context: 'question' | 'conversation'
}

export interface OpenConversation {
  readonly publicId: string
  readonly exchanges: readonly Exchange[]
  readonly turnsRemaining: number
  /** From a transcript; null until one has been read, since a turn response carries none. */
  readonly expiresAt: string | null
}

export type ListStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface ConversationListState {
  readonly status: ListStatus
  readonly items: readonly ConversationSummary[]
  readonly total: number
  readonly error: ApiError | null
}

export interface CopilotState {
  readonly mode: CopilotMode
  readonly setMode: (mode: CopilotMode) => void
  /** The one-off thread of this visit, oldest first. */
  readonly oneOff: readonly Exchange[]
  readonly conversation: OpenConversation | null
  readonly list: ConversationListState
  readonly pending: PendingAction | null
  /** The question being answered, shown while it is in flight. */
  readonly pendingQuestion: string | null
  readonly failure: CopilotFailureState | null
  /** True after `LLM_DISABLED`: no new question is offered for the rest of this visit. */
  readonly disabled: boolean

  /** Resolves true when the question was answered, so the form may clear itself. */
  readonly ask: (question: string) => Promise<boolean>
  readonly newConversation: () => void
  readonly openConversation: (publicId: string) => Promise<void>
  readonly deleteConversation: (publicId: string) => Promise<boolean>
  readonly reloadList: () => void
  readonly dismiss: () => void
}

const EMPTY_LIST: ConversationListState = { status: 'idle', items: [], total: 0, error: null }

function malformed(): ApiError {
  return new ApiError(
    200,
    ApiError.MALFORMED_CODE,
    'The copilot response was not in the expected format.',
  )
}

function asApiError(cause: unknown): ApiError {
  return cause instanceof ApiError
    ? cause
    : new ApiError(0, ApiError.NETWORK_CODE, 'The request could not be completed.')
}

export function useCopilot(hotelPublicId: string | null): CopilotState {
  const [mode, setModeState] = useState<CopilotMode>('one-off')
  const [oneOff, setOneOff] = useState<readonly Exchange[]>([])
  const [conversation, setConversation] = useState<OpenConversation | null>(null)
  const [list, setList] = useState<ConversationListState>(EMPTY_LIST)
  const [listAttempt, setListAttempt] = useState(0)
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null)
  const [failure, setFailure] = useState<CopilotFailureState | null>(null)
  const [disabled, setDisabled] = useState(false)

  const inFlight = useRef(false)
  const generation = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const keyCounter = useRef(0)

  const nextKey = useCallback(() => {
    keyCounter.current += 1
    return `exchange-${keyCounter.current}`
  }, [])

  /* --- the hotel boundary --------------------------------------------------------------- */

  useEffect(() => {
    generation.current += 1
    controller.current?.abort()
    controller.current = null
    inFlight.current = false
    setOneOff([])
    setConversation(null)
    setList(EMPTY_LIST)
    setPending(null)
    setPendingQuestion(null)
    setFailure(null)
    return () => {
      // Unmounting is a boundary too: nothing may land after the page has gone.
      generation.current += 1
      controller.current?.abort()
    }
  }, [hotelPublicId])

  /* --- the caller's conversations ------------------------------------------------------- */

  useEffect(() => {
    if (hotelPublicId === null || mode !== 'conversation') {
      setList(EMPTY_LIST)
      return
    }
    const request = new AbortController()
    let cancelled = false
    setList((previous) => ({ ...previous, status: 'loading', error: null }))

    copilotService
      .listConversations(hotelPublicId, request.signal)
      .then((page) => {
        if (cancelled) {
          return
        }
        // A 200 is not proof of the documented shape; an unreadable list is a failure, never
        // "you have no conversations".
        if (!Array.isArray((page as { items?: unknown }).items)) {
          setList({ status: 'error', items: [], total: 0, error: malformed() })
          return
        }
        setList({ status: 'ready', items: page.items, total: page.total, error: null })
      })
      .catch((cause: unknown) => {
        if (cancelled || request.signal.aborted) {
          return
        }
        setList({ status: 'error', items: [], total: 0, error: asApiError(cause) })
      })

    return () => {
      cancelled = true
      request.abort()
    }
  }, [hotelPublicId, mode, listAttempt])

  const reloadList = useCallback(() => {
    setListAttempt((n) => n + 1)
  }, [])

  /* --- running one request -------------------------------------------------------------- */

  /**
   * Runs one request under the in-flight guard and the hotel generation.
   *
   * `work` receives the abort signal and a `current()` check; it applies its result only while
   * `current()` holds. Failures are recorded here, once, with the context that says what a 404
   * means.
   */
  const run = useCallback(
    async (
      action: PendingAction,
      context: CopilotFailureState['context'],
      work: (signal: AbortSignal, current: () => boolean) => Promise<void>,
      question: string | null = null,
    ): Promise<boolean> => {
      if (inFlight.current || hotelPublicId === null) {
        return false
      }
      inFlight.current = true
      const mine = generation.current
      const current = () => mine === generation.current
      const request = new AbortController()
      controller.current = request
      setPending(action)
      setPendingQuestion(question)
      setFailure(null)

      try {
        await work(request.signal, current)
        return current()
      } catch (cause: unknown) {
        if (!current() || request.signal.aborted) {
          return false
        }
        const error = asApiError(cause)
        setFailure({ error, context })
        if (error.code === 'LLM_DISABLED') {
          setDisabled(true)
        }
        return false
      } finally {
        // A request from a previous hotel must not release the guard of the current one.
        if (current()) {
          inFlight.current = false
          controller.current = null
          setPending(null)
          setPendingQuestion(null)
        }
      }
    },
    [hotelPublicId],
  )

  /* --- asking ----------------------------------------------------------------------------- */

  const ask = useCallback(
    async (question: string): Promise<boolean> => {
      const trimmed = question.trim()
      if (trimmed === '' || disabled || hotelPublicId === null) {
        return false
      }
      const hotel = hotelPublicId

      if (mode === 'one-off') {
        return run(
          'asking',
          'question',
          async (signal, current) => {
            const answer: unknown = await copilotService.ask(hotel, trimmed, signal)
            if (!isAnswer(answer)) {
              throw malformed()
            }
            if (current()) {
              setOneOff((previous) => [...previous, fromAnswer(nextKey(), trimmed, answer)])
            }
          },
          trimmed,
        )
      }

      const open = conversation
      if (open === null) {
        return run(
          'asking',
          'question',
          async (signal, current) => {
            const response: unknown = await copilotService.startConversation(hotel, trimmed, signal)
            if (!isTurnResponse(response)) {
              throw malformed()
            }
            if (current()) {
              setConversation({
                publicId: response.conversation_public_id,
                exchanges: [fromTurn(nextKey(), response.turn)],
                turnsRemaining: response.turns_remaining,
                expiresAt: null,
              })
              setListAttempt((n) => n + 1)
            }
          },
          trimmed,
        )
      }

      return run(
        'asking',
        'conversation',
        async (signal, current) => {
          const response: unknown = await copilotService.continueConversation(
            hotel,
            open.publicId,
            trimmed,
            signal,
          )
          if (!isTurnResponse(response)) {
            throw malformed()
          }
          if (current()) {
            setConversation((previous) =>
              previous !== null && previous.publicId === response.conversation_public_id
                ? {
                    ...previous,
                    exchanges: [...previous.exchanges, fromTurn(nextKey(), response.turn)],
                    turnsRemaining: response.turns_remaining,
                  }
                : previous,
            )
            setListAttempt((n) => n + 1)
          }
        },
        trimmed,
      )
    },
    [conversation, disabled, hotelPublicId, mode, nextKey, run],
  )

  /* --- the conversation list's actions ---------------------------------------------------- */

  const openConversation = useCallback(
    async (publicId: string): Promise<void> => {
      if (hotelPublicId === null) {
        return
      }
      const hotel = hotelPublicId
      await run('opening', 'conversation', async (signal, current) => {
        const transcript: unknown = await copilotService.readConversation(hotel, publicId, signal)
        if (!isTranscript(transcript)) {
          throw malformed()
        }
        if (current()) {
          setConversation({
            publicId: transcript.public_id,
            exchanges: transcript.turns.map((turn) => fromStoredTurn(nextKey(), turn)),
            turnsRemaining: transcript.turns_remaining,
            expiresAt: transcript.expires_at,
          })
        }
      })
    },
    [hotelPublicId, nextKey, run],
  )

  const deleteConversation = useCallback(
    async (publicId: string): Promise<boolean> => {
      if (hotelPublicId === null) {
        return false
      }
      const hotel = hotelPublicId
      const deleted = await run('deleting', 'conversation', async (_signal, current) => {
        await copilotService.deleteConversation(hotel, publicId)
        if (current()) {
          setConversation((previous) => (previous?.publicId === publicId ? null : previous))
        }
      })
      // Re-read either way: after a 404 the list is stale too.
      setListAttempt((n) => n + 1)
      return deleted
    },
    [hotelPublicId, run],
  )

  const newConversation = useCallback(() => {
    if (inFlight.current) {
      return
    }
    setConversation(null)
    setFailure(null)
  }, [])

  const setMode = useCallback((next: CopilotMode) => {
    if (inFlight.current) {
      return
    }
    setModeState(next)
    setFailure(null)
  }, [])

  const dismiss = useCallback(() => {
    setFailure(null)
  }, [])

  return {
    mode,
    setMode,
    oneOff,
    conversation,
    list,
    pending,
    pendingQuestion,
    failure,
    disabled,
    ask,
    newConversation,
    openConversation,
    deleteConversation,
    reloadList,
    dismiss,
  }
}
