import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type {
  ConversationSummary,
  ConversationTranscript,
  ConversationTurnResponse,
  CopilotAnswerResponse,
} from '@/types/copilot'

/**
 * The copilot calls this application makes: the stateless question of Stage 7.7 and the
 * conversation operations of Stage 7.11. Nothing else — no tool listing exists, and none is
 * needed: which tools the model is offered is the server's decision, made per request from
 * the caller's role, and the UI shows only the tools a response reports it used.
 *
 * ## The body is a question and nothing else
 *
 * Both POST bodies are exactly `{question}`. The backend refuses any other field with a 422
 * (`extra="forbid"`), so there is no hotel, prompt, model or tool list to send even by
 * accident. The hotel is the path segment, taken from the shell's hotel context.
 *
 * ## What each call costs, and what it stores
 *
 * * `ask` — charged against the hourly allowance; nothing is stored but an accounting record
 *   without text.
 * * `startConversation` / `continueConversation` — charged the same way; the question and the
 *   answer as served are stored with the conversation, which expires 30 days (by default)
 *   after it was last used.
 * * the reads and the delete are not charged.
 *
 * Every call throws `ApiError`. The copilot-specific codes (`LLM_DISABLED`,
 * `LLM_BUDGET_EXHAUSTED`, `LLM_RATE_LIMITED`, `LLM_UNAVAILABLE`, `LLM_INVALID_RESPONSE`,
 * `CONVERSATION_FULL`) are explained by the feature's own copy, never by the server message.
 */

/** From `app.services.copilot_conversation`. */
export const CONVERSATION_PAGE_SIZE = 20

/** The encoded path segment for one conversation. Public identifiers only. */
function conversationPath(hotelPublicId: string, conversationPublicId: string): string {
  return `/hotels/${hotelPublicId}/copilot/conversations/${encodeURIComponent(conversationPublicId)}`
}

export const copilotService = {
  /** One stateless question. Not stored. */
  ask(hotelPublicId: string, question: string, signal?: AbortSignal) {
    return api.post<CopilotAnswerResponse>(`/hotels/${hotelPublicId}/copilot/ask`, {
      body: { question },
      ...(signal ? { signal } : {}),
    })
  },

  /** Answers the first question and, if it was answered, stores the new conversation. */
  startConversation(hotelPublicId: string, question: string, signal?: AbortSignal) {
    return api.post<ConversationTurnResponse>(`/hotels/${hotelPublicId}/copilot/conversations`, {
      body: { question },
      ...(signal ? { signal } : {}),
    })
  },

  /** Answers the next question of one of the caller's own conversations. */
  continueConversation(
    hotelPublicId: string,
    conversationPublicId: string,
    question: string,
    signal?: AbortSignal,
  ) {
    return api.post<ConversationTurnResponse>(
      `${conversationPath(hotelPublicId, conversationPublicId)}/messages`,
      { body: { question }, ...(signal ? { signal } : {}) },
    )
  },

  /** The caller's own live conversations at this hotel, most recently used first. */
  listConversations(hotelPublicId: string, signal?: AbortSignal) {
    return api.get<Page<ConversationSummary>>(`/hotels/${hotelPublicId}/copilot/conversations`, {
      query: { page: 1, page_size: CONVERSATION_PAGE_SIZE },
      ...(signal ? { signal } : {}),
    })
  },

  /** One stored transcript. 404 for anything that is not the caller's own, live conversation. */
  readConversation(hotelPublicId: string, conversationPublicId: string, signal?: AbortSignal) {
    return api.get<ConversationTranscript>(conversationPath(hotelPublicId, conversationPublicId), {
      ...(signal ? { signal } : {}),
    })
  },

  /** Deletes the conversation and every turn in it, physically. 204. */
  deleteConversation(hotelPublicId: string, conversationPublicId: string) {
    return api.delete(conversationPath(hotelPublicId, conversationPublicId))
  },
} as const
