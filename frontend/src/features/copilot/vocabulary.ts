import type { ApiError } from '@/services/api/ApiError'
import type { DocumentStatus } from '@/types/knowledge'
import type { DocumentEvidence, StopReason, ToolOutcomeName } from '@/types/copilot'

/**
 * Every sentence the copilot screen says about an answer or a failure, written here.
 *
 * ## Fixed copy, chosen by code
 *
 * Nothing on this screen renders `error.message`, and nothing renders model text anywhere
 * except as the answer itself. Each refusal the backend can return has a stable code; the copy
 * below is selected by that code, so a message from a proxy, a gateway or a misbehaving
 * provider can never reach the page (see `services/api/failures.ts` for the same rule applied
 * to every other screen).
 *
 * ## Tool names are labels, not permissions
 *
 * `TOOL_LABELS` names the seven tools the backend registers at Stage 7.12, so the lookups a
 * response reports read as words rather than identifiers. It is a lookup table and nothing
 * more: it carries no role, it is never rendered as a catalogue, and the page shows a tool
 * only when a response says the model called it. Which tools the model is offered is decided
 * by the server from the caller's role, per request. A name this table does not know is shown
 * as the server sent it; it is never hidden and never guessed at.
 */

/** Readable names for the registered tools (`app.copilot.registry`, Stage 7.12). */
export const TOOL_LABELS: Readonly<Record<string, string>> = {
  get_hotel_kpis: 'Hotel KPIs for a period',
  get_daily_series: 'Daily figures for a period',
  get_revenue_breakdown: 'Revenue by category',
  get_demand_forecast: 'Demand forecast',
  get_forecast_accuracy: 'Measured forecast accuracy',
  search_hotel_knowledge: 'Search of this hotel’s documents',
  get_hotel_priorities: 'Attention list',
}

/** Shown for a call whose tool the server reported as `null`: a name no tool has. */
export const UNNAMED_TOOL_LABEL = 'A lookup that does not exist'

/** How one tool call ended, in words. */
export const OUTCOME_LABELS: Readonly<Record<ToolOutcomeName, string>> = {
  succeeded: 'Returned data',
  unknown_tool: 'Not an available lookup',
  forbidden: 'Refused',
  invalid_arguments: 'Refused: invalid request',
  failed: 'Failed',
  error: 'Failed unexpectedly',
}

/** Whether a tool call's outcome is a success, for the badge tone. */
export function outcomeSucceeded(outcome: ToolOutcomeName): boolean {
  return outcome === 'succeeded'
}

/**
 * The label every answer carries, whatever its state.
 *
 * Kept to what the backend actually checks. The copilot service verifies that every number in
 * a served answer was returned by one of that request's lookups; it does not verify that the
 * words around a number describe it correctly, and no real model's answers have been measured
 * here. So the label says the text is generated and says exactly what was and was not checked.
 */
export const GENERATED_LABEL = 'Generated'
export const GENERATED_NOTE =
  'Written by a language model. Every number in it was returned by one of this request’s data lookups; the wording around those numbers was not checked. Read it against the source figures before relying on it.'

/**
 * Why a STORED turn was incomplete.
 *
 * A transcript does not carry the server's `notice` (Stage 7.11 never stored it), so the
 * reason is said here from the stored `stop_reason`, which is a closed vocabulary.
 */
export const STORED_STOP_REASONS: Readonly<Record<Exclude<StopReason, 'completed'>, string>> = {
  tool_failed: 'This answer was incomplete: the assistant stopped after two of its data lookups failed.',
  max_rounds:
    'This answer was incomplete: the assistant reached its limit of lookup rounds before finishing.',
  tool_call_cap:
    'This answer was incomplete: the assistant asked for more lookups at once than are allowed.',
  model_failed:
    'This answer was incomplete: the assistant became unavailable after some lookups had run.',
  ungrounded_figures:
    'This answer was withheld because it contained figures that none of the data lookups returned.',
}

/** Shown for every stored turn in place of its lookups, which were never recorded. */
export const LOOKUPS_NOT_RECORDED =
  'Which data lookups ran for this earlier turn was not recorded. Only the question, the answer as shown and its citations are stored with a conversation.'

/** Shown when a live answer made no lookup at all. */
export const NO_LOOKUPS = 'No data lookups were made for this answer.'

/** Shown in place of an answer whose text is empty because it was withheld or never produced. */
export const WITHHELD_ANSWER = 'No answer text is shown.'

/** What the document-evidence outcome means, when it replaced or qualified the answer. */
export const EVIDENCE_EXPLANATIONS: Readonly<Partial<Record<DocumentEvidence, string>>> = {
  not_found:
    'The assistant searched this hotel’s documents, but its answer cited none of the excerpts it found, so the answer was replaced by the fixed sentence above.',
  citation_rejected:
    'The answer cited a source that no document search in this request returned, so it was replaced by the fixed sentence above rather than shown with the citation removed.',
}

/** A document version's status, as a citation panel marks it. */
export const DOCUMENT_STATUS_LABELS: Readonly<Record<DocumentStatus, string>> = {
  active: 'Current version',
  superseded: 'Superseded by a newer version',
  withdrawn: 'Withdrawn',
}

/* --- disclosures ------------------------------------------------------------------------ */

/** Shown before any question can be asked, in both modes. Mirrors the endpoints' own OpenAPI. */
export const PROVIDER_DISCLOSURE =
  'Your question, and the results of the data lookups the assistant makes to answer it, are sent to an external language-model provider. Nothing about any other hotel is included.'

export const ONE_OFF_DISCLOSURE =
  'A one-off question is not stored. The question and its answer are kept only on this page; the server keeps an accounting record of the request with no text in it.'

export const CONVERSATION_DISCLOSURE =
  'A conversation is stored: your questions and the answers as shown are kept with this hotel and deleted 30 days after the conversation was last used (the default retention; each conversation shows its own deletion date). Only you can read it, and you can delete it at any time.'

/* --- failures --------------------------------------------------------------------------- */

export interface CopilotFailure {
  readonly title: string
  readonly detail: string
  /** True only for LLM_DISABLED: no further input is offered for this visit. */
  readonly disablesInput: boolean
}

/** Whole minutes, rounded up, for a `Retry-After` in seconds. */
function describeWait(seconds: number): string {
  if (seconds < 60) {
    return `${seconds} second${seconds === 1 ? '' : 's'}`
  }
  const minutes = Math.ceil(seconds / 60)
  return `${minutes} minute${minutes === 1 ? '' : 's'}`
}

/**
 * What a refused or failed question says, by code. Never `error.message`.
 *
 * `context` distinguishes the one status whose meaning depends on what was asked: a 404 when
 * continuing or reading a conversation means the conversation is gone — expired, deleted, or
 * never the caller's — and the copy deliberately does not say which, because the server does
 * not either.
 */
export function describeCopilotFailure(
  error: ApiError,
  context: 'question' | 'conversation',
): CopilotFailure {
  const plain = (title: string, detail: string): CopilotFailure => ({
    title,
    detail,
    disablesInput: false,
  })

  switch (error.code) {
    case 'LLM_DISABLED':
      return {
        title: 'The copilot is switched off',
        detail:
          'This deployment has no language model enabled, so questions cannot be answered here. Nothing was sent to a provider. Asking again will not change this; it is enabled in the server’s configuration.',
        disablesInput: true,
      }
    case 'LLM_BUDGET_EXHAUSTED':
      return plain(
        'Question allowance used up',
        error.retryAfterSeconds === null
          ? 'This question needed more than a single request is allowed to use. A narrower question may fit.'
          : `The hourly allowance of copilot questions — yours or this hotel’s — is spent. It returns in about ${describeWait(error.retryAfterSeconds)}.`,
      )
    case 'LLM_RATE_LIMITED':
      return plain(
        'The language-model provider is limiting requests',
        'The provider refused the request for rate. Wait a little and ask again.',
      )
    case 'LLM_UNAVAILABLE':
      return plain(
        'The language-model provider could not be reached',
        'No answer was produced. Try again shortly.',
      )
    case 'LLM_INVALID_RESPONSE':
      return plain(
        'The provider’s answer could not be used',
        'The answer that came back was unusable, even after one retry, so none is shown. Try again, or rephrase the question.',
      )
    case 'CONVERSATION_FULL':
      return plain(
        'This conversation is full',
        'A conversation holds at most 20 turns. Start a new conversation to keep asking.',
      )
    default:
      break
  }

  if (error.status === 409) {
    return plain(
      'Another turn was added at the same time',
      'This conversation gained a turn while this question was being answered — perhaps from another tab — so this answer was not stored. Reopen the conversation to see its latest turns, then ask again.',
    )
  }
  if (error.status === 422) {
    return plain(
      'The question could not be accepted',
      'A question must be between 1 and 2,000 characters.',
    )
  }
  if (error.status === 404) {
    return context === 'conversation'
      ? plain(
          'This conversation is not available',
          'It could not be found. It may have expired or been deleted.',
        )
      : plain(
          'Hotel not available',
          'This property could not be found, or your access to it has been removed.',
        )
  }
  if (error.status === 401) {
    return plain('Your session has ended', 'Please sign in again to continue.')
  }
  if (error.status === 504 || error.code === 'MALFORMED_RESPONSE') {
    return plain(
      'The answer did not arrive',
      'The request took longer than the connection allows, or its response could not be read. It may still have been answered on the server and counted against the hourly allowance; in a conversation, reopen it to see whether the turn was stored.',
    )
  }
  if (error.code === 'NETWORK_ERROR') {
    return plain('Could not reach the server', 'Check your connection and try again.')
  }
  return plain('The copilot is temporarily unavailable', 'No answer was produced. Try again shortly.')
}
