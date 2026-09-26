/**
 * The copilot's HTTP contract, transcribed from `app.schemas.copilot` and
 * `app.schemas.copilot_conversation` (Stages 7.7, 7.10 and 7.11). Nothing here is invented:
 * every field is one the backend sends, and none it does not.
 *
 * Two shapes of turn exist because the backend stores less than it answers. A turn answered in
 * this visit carries `notice`, `tools_used` and `invocation_public_id`; a turn read back from a
 * stored transcript does not, because Stage 7.11 never stored them. The difference is kept in
 * the types rather than papered over with empty defaults, so a component cannot render "no
 * lookups were made" for a turn whose lookups were simply not recorded.
 */

/** How a request ended. `completed` is the only complete outcome. */
export type StopReason =
  | 'completed'
  | 'tool_failed'
  | 'max_rounds'
  | 'tool_call_cap'
  | 'model_failed'
  | 'ungrounded_figures'

/** How an answer relates to the hotel's documents. Closed, and never persisted server-side. */
export type DocumentEvidence = 'none' | 'cited' | 'not_found' | 'citation_rejected'

/** How one tool call ended. */
export type ToolOutcomeName =
  | 'succeeded'
  | 'unknown_tool'
  | 'forbidden'
  | 'invalid_arguments'
  | 'failed'
  | 'error'

/** One tool call the model made. `tool` is null when the model named no available tool. */
export interface CopilotToolUse {
  readonly tool: string | null
  readonly outcome: ToolOutcomeName
}

/** One document excerpt a served answer cites, as the server resolved it. */
export interface CopilotCitation {
  /** The label written in the answer, e.g. `S1`. */
  readonly source: string
  readonly chunk_public_id: string
  readonly document_public_id: string
  readonly title: string
  readonly version: number
}

/** `POST /hotels/{h}/copilot/ask` — one stateless answer. */
export interface CopilotAnswerResponse {
  readonly answer: string
  readonly complete: boolean
  readonly stop_reason: StopReason
  readonly notice: string | null
  readonly tools_used: readonly CopilotToolUse[]
  readonly prompt_id: string
  readonly prompt_version: string
  readonly invocation_public_id: string
  readonly citations: readonly CopilotCitation[]
  readonly document_evidence: DocumentEvidence
}

/** A conversation turn just answered, as the copilot answered it. */
export interface ConversationTurn {
  readonly turn: number
  readonly question: string
  readonly answer: string
  readonly complete: boolean
  readonly stop_reason: StopReason
  readonly notice: string | null
  readonly tools_used: readonly CopilotToolUse[]
  readonly citations: readonly CopilotCitation[]
  readonly document_evidence: DocumentEvidence
  readonly context_turns: number
  readonly prompt_id: string
  readonly prompt_version: string
  readonly invocation_public_id: string
}

/** What starting or continuing a conversation returns. */
export interface ConversationTurnResponse {
  readonly conversation_public_id: string
  readonly turn: ConversationTurn
  readonly turns_remaining: number
}

/** One turn of a stored transcript: no notice, no tool calls, no invocation reference. */
export interface StoredTurn {
  readonly turn: number
  readonly question: string
  readonly answer: string
  readonly complete: boolean
  readonly stop_reason: StopReason
  readonly document_evidence: DocumentEvidence
  readonly citations: readonly CopilotCitation[]
  readonly context_turns: number
  readonly prompt_id: string
  readonly prompt_version: string
  readonly created_at: string
}

/** One of the caller's conversations, as the list shows it. */
export interface ConversationSummary {
  readonly public_id: string
  readonly created_at: string
  readonly last_activity_at: string
  readonly expires_at: string
  readonly turn_count: number
  readonly first_question_preview: string
}

/** A conversation and every stored turn, in turn order. */
export interface ConversationTranscript {
  readonly public_id: string
  readonly created_at: string
  readonly last_activity_at: string
  readonly expires_at: string
  readonly turn_count: number
  readonly turns_remaining: number
  readonly turns: readonly StoredTurn[]
}
