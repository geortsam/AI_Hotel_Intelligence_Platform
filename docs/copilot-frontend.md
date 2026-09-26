# Copilot front end — Stage 7.13

> The browser screen for the copilot built in Stages 7.7–7.12. **Front end only:** no backend
> operation, contract, prompt, tool, migration or dependency was added or changed.
> Architecture Amendment A8 (`v2-architecture.md` §8) summarises it; this document is the
> stage record.

## 1. What was built

A new navigation area, **Copilot** (`/copilot`), the last item of the existing Intelligence group.
It is lazy-loaded like every other screen after sign-in (`router/splitting.node.test.ts` pins it).

| Part | File | Role |
|---|---|---|
| Page | `pages/CopilotPage.tsx` | the frame, hotel states, mode, thread, failure, question box |
| State | `features/copilot/useCopilot.ts` | one-off thread, open conversation, list, in-flight guard, hotel boundary |
| Citation source | `features/copilot/useCitationSource.ts` | reads the cited document version |
| Display model | `features/copilot/exchange.ts` | one shape for a live answer, a live turn and a stored turn |
| Copy | `features/copilot/vocabulary.ts` | every sentence about an answer or a failure; tool labels |
| Components | `ExchangeView`, `ToolCallList`, `CitationList`, `QuestionForm`, `ModeSelector`, `ConversationList` | presentational, one CSS module each |
| Services | `services/copilot/copilotService.ts`, `services/knowledge/knowledgeService.ts` | the six copilot calls and the one document read, through the shared client |
| Types | `types/copilot.ts`, `types/knowledge.ts` | transcribed from the backend schemas |

### Two modes

| Mode | Endpoints | What is stored |
|---|---|---|
| **One-off question** (default) | `POST …/copilot/ask` | nothing but the server's text-free accounting record; the answer lives in page memory for this visit |
| **Conversation** (explicit choice) | the five `…/copilot/conversations` operations of Stage 7.11 | the questions and the answers as served, under the server's retention rule (30 days after last use by default), deletable at any time |

One-off is the default so nothing is stored unless the user chooses it. Each option states what it
stores in its own label; the disclosure above the question box repeats it for the selected mode.

## 2. What was deliberately not built

- **No backend change of any kind.** No `GET …/copilot/tools`, no "is the copilot enabled" route,
  no new field. Everything shown comes from the existing responses.
- **No stored tool calls.** A reopened transcript cannot show its lookups because Stage 7.11 never
  stored them; the screen says so instead of adding a migration (decision H2).
- **No streaming.** The server checks every figure and citation after the answer is complete and
  may withhold or replace it; a streamed answer would show text before that check.
- **No Markdown or HTML rendering**, no auto-linking, no new dependency.
- **No client-side authorization.** See §3.
- **Out of scope (decision H11):** a screen for the Stage 7.12 attention list
  (`…/intelligence/priorities`), and any document upload, versioning or withdrawal screen. The
  documents a citation opens are read-only here; documents are still uploaded through the API.
- **No screenshot of a generated answer.** No language model runs in this repository's demo, and
  a fixture dressed as a real answer would be a fabrication (decision H12).

## 3. Security and privacy decisions

| Rule | How it holds | Pinned by |
|---|---|---|
| The server's role-filtered catalogue is the only authority over tools | no role is known to the frontend and none is inferred; the screen shows only the `tools_used` a response reports and never lists the tool table | architecture test (no `HotelRole`, role predicates, role literals, catalogue iteration); feature test with a viewer's response |
| Model and document text never become markup | answers, questions and excerpts are React text nodes; line breaks by `white-space: pre-wrap` | architecture test (no `dangerouslySetInnerHTML`, `innerHTML`, `<a`, `href`, Markdown/sanitiser libraries; dependency list unchanged); feature tests with HTML, Markdown and URL payloads |
| Citations only from server metadata | a citation opens `GET …/documents/{document_public_id}` from the server's `citations`; `[S1]` in the answer is text, and no code parses it | architecture test; feature test (an `[S9]` in text gets no control; the request uses the citation's document id) |
| One HTTP seam | both services use `services/api/client.ts`; no `fetch`, `XMLHttpRequest`, `EventSource`, `WebSocket` or stream reader | architecture test |
| The body is exactly `{question}` | three asking calls, each `body: { question }`; the hotel is only the path | architecture test; feature tests on the recorded request bodies |
| The server's words never reach the screen | every failure is fixed copy chosen by code; no file reads `.message` | architecture test; feature tests assert the server's message text is absent |
| Nothing in browser storage | no `localStorage`, `sessionStorage`, IndexedDB, cookies or Cache API in the feature | architecture test; feature test asserts no question, answer or conversation id in either storage |
| Switching hotel clears the screen | a hotel change aborts any request, clears both threads, the open conversation, the list and the failure; a generation counter drops a late answer | feature tests (one-off and conversation) |
| Another hotel's or user's conversation is a 404 like any other | the 404 copy says only that the conversation could not be found and may have expired or been deleted | feature test forbids "another", "owner", "belong", "access" |
| One question at a time | a ref guard in the hook plus the disabled button | feature test submitting twice inside one React batch |
| No further input after `LLM_DISABLED` | the question box is replaced for the rest of the visit, in both modes; reading and deleting conversations still work | feature test |
| Disclosure before asking | the external-provider sentence, and the mode's storage sentence, sit above the question box | feature tests |

## 4. What each outcome looks like

Every answer — complete, partial, withheld, replaced, live or stored — carries a **Generated**
badge and one sentence: every number was returned by one of the request's lookups; the wording
around them was not checked. That is exactly what the server checks and no more.

| Outcome | Badge | Shown |
|---|---|---|
| `completed`, evidence `none`/`cited` | Complete | the answer, lookups, citations |
| partial (`tool_failed`, `max_rounds`, `tool_call_cap`, `model_failed`) | Incomplete | the answer so far and the server's fixed `notice` |
| `ungrounded_figures` | Withheld | "No answer text is shown." and the notice |
| evidence `not_found` | Replaced: nothing cited | the fixed not-found sentence and why it replaced the answer |
| evidence `citation_rejected` | Replaced: invalid citation | the same, with its own reason |

| Refusal | Copy (fixed) | Input afterwards |
|---|---|---|
| `503 LLM_DISABLED` | the copilot is switched off for this deployment | **none for this visit** |
| `429 LLM_BUDGET_EXHAUSTED` with `Retry-After` | the hourly allowance is spent; returns in about *n* minutes | offered |
| `429 LLM_BUDGET_EXHAUSTED` without `Retry-After` | the question needed more than one request may use | offered |
| `429 LLM_RATE_LIMITED` | the provider is limiting requests | offered |
| `503 LLM_UNAVAILABLE` | the provider could not be reached | offered |
| `502 LLM_INVALID_RESPONSE` | the provider's answer could not be used | offered |
| `409 CONVERSATION_FULL` | the conversation holds 20 turns; start a new one | offered in a new conversation |
| `409` other | another turn was stored at the same time; reopen and ask again | offered |
| `422` | a question must be 1–2,000 characters | offered |
| `404` | hotel not available, or conversation not available | offered |
| `504` or a non-JSON body | the answer did not arrive; it may still have been answered and counted | offered |

## 5. Conversations

- The list shows the caller's own live conversations at this hotel (first page, 20 most recently
  used), each with its turn count, last use and the date it will be deleted unless used again.
- **Reopen** reads the transcript. Stored turns carry no `notice`, `tools_used` or invocation
  reference, so each says *"Which data lookups ran for this earlier turn was not recorded"* —
  never "no lookups were made" — and an incomplete stored turn shows a fixed sentence for its
  stored `stop_reason`. Its citations still open their source.
- A continued turn is live: it shows its lookups and notice like a one-off answer.
- **Delete** asks first ("This cannot be undone"), then deletes the conversation and its turns.
- **New conversation** closes the open one without deleting it.

## 6. Tool display

`TOOL_LABELS` names the seven tools the backend registers at Stage 7.12 — `get_hotel_kpis`,
`get_daily_series`, `get_revenue_breakdown`, `get_demand_forecast`, `get_forecast_accuracy`,
`search_hotel_knowledge`, `get_hotel_priorities` — with readable labels, and the registered name
beside each. A name the table does not know is shown exactly as sent; a `null` tool (a name the
model invented) is "A lookup that does not exist". The table carries no role and is never rendered
as a list.

## 7. Testing

- `features/copilot/copilot.test.tsx` — behaviour at `fetch`: one-off answer, conversation start
  and continuation, generated label on every state, citation list and opening (superseded marker,
  verbatim excerpt, the right document, a failed read), visible tool calls in order including
  failures and unknown names, a viewer's response, every refusal code, 504/unreadable responses,
  partial, withheld, `not_found`, `citation_rejected`, transcript reopening with the not-recorded
  statement, continuation after reopening, deletion with confirmation and cancellation, a full
  conversation, a concurrent-turn conflict, a missing conversation, hotel switching in both
  modes, double submission, browser storage, and disclosure.
- `features/copilot/architecture.node.test.ts` — the source-level rules of §3, with a positive
  control and a full file list so a new file cannot escape the scan.
- `router/splitting.node.test.ts` — `CopilotPage` is loaded on demand.
- Mutation check: fourteen deliberate regressions (HTML rendering, a rendered tool catalogue, no
  hotel reset, no in-flight guard, input kept after `LLM_DISABLED`, the wrong citation document,
  stored turns claiming no lookups, browser storage, the server's message rendered, a stored turn
  without the Generated label, a continuation that starts a new conversation, delete without
  confirmation, a missing disclosure, a role check) — each was caught.

## 8. Known limitations

- **No real language model has been evaluated through this screen, or at all in this repository.**
  Nothing here asserts that answers are accurate, reliable or useful. The tests use fixtures in
  the backend's response shapes.
- **The proxy may give up first.** nginx proxies `/api/` with its default 60-second read timeout,
  while one copilot request can in the worst case run about four minutes (four model calls, each
  up to 30 seconds with one retry, plus lookups). A request cut off this way shows "The answer did
  not arrive" and warns that it may still have been answered and counted. No deployment
  configuration was changed (decision H8).
- **`LLM_DISABLED` is learned by asking.** No route says in advance whether the copilot is on, and
  by the dependency order a question to a disabled deployment is still charged to the hourly
  allowance; the screen stops offering input after the first refusal to avoid repeating that.
- **Stored turns cannot show their lookups** (§5).
- **The tool-label table can drift** from the backend registry. An unknown name is still shown
  verbatim, so drift degrades a label, not the truthfulness of the list.
- **The conversation list shows the first page only** (20 most recently used), and says so when
  there are more.
- **No document screen exists**, so in a deployment without uploaded documents there is nothing to
  cite.
- **The retention period in the disclosure is the default.** A deployment can configure 1–365
  days; each conversation shows its own deletion date, which is the authoritative figure.
