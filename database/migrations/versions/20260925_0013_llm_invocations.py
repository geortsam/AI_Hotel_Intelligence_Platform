"""create llm_invocations, the content-free account of each copilot request

Authorised in Stage 7.7 for exactly this: **one new table**, its two indexes and its append-only
trigger. No existing table is altered, no existing constraint, index or trigger is touched, and
migrations 0001-0012 are unchanged.

**What a row is.** One ``POST /hotels/{id}/copilot/ask``: which hotel, which authenticated
caller, which prompt version, which upstream provider and model, how the bounded tool loop ended,
and what it cost in tokens and milliseconds. It is the attribution record architecture §5.3 asks
for and the cost account §4.5 needs.

**What a row can never hold.** No question, no answer, no prompt text, no tool output, no
model-supplied argument, no key and no credential: there is no column for any of them. The
answer is returned to the caller and not kept.

**Append-only, enforced by the database.** ``trg_llm_invocations_append_only`` raises on UPDATE
and DELETE, exactly as ``trg_audit_events_append_only`` does for the audit trail, with its own
function named for what it guards. TRUNCATE is neither, so the disposable test databases can
still be reset.

**The CHECK constraints describe the data, not the loop's policy.** Counts are non-negative,
failures never exceed calls, ``complete`` agrees with ``stop_reason``, and ``error_code`` is
present exactly when the model call failed. The loop's operational limits (3 rounds, 4 calls a
round, 2 failures) are application settings and are deliberately absent: encoding them here would
make changing a setting a schema change.

**ON DELETE RESTRICT on both foreign keys** -- the policy this schema uses for every historical
record (``audit_events``, ``demand_predictions``, ``bookings``). A property or user with
invocations cannot be deleted out from under them.

**Retention is deferred.** The repository's one retention mechanism archives ``audit_events``;
nothing here extends it. A retention rule for this table is recorded as a known limitation.

Revision ID: 0013_llm_invocations
Revises: 0012_audit_tool_invoked
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_llm_invocations"
down_revision: str | None = "0012_audit_tool_invoked"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE llm_invocations (
            id BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            hotel_id BIGINT NOT NULL,
            actor_user_id BIGINT NOT NULL,
            prompt_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            stop_reason TEXT NOT NULL,
            complete BOOLEAN NOT NULL,
            error_code TEXT,
            rounds INTEGER NOT NULL,
            model_calls INTEGER NOT NULL,
            tool_calls INTEGER NOT NULL,
            tool_failures INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            latency_ms INTEGER NOT NULL,
            request_id TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_llm_invocations PRIMARY KEY (id),
            CONSTRAINT uq_llm_invocations_public_id UNIQUE (public_id),
            CONSTRAINT fk_llm_invocations_hotel_id_hotels
                FOREIGN KEY (hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT,
            CONSTRAINT fk_llm_invocations_actor_user_id_users
                FOREIGN KEY (actor_user_id) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_llm_invocations_stop_reason_valid CHECK (stop_reason IN (
                'completed', 'tool_failed', 'max_rounds', 'tool_call_cap', 'model_failed',
                'ungrounded_figures'
            )),
            CONSTRAINT ck_llm_invocations_complete_matches
                CHECK (complete = (stop_reason = 'completed')),
            CONSTRAINT ck_llm_invocations_error_code_valid CHECK (
                ((stop_reason = 'model_failed') = (error_code IS NOT NULL))
                AND (error_code IS NULL OR error_code IN (
                    'LLM_DISABLED', 'LLM_UNAVAILABLE', 'LLM_RATE_LIMITED',
                    'LLM_INVALID_RESPONSE', 'LLM_BUDGET_EXHAUSTED'
                ))
            ),
            CONSTRAINT ck_llm_invocations_counts_non_negative CHECK (
                rounds >= 0 AND model_calls >= 0 AND tool_calls >= 0 AND tool_failures >= 0
            ),
            CONSTRAINT ck_llm_invocations_failures_within_calls
                CHECK (tool_failures <= tool_calls),
            CONSTRAINT ck_llm_invocations_usage_non_negative CHECK (
                input_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0
            ),
            CONSTRAINT ck_llm_invocations_identity_bounded CHECK (
                char_length(prompt_id) BETWEEN 1 AND 64
                AND char_length(prompt_version) BETWEEN 1 AND 32
                AND char_length(provider) BETWEEN 1 AND 32
                AND char_length(model) BETWEEN 1 AND 64
            ),
            CONSTRAINT ck_llm_invocations_request_id_shape
                CHECK (request_id IS NULL OR request_id ~ '^[A-Za-z0-9._-]{1,64}$')
        )
        """
    )

    # "What has this property's copilot cost?" and "what has this person asked for?", both in
    # time order. The two questions the table exists to answer; no other access pattern.
    op.execute(
        "CREATE INDEX ix_llm_invocations_hotel_id_created_at "
        "ON llm_invocations (hotel_id, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_llm_invocations_actor_user_id_created_at "
        "ON llm_invocations (actor_user_id, created_at)"
    )

    # --- append-only, as a database fact -------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION llm_invocations_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'llm_invocations is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_llm_invocations_append_only "
        "BEFORE UPDATE OR DELETE ON llm_invocations "
        "FOR EACH ROW EXECUTE FUNCTION llm_invocations_append_only()"
    )


def downgrade() -> None:
    # DROP TABLE is neither an UPDATE nor a DELETE, so the trigger cannot make this
    # irreversible. The trigger goes with the table; the function is dropped explicitly.
    op.execute("DROP TABLE IF EXISTS llm_invocations")
    op.execute("DROP FUNCTION IF EXISTS llm_invocations_append_only()")
