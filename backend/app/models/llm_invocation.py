"""One copilot question, accounted for without its content (Stage 7.7).

A row says that a named caller asked the copilot something at a named hotel, which prompt
version and which upstream model handled it, how the bounded tool loop ended, and what it cost
-- tokens as the provider reported them, and wall-clock latency. It is the record §5.3 asks for
("every stored copilot answer references the prompt version that produced it") and the cost
account §4.5 needs, and nothing more.

## What is deliberately NOT here

No question text, no answer text, no prompt text, no tool output, no argument a model supplied,
no API key and no credential of any kind. The table has no column any of them could travel in,
and a test pins the column set. §4.4: free text in an operational record is a data-retention
liability, and the copilot's answer is returned to the caller -- not kept.

## Append-only, like the audit trail

A row describes something that already happened. ``trg_llm_invocations_append_only`` refuses
UPDATE and DELETE at the database, mirroring ``trg_audit_events_append_only``, and there is no
``updated_at``: a row that is never updated has no moment of last update.

## The CHECKs are the data's shape, not the loop's policy

Counts are non-negative, failures cannot exceed calls, ``complete`` agrees with
``stop_reason``, and ``error_code`` is present exactly when the model call failed. The tool
loop's OPERATIONAL limits -- 3 rounds, 4 calls a round, 2 failures -- are deliberately not
here: they are application policy, and a schema that encoded them would need a migration to
change a setting.

## Attribution

``provider`` and ``model`` name the upstream the request was ROUTED to, resolved from settings
by the composition root -- not read back off a response. They are recorded even when the call
failed, because "which upstream refused" is exactly what an operator attributing a failure
needs. They are never returned to a client.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_column
from app.models.audit import REQUEST_ID_SQL_PATTERN

#: How a copilot request ended. The five ways the Stage 7.6 tool loop stops, plus the one
#: verdict the copilot service adds: an answer whose figures no tool returned is withheld.
LLM_STOP_REASONS: tuple[str, ...] = (
    "completed",
    "tool_failed",
    "max_rounds",
    "tool_call_cap",
    "model_failed",
    "ungrounded_figures",
)

#: The declared failures a model call can end in. `LLM_TOOL_FAILED` is absent on purpose: the
#: loop reports a tool failure as a stop reason, never as a model error.
LLM_MODEL_ERROR_CODES: tuple[str, ...] = (
    "LLM_DISABLED",
    "LLM_UNAVAILABLE",
    "LLM_RATE_LIMITED",
    "LLM_INVALID_RESPONSE",
    "LLM_BUDGET_EXHAUSTED",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class LlmInvocation(Base):
    """One copilot request. Counts, identities and costs -- never content."""

    __tablename__ = "llm_invocations"

    id: Mapped[int] = pk_column()
    #: How the invocation is named outside the database -- returned to the caller as
    #: `invocation_public_id`, so an answer can be quoted without a BIGINT leaving the server.
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )

    #: The hotel the request path named and the scope resolver accepted. Never model-supplied.
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )
    #: The authenticated caller, bound at construction the way `AuditTrail` binds it.
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    #: The registry identity of the prompt that was rendered. Resolvable to its content.
    prompt_id: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    #: The upstream the request was routed to. See the module docstring.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)

    stop_reason: Mapped[str] = mapped_column(Text, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: The declared model failure, when `stop_reason = 'model_failed'`; NULL otherwise.
    error_code: Mapped[str | None] = mapped_column(Text)

    rounds: Mapped[int] = mapped_column(Integer, nullable=False)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Whole milliseconds, measured by the copilot service on a monotonic clock. An integer so
    #: the schema's rule that real numbers are not stored keeps its single named exception.
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The existing correlation id, so this row, the request's `tool.invoked` audit events and
    #: its log lines can be read together.
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_llm_invocations_public_id"),
        CheckConstraint(
            f"stop_reason IN ({_sql_list(LLM_STOP_REASONS)})", name="stop_reason_valid"
        ),
        CheckConstraint("complete = (stop_reason = 'completed')", name="complete_matches"),
        CheckConstraint(
            "((stop_reason = 'model_failed') = (error_code IS NOT NULL)) AND "
            f"(error_code IS NULL OR error_code IN ({_sql_list(LLM_MODEL_ERROR_CODES)}))",
            name="error_code_valid",
        ),
        CheckConstraint(
            "rounds >= 0 AND model_calls >= 0 AND tool_calls >= 0 AND tool_failures >= 0",
            name="counts_non_negative",
        ),
        CheckConstraint("tool_failures <= tool_calls", name="failures_within_calls"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0",
            name="usage_non_negative",
        ),
        CheckConstraint(
            "char_length(prompt_id) BETWEEN 1 AND 64 AND char_length(prompt_version) BETWEEN 1 "
            "AND 32 AND char_length(provider) BETWEEN 1 AND 32 AND char_length(model) BETWEEN 1 "
            "AND 64",
            name="identity_bounded",
        ),
        CheckConstraint(
            f"request_id IS NULL OR request_id ~ '{REQUEST_ID_SQL_PATTERN}'",
            name="request_id_shape",
        ),
        Index("ix_llm_invocations_hotel_id_created_at", "hotel_id", "created_at"),
        Index("ix_llm_invocations_actor_user_id_created_at", "actor_user_id", "created_at"),
    )


__all__ = ["LLM_MODEL_ERROR_CODES", "LLM_STOP_REASONS", "LlmInvocation"]
