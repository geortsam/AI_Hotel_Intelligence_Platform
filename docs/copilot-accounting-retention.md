# Copilot accounting retention (`llm_invocations`)

Issue 4. Migration `0017_llm_invocation_retention`. The contract the project owner approved, and
how an operator runs it.

## 1. The contract

| | |
|---|---|
| **What a record is** | One copilot question's content-free account: hotel, actor, prompt version, provider, model, stop reason, counts, tokens, latency, request id. Never a question, an answer, a prompt or a tool output (no column for any). |
| **How long it is kept** | `LLM_INVOCATION_RETENTION_DAYS` after its `created_at` — **365 by default**, the approved period. A day is exactly 24 hours, counted by PostgreSQL's clock, so neither the session's time zone nor a daylight-saving change moves the cut-off. |
| **Allowed values** | 1–3650 days, and never shorter than `COPILOT_CONVERSATION_RETENTION_DAYS` (a stored turn names its record by `request_id`). The bounds are technical guards; 365 is the policy. A value outside them stops the application and the command from starting. |
| **After expiry** | The record is **physically deleted** by the purge. Nothing is archived. Until the purge runs, an expired record is still in the table; nothing reads it. |
| **Immutable?** | Yes. UPDATE is refused by the database, always. |
| **Deletion** | Only of an expired record, only by a transaction that has declared the retention period to the database (`app.llm_invocation_retention_hours`, transaction-local), and never with a declared period under 24 hours. Every other DELETE is refused by `trg_llm_invocations_append_only`. TRUNCATE is unchanged (test databases only). |
| **Support reference** | `invocation_public_id` identifies its record for as long as the record exists: the retention period, then nothing. |
| **Hotel / user deletion** | Unchanged: `ON DELETE RESTRICT`. An unexpired record still blocks deleting its hotel or its actor; once the purge has removed the last one, this table no longer blocks it (`audit_events` still may). |
| **Conversations** | Independent. The conversation purge never touches this table; this purge never touches conversations, turns or the audit trail. |
| **Who runs it** | An operator, or the deployment's scheduler. No HTTP endpoint, no actor, no audit event, no model call, no copilot allowance. |
| **Automatic?** | No. The application schedules nothing. |

This is a deliberate difference from `audit_events`, which nothing deletes
(`app.services.retention`): an audit event is evidence of a business change; an accounting record
is a cost and attribution account whose owner chose a period.

## 2. Running the purge

```bash
docker compose run --rm api python -m app.jobs.purge_llm_invocations
```

It wraps `purge_expired_invocations(session, settings)` in `app.services.llm_invocation_retention`:
for each hotel holding an expired record, in id order, it deletes that hotel's oldest expired
records in batches of 1,000 until a batch finds fewer. It uses the application's own settings and
database configuration, so it runs from the API image as it is.

- **Output:** one line of counts — records deleted, hotels, batches, the retention in days.
  Never an identifier, a hotel, an actor, a request id or a connection string; on failure, only
  the exception's type.
- **Exit status:** 0 when the purge completed; 1 when it failed — including a configuration
  error, before anything is deleted; 2 for a usage error.
- **Failure and retry:** every batch is its own transaction and is scoped to one hotel. A failure
  leaves every earlier batch deleted and nothing half-deleted; running the command again finishes
  the work.
- **Safe to repeat:** a second run finds nothing and deletes nothing.
- **Observability:** the line above, and one log event (`app.services.llm_invocation_retention`)
  with `llm_invocations_purged`, `purge_hotels` and `purge_batches`.

**Scheduling is a deployment responsibility.** Run it periodically — **daily is the recommended
cadence** — from the host's cron, a systemd timer or the platform's scheduler, for example:

```
45 3 * * *  cd /srv/hotel-intelligence && docker compose run --rm api python -m app.jobs.purge_llm_invocations
```

How often it runs bounds how long a record outlives its retention period.

## 3. Backups

**Backups are not rewritten.** A backup taken before a record was purged still contains it until
that backup is itself deleted under the deployment's backup-retention policy
([deployment/backup-restore.md](deployment/backup-restore.md) §9, which sets none). The purge
bounds the live database, not copies of it. A restored backup can hold records older than the
retention period; the next purge run deletes them.

## 4. What pins it

- `tests/integration/test_llm_invocation_purge.py` — the boundary to the microsecond and the
  second, the configured period, every hotel and user, tenant scope, empty and clean tables,
  repetition, batching, failure and retry, every trigger refusal, the support reference, hotel and
  user deletion, conversation independence, privacy and the command.
- `tests/backend/test_llm_invocation_retention.py` — the setting's bounds, one declaring module,
  one purge, no HTTP surface, the 24-hour day, and a migration that replaced one function body.
- `tests/mutation/inventory.py` stage `I4` — fourteen deliberate breaks, each caught.
