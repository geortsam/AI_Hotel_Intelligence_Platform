"""The copilot's multi-turn memory: conversations and their turns (Stage 7.11, migration 0015).

## One conversation, one hotel, one creator

`CopilotConversation` belongs to exactly one hotel and one authenticated caller, and ownership is
the table's whole domain: every read is filtered by both, in SQL. Its identity -- id, public id,
hotel, creator, creation time -- never changes, and its counters and `last_activity_at` never
decrease (trigger `trg_copilot_conversations_guard`). It always holds 1 to 20 turns: an empty
conversation cannot exist, and none grows without bound.

`next_source_label` is where the next turn's citation labels start. Labels continue across the
conversation (turn 1 issues S1..S5, turn 2 starts at S6), so a label an earlier turn issued can
never name a different chunk in a later one.

## One row per turn

`CopilotMessage` is one stored turn: the caller's question and the answer the copilot SERVED --
empty when it was withheld -- with how the turn ended, its document evidence, the citations it
served, the prompt identity it ran under, how many earlier turns it was shown, and the request id
that ties it to its `llm_invocations` row and `tool.invoked` events. No tool call and no tool
result is stored. A turn is immutable (trigger `trg_copilot_messages_immutable`); it is deleted
only with its conversation, through the composite foreign key's ON DELETE CASCADE.

## Retention

Both tables hold free text, and both are covered by one rule: a conversation, and every turn in
it, expires `copilot_conversation_retention_days` after its last activity. The rule is applied
in SQL by every query, and expired rows are physically deleted by a bounded purge. See
`app.services.copilot_conversation`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_column
from app.models.audit import REQUEST_ID_SQL_PATTERN
from app.models.llm_invocation import LLM_STOP_REASONS

#: The most turns one conversation may hold. The 21st is refused with `CONVERSATION_FULL`.
MAX_TURNS = 20
#: A question's bounds: the copilot's own `MAX_QUESTION_LENGTH`, restated for the schema.
MAX_QUESTION_LENGTH = 2000
#: A served answer's upper bound, in characters.
MAX_ANSWER_LENGTH = 100_000

#: How a turn's answer relates to the hotel's documents -- the Stage 7.10 vocabulary.
DOCUMENT_EVIDENCE: tuple[str, ...] = ("none", "cited", "not_found", "citation_rejected")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class CopilotConversation(Base):
    """One caller's conversation with the copilot about one hotel."""

    __tablename__ = "copilot_conversations"

    id: Mapped[int] = pk_column()
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )
    #: The creator: the only caller who may read, continue or delete this conversation.
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_source_label: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    #: When the last turn was stored. Retention counts from here.
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_copilot_conversations_public_id"),
        # The composite-FK target for turns. Redundant as a business rule (id is already unique).
        UniqueConstraint("id", "hotel_id", name="uq_copilot_conversations_id_hotel_id"),
        CheckConstraint(f"turn_count BETWEEN 1 AND {MAX_TURNS}", name="turn_count_bounded"),
        CheckConstraint("next_source_label >= 1", name="next_source_label_positive"),
        CheckConstraint("last_activity_at >= created_at", name="activity_after_creation"),
        Index(
            "ix_copilot_conversations_owner_last_activity",
            "hotel_id",
            "actor_user_id",
            "last_activity_at",
        ),
        Index("ix_copilot_conversations_last_activity_at", "last_activity_at"),
    )


class CopilotMessage(Base):
    """One stored turn: a question, the answer served for it, and how the turn ended."""

    __tablename__ = "copilot_messages"

    id: Mapped[int] = pk_column()
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Carried so the foreign key to the conversation is composite: a turn in another hotel's
    #: conversation is refused by the database, not merely avoided by the application.
    hotel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: The answer as SERVED: the model's text, the fixed not-found sentence, or empty when
    #: withheld. Never a withheld text.
    answer: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    stop_reason: Mapped[str] = mapped_column(Text, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    document_evidence: Mapped[str] = mapped_column(Text, nullable=False)
    #: The citations served with the answer: label, chunk and document public ids, title,
    #: version. What a transcript shows; never offered to a later turn as evidence.
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    prompt_id: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    #: How many earlier turns the model was shown for this one, after the context budget.
    context_turns: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_copilot_messages_public_id"),
        # The ordering guarantee, and what a concurrent continuation collides on.
        UniqueConstraint(
            "conversation_id", "turn", name="uq_copilot_messages_conversation_id_turn"
        ),
        ForeignKeyConstraint(
            ["conversation_id", "hotel_id"],
            ["copilot_conversations.id", "copilot_conversations.hotel_id"],
            name="fk_copilot_messages_conversation_id_hotel_id",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"turn BETWEEN 1 AND {MAX_TURNS}", name="turn_bounded"),
        CheckConstraint(
            f"char_length(question) BETWEEN 1 AND {MAX_QUESTION_LENGTH}", name="question_bounded"
        ),
        CheckConstraint(f"char_length(answer) <= {MAX_ANSWER_LENGTH}", name="answer_bounded"),
        CheckConstraint(
            f"stop_reason IN ({_sql_list(LLM_STOP_REASONS)})", name="stop_reason_valid"
        ),
        CheckConstraint("complete = (stop_reason = 'completed')", name="complete_matches"),
        CheckConstraint(
            f"document_evidence IN ({_sql_list(DOCUMENT_EVIDENCE)})",
            name="document_evidence_valid",
        ),
        CheckConstraint("jsonb_typeof(citations) = 'array'", name="citations_array"),
        CheckConstraint(
            f"context_turns BETWEEN 0 AND {MAX_TURNS - 1}", name="context_turns_bounded"
        ),
        CheckConstraint("context_turns < turn", name="context_before_turn"),
        CheckConstraint(
            "char_length(prompt_id) BETWEEN 1 AND 64 AND char_length(prompt_version) BETWEEN 1 "
            "AND 32",
            name="identity_bounded",
        ),
        CheckConstraint(
            f"request_id IS NULL OR request_id ~ '{REQUEST_ID_SQL_PATTERN}'",
            name="request_id_shape",
        ),
    )


__all__ = [
    "DOCUMENT_EVIDENCE",
    "MAX_ANSWER_LENGTH",
    "MAX_QUESTION_LENGTH",
    "MAX_TURNS",
    "CopilotConversation",
    "CopilotMessage",
]
