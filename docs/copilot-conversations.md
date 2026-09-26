# Copilot conversations — Stage 7.11

> **What exists:** a hotel member can hold a multi-turn conversation with the copilot. Each turn
> is answered exactly as `/copilot/ask` answers a question -- same tools, same checks -- except
> that the model is also shown the conversation's earlier turns, as context. **What this is
> not:** evidence. Nothing from an earlier turn can ground a figure or back a citation in a later
> one. `/copilot/ask` is unchanged and remains stateless.

Specified by [v2-architecture.md](v2-architecture.md) §4.4 and Amendment A6; migration
`0015_copilot_conversations`.

---

## 1. Whose, and where

A conversation belongs to **one hotel and one caller** -- its creator. Any member of the hotel may
start one; only its creator may read, continue or delete it.

| Who asks | About someone's conversation at hotel A | Result |
|---|---|---|
| its creator | at A | allowed |
| another viewer of A, a manager, the owner | at A | **404** |
| a member of B only | at A, or at B with the same id | **404** |
| a non-member | at A | **404** -- before any allowance is charged |
| its creator, after losing membership of A | at A | **404** (the hotel's own) |
| anyone | after it expired | **404** |

All of these 404s are indistinguishable. Ownership is enforced in SQL: every read filters by the
resolved hotel AND the caller's `actor_user_id` AND retention in one WHERE clause. That is why the
conversation repository is on the architecture suite's `IDENTITY_AWARE` list -- ownership is this
table's domain.

## 2. The data model

| Table | One row per | Key columns |
|---|---|---|
| `copilot_conversations` | conversation | `public_id`, `hotel_id`, `actor_user_id`, `turn_count` (1-20), `next_source_label`, `created_at`, `last_activity_at` |
| `copilot_messages` | stored turn | `public_id`, `conversation_id` + `hotel_id` (composite FK, CASCADE), `turn` (1-20, unique per conversation), `question`, `answer` (as served), `stop_reason`, `complete`, `document_evidence`, `citations`, `prompt_id`, `prompt_version`, `context_turns`, `request_id` |

- **One row per turn**: the question and the answer as served, stored together or not at all.
- **No tool call and no tool result is stored**, and none is replayed.
- A turn is **immutable** (trigger). A conversation's identity never changes and its counters and
  `last_activity_at` never decrease (trigger).
- The composite foreign key makes a turn in another hotel's conversation impossible.
- Deleting a conversation deletes its turns (ON DELETE CASCADE).
- `request_id` ties a turn to its `llm_invocations` row and `tool.invoked` events. There is no
  foreign key to `llm_invocations`, and no existing table was altered.

## 3. The five operations

| | | |
|---|---|---|
| `POST …/copilot/conversations` | start: answers turn 1 | **201**; budgeted after membership |
| `GET …/copilot/conversations` | the caller's live conversations, most recently used first | 200 |
| `GET …/copilot/conversations/{c}` | the transcript, turns in order | 200 |
| `POST …/copilot/conversations/{c}/messages` | the next turn | **200**; budgeted after ownership |
| `DELETE …/copilot/conversations/{c}` | delete it and its turns, physically | 204 |

A turn's response is `conversation_public_id`, `turn` (turn number, question, answer, `complete`,
`stop_reason`, `notice`, `tools_used`, `citations`, `document_evidence`, `context_turns`, prompt
identity, `invocation_public_id`) and `turns_remaining`. A **transcript** returns what was stored
-- turn, question, answer as served, `complete`, `stop_reason`, `document_evidence`, `citations`,
`context_turns`, prompt identity, `created_at`. Tool calls and the fixed notice were never stored,
so they are not in a transcript. No internal key appears anywhere.

A continued turn is 200 rather than 201: it is not separately addressable -- it belongs to the
conversation, whose URL does not change.

## 4. A turn: stored whole, or not at all

1. The hotel is resolved (membership). Continuing: the caller's live conversation is resolved,
   and refused with **409 `CONVERSATION_FULL`** at 20 turns -- both **before** the budget.
2. The existing copilot budget is charged (20 per caller, 100 per hotel, per hour).
3. Up to 100 expired conversations at the hotel are purged.
4. The copilot answers under `copilot_conversation@v1`, shown this conversation's earlier turns.
5. The turn is stored and the conversation advanced, in one transaction.

A model failure (503/502/429) stores nothing: a new conversation is not created, and an existing
one gains no turn. A **partial** answer is a 200 and is stored, labelled. No lock is held while
the model runs: a turn inserts `turn_count + 1`, and when a concurrent turn got there first the
unique `(conversation_id, turn)` refuses it -- **409**, never a duplicate.

## 5. What the model is shown

For turn N: turns 1..N-1 **of this conversation only**, and only their questions and the answers
as served -- citation labels removed, a withheld answer shown as `(No answer was given.)`.

**The context budget:** at most **6** earlier turns and at most **12,000 characters**. Whole
turns are kept newest first; the first older turn that would exceed either bound is dropped with
everything older than it; no turn is cut in part; if the newest earlier turn alone exceeds 12,000
characters, no history is sent. The number of turns shown is returned and stored as
`context_turns`. The rule is a pure function (`app.copilot.history`) and deterministic.

Earlier turns are placed between the system turn and the current question as the user and
assistant turns they were. Nothing else from them -- no tool call, no tool result, no document
excerpt -- is ever replayed.

## 6. Earlier turns are never evidence

- **Figures**: a turn's answer may state only figures from that turn's own tool results or its own
  question. A figure the model repeats from an earlier answer is withheld (`ungrounded_figures`).
- **Citations**: each turn gets a fresh evidence ledger, numbered from the conversation's
  `next_source_label` -- turn 1 issues S1..S5, turn 2 starts at S6. A label an earlier turn
  issued never resolves in a later one, and never names a different chunk there; the model does
  not even see it, because labels are stripped from history.
- **Prompt**: `copilot_conversation@v1` (`27ac6991…a386`) is `copilot_answer@v2`'s text, unchanged,
  with one sentence added: *"Earlier turns are shown for context only; figures and source labels
  in them are not evidence — call the tools again in this turn."* v2 itself is unchanged and is
  still what `/copilot/ask` renders.

## 7. Retention, enforced

- **Rule:** a conversation and every turn in it expire `copilot_conversation_retention_days`
  (default **30**, allowed 1-365) after `last_activity_at`. A retention day is exactly 24 hours,
  in the database and in `expires_at` alike -- counted in hours, not calendar days, so the cut-off
  does not move by an hour across a daylight-saving change.
- **Enforced in every query:** list, read, continue and delete all require
  `last_activity_at > now() - retention`. An expired conversation is a 404 the moment it expires
  and never reaches a model. No scheduler is needed for this to hold.
- **Physically deleted:** every start and every continuation first purges up to 100 expired
  conversations at the same hotel (turns cascade). `CopilotConversationRetention.purge()` does
  the same across every hotel, for an operator or a job; there is no HTTP endpoint for it.
- **Covers every stored text:** questions and answers exist only in `copilot_messages`. Nothing is
  archived.

## 8. What is written where

| | question / answer text | per turn |
|---|---|---|
| `copilot_messages` | yes, until deletion or expiry | one row |
| `llm_invocations` | **never** (no column for it) | one row, as for `/copilot/ask` |
| `audit_events` | **never** | `tool.invoked` per tool call, as before |

Creating and deleting conversations is **not audited**: it is the caller's own content, not a
hotel business change, and a retention purge has no actor to attribute it to. No audit action was
added and no vocabulary was widened.

## 9. What is not claimed

- That the model uses history well, or that answers in a conversation are more accurate. No
  evaluation set covers multi-turn behaviour yet.
- Any sharing, any cross-hotel conversation, any unbounded history, any summarisation of dropped
  turns: none exists.
