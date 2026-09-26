"""create copilot_conversations and copilot_messages, the copilot's multi-turn memory

Authorised in Stage 7.11 for exactly this: **two new tables**, their indexes and their guard
triggers. No existing table is altered, and no existing constraint, index or trigger is touched.

1. **`copilot_conversations`** -- one row per conversation: the hotel, its creator, how many
   turns it holds (1-20: an empty conversation cannot exist, and none is unbounded), the next
   citation label its next turn starts at, and when it was last used. Ownership is the table's
   domain: every read is filtered by hotel AND creator in SQL. The identity columns never change
   (trigger); the counters and `last_activity_at` never decrease (trigger).
2. **`copilot_messages`** -- one row per stored turn: the caller's question, the answer the copilot
   SERVED (empty when withheld), how the turn ended, its document evidence and citations, the
   prompt it ran under, how many earlier turns it was shown, and the request id that correlates
   it with its `llm_invocations` row and `tool.invoked` events. No tool call or tool result is
   stored. A turn is immutable (trigger refuses UPDATE); DELETE is allowed, because a conversation
   is deleted with its turns (composite FK, ON DELETE CASCADE) -- by its creator, or by retention.

The composite foreign key `(conversation_id, hotel_id)` makes a turn in another hotel's
conversation impossible, as every cross-row reference in this schema is.

**Retention** (`copilot_conversation_retention_days`, default 30) is applied by the application
in every query and by a bounded purge; it is not a database default because it is a setting.

Revision ID: 0015_copilot_conversations
Revises: 0014_hotel_documents
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_copilot_conversations"
down_revision: str | None = "0014_hotel_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copilot_conversations (
            id BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            hotel_id BIGINT NOT NULL,
            actor_user_id BIGINT NOT NULL,
            turn_count INTEGER NOT NULL,
            next_source_label INTEGER DEFAULT 1 NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            last_activity_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_copilot_conversations PRIMARY KEY (id),
            CONSTRAINT uq_copilot_conversations_public_id UNIQUE (public_id),
            CONSTRAINT uq_copilot_conversations_id_hotel_id UNIQUE (id, hotel_id),
            CONSTRAINT fk_copilot_conversations_hotel_id_hotels
                FOREIGN KEY (hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT,
            CONSTRAINT fk_copilot_conversations_actor_user_id_users
                FOREIGN KEY (actor_user_id) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_copilot_conversations_turn_count_bounded
                CHECK (turn_count BETWEEN 1 AND 20),
            CONSTRAINT ck_copilot_conversations_next_source_label_positive
                CHECK (next_source_label >= 1),
            CONSTRAINT ck_copilot_conversations_activity_after_creation
                CHECK (last_activity_at >= created_at)
        )
        """
    )
    # "This person's conversations at this hotel, most recent first" -- and the per-hotel purge.
    op.execute(
        "CREATE INDEX ix_copilot_conversations_owner_last_activity "
        "ON copilot_conversations (hotel_id, actor_user_id, last_activity_at)"
    )
    # The purge across every hotel.
    op.execute(
        "CREATE INDEX ix_copilot_conversations_last_activity_at "
        "ON copilot_conversations (last_activity_at)"
    )
    op.execute(
        """
        CREATE FUNCTION copilot_conversations_guard() RETURNS trigger AS $$
        BEGIN
            IF (NEW.id, NEW.public_id, NEW.hotel_id, NEW.actor_user_id, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.public_id, OLD.hotel_id, OLD.actor_user_id, OLD.created_at) THEN
                RAISE EXCEPTION 'a copilot conversation''s identity is immutable'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.turn_count < OLD.turn_count
               OR NEW.next_source_label < OLD.next_source_label
               OR NEW.last_activity_at < OLD.last_activity_at THEN
                RAISE EXCEPTION 'a copilot conversation''s counters never decrease'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_copilot_conversations_guard "
        "BEFORE UPDATE ON copilot_conversations "
        "FOR EACH ROW EXECUTE FUNCTION copilot_conversations_guard()"
    )

    op.execute(
        """
        CREATE TABLE copilot_messages (
            id BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            conversation_id BIGINT NOT NULL,
            hotel_id BIGINT NOT NULL,
            turn INTEGER NOT NULL,
            question TEXT NOT NULL,
            answer TEXT DEFAULT '' NOT NULL,
            stop_reason TEXT NOT NULL,
            complete BOOLEAN NOT NULL,
            document_evidence TEXT NOT NULL,
            citations JSONB DEFAULT '[]'::jsonb NOT NULL,
            prompt_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            context_turns INTEGER NOT NULL,
            request_id TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_copilot_messages PRIMARY KEY (id),
            CONSTRAINT uq_copilot_messages_public_id UNIQUE (public_id),
            CONSTRAINT uq_copilot_messages_conversation_id_turn UNIQUE (conversation_id, turn),
            CONSTRAINT fk_copilot_messages_conversation_id_hotel_id
                FOREIGN KEY (conversation_id, hotel_id)
                REFERENCES copilot_conversations (id, hotel_id) ON DELETE CASCADE,
            CONSTRAINT ck_copilot_messages_turn_bounded CHECK (turn BETWEEN 1 AND 20),
            CONSTRAINT ck_copilot_messages_question_bounded
                CHECK (char_length(question) BETWEEN 1 AND 2000),
            CONSTRAINT ck_copilot_messages_answer_bounded
                CHECK (char_length(answer) <= 100000),
            CONSTRAINT ck_copilot_messages_stop_reason_valid CHECK (stop_reason IN (
                'completed', 'tool_failed', 'max_rounds', 'tool_call_cap', 'model_failed',
                'ungrounded_figures'
            )),
            CONSTRAINT ck_copilot_messages_complete_matches
                CHECK (complete = (stop_reason = 'completed')),
            CONSTRAINT ck_copilot_messages_document_evidence_valid CHECK (document_evidence IN (
                'none', 'cited', 'not_found', 'citation_rejected'
            )),
            CONSTRAINT ck_copilot_messages_citations_array
                CHECK (jsonb_typeof(citations) = 'array'),
            CONSTRAINT ck_copilot_messages_context_turns_bounded
                CHECK (context_turns BETWEEN 0 AND 19),
            CONSTRAINT ck_copilot_messages_context_before_turn CHECK (context_turns < turn),
            CONSTRAINT ck_copilot_messages_identity_bounded CHECK (
                char_length(prompt_id) BETWEEN 1 AND 64
                AND char_length(prompt_version) BETWEEN 1 AND 32
            ),
            CONSTRAINT ck_copilot_messages_request_id_shape
                CHECK (request_id IS NULL OR request_id ~ '^[A-Za-z0-9._-]{1,64}$')
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION copilot_messages_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'a copilot turn is immutable: UPDATE is not permitted'
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_copilot_messages_immutable "
        "BEFORE UPDATE ON copilot_messages "
        "FOR EACH ROW EXECUTE FUNCTION copilot_messages_immutable()"
    )


def downgrade() -> None:
    # Messages first: their foreign key points at conversations. The triggers go with their
    # tables; the functions are dropped explicitly.
    op.execute("DROP TABLE IF EXISTS copilot_messages")
    op.execute("DROP FUNCTION IF EXISTS copilot_messages_immutable()")
    op.execute("DROP TABLE IF EXISTS copilot_conversations")
    op.execute("DROP FUNCTION IF EXISTS copilot_conversations_guard()")
