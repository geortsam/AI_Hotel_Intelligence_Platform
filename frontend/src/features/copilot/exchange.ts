import type {
  ConversationTranscript,
  ConversationTurn,
  ConversationTurnResponse,
  CopilotAnswerResponse,
  CopilotCitation,
  CopilotToolUse,
  DocumentEvidence,
  StopReason,
  StoredTurn,
} from '@/types/copilot'

/**
 * One question and its answer, as the screen shows it — from whichever of the three backend
 * shapes it came from.
 *
 * The one difference that matters is `recorded`. A `live` exchange was answered during this
 * visit and carries what the server sent with it: the `notice`, the tool calls and the
 * invocation reference. A `stored` exchange was read back from a transcript, and Stage 7.11
 * stored none of those three, so they are `null` here — not empty. An empty list would say
 * "no lookups were made"; `null` says "not recorded", which is the truth.
 */
export interface Exchange {
  /** Unique within the page, so citation panels and list keys do not collide. */
  readonly key: string
  readonly recorded: 'live' | 'stored'
  /** The conversation turn number, or null for a one-off question. */
  readonly turn: number | null
  readonly question: string
  readonly answer: string
  readonly complete: boolean
  readonly stopReason: StopReason
  /** The server's fixed notice. Null when complete, and always null for a stored turn. */
  readonly notice: string | null
  /** The tool calls, in order. Null for a stored turn: they were never recorded. */
  readonly toolsUsed: readonly CopilotToolUse[] | null
  readonly citations: readonly CopilotCitation[]
  readonly documentEvidence: DocumentEvidence
  /** Identifies the server's accounting record. Null for a stored turn. */
  readonly invocationPublicId: string | null
}

export function fromAnswer(key: string, question: string, answer: CopilotAnswerResponse): Exchange {
  return {
    key,
    recorded: 'live',
    turn: null,
    question,
    answer: answer.answer,
    complete: answer.complete,
    stopReason: answer.stop_reason,
    notice: answer.notice,
    toolsUsed: answer.tools_used,
    citations: answer.citations,
    documentEvidence: answer.document_evidence,
    invocationPublicId: answer.invocation_public_id,
  }
}

export function fromTurn(key: string, turn: ConversationTurn): Exchange {
  return {
    key,
    recorded: 'live',
    turn: turn.turn,
    question: turn.question,
    answer: turn.answer,
    complete: turn.complete,
    stopReason: turn.stop_reason,
    notice: turn.notice,
    toolsUsed: turn.tools_used,
    citations: turn.citations,
    documentEvidence: turn.document_evidence,
    invocationPublicId: turn.invocation_public_id,
  }
}

export function fromStoredTurn(key: string, turn: StoredTurn): Exchange {
  return {
    key,
    recorded: 'stored',
    turn: turn.turn,
    question: turn.question,
    answer: turn.answer,
    complete: turn.complete,
    stopReason: turn.stop_reason,
    notice: null,
    toolsUsed: null,
    citations: turn.citations,
    documentEvidence: turn.document_evidence,
    invocationPublicId: null,
  }
}

/** Whether a 200 body is the documented answer shape, before any component reads it. */
export function isAnswer(value: unknown): value is CopilotAnswerResponse {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const body = value as Partial<Record<keyof CopilotAnswerResponse, unknown>>
  return (
    typeof body.answer === 'string' &&
    typeof body.complete === 'boolean' &&
    typeof body.stop_reason === 'string' &&
    Array.isArray(body.tools_used) &&
    Array.isArray(body.citations) &&
    typeof body.document_evidence === 'string'
  )
}

/** Whether a 200 body is a conversation turn response. */
export function isTurnResponse(value: unknown): value is ConversationTurnResponse {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const body = value as { conversation_public_id?: unknown; turn?: unknown; turns_remaining?: unknown }
  return (
    typeof body.conversation_public_id === 'string' &&
    typeof body.turns_remaining === 'number' &&
    isAnswer(body.turn) &&
    typeof (body.turn as { turn?: unknown }).turn === 'number'
  )
}

/** Whether a 200 body is a transcript whose turns can be rendered. */
export function isTranscript(value: unknown): value is ConversationTranscript {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const body = value as { public_id?: unknown; turns?: unknown; turns_remaining?: unknown }
  return (
    typeof body.public_id === 'string' &&
    typeof body.turns_remaining === 'number' &&
    Array.isArray(body.turns) &&
    body.turns.every(
      (turn: unknown) =>
        typeof turn === 'object' &&
        turn !== null &&
        typeof (turn as { question?: unknown }).question === 'string' &&
        typeof (turn as { answer?: unknown }).answer === 'string' &&
        Array.isArray((turn as { citations?: unknown }).citations),
    )
  )
}
