"""The copilot conversation contracts (Stage 7.11). No internal key appears in any of them.

- **A question** -- the only thing a caller sends -- is exactly `/copilot/ask`'s: 1-2000
  characters after trimming, and no other field (`extra="forbid"`). No hotel, no conversation
  history, no prompt, no model: the hotel is the path, the history is the server's own record.
- **A turn just answered** (`ConversationTurn`) carries everything `/copilot/ask` returns, plus
  its turn number and how many earlier turns the model was shown.
- **A stored turn** (`StoredTurn`, in a transcript) carries what was stored: the question, the
  answer as served, how the turn ended, its document evidence and citations, its prompt identity
  and context size. Tool calls, tool results and the fixed notice were never stored, so they are
  not in a transcript; `complete` and `stop_reason` still say whether a turn was partial.
- **A summary** (`ConversationSummary`, in a list) carries the first 100 characters of the first
  question, so a caller can tell their conversations apart, and when each expires.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from app.models.copilot_conversation import MAX_TURNS
from app.schemas.copilot import (
    CopilotAskCreate,
    CopilotCitation,
    CopilotToolUse,
    DocumentEvidence,
    StopReason,
)


class ConversationQuestionCreate(CopilotAskCreate):
    """A question: the first of a new conversation, or the next of an existing one."""


class ConversationTurn(BaseModel):
    """A turn just answered, as the copilot answered it."""

    turn: int = Field(ge=1, le=MAX_TURNS)
    question: str
    answer: str = Field(
        description=(
            "The answer as served: the model's text, the fixed not-found sentence, or empty when "
            "withheld."
        )
    )
    complete: bool
    stop_reason: StopReason
    notice: str | None
    tools_used: list[CopilotToolUse]
    citations: list[CopilotCitation]
    document_evidence: DocumentEvidence
    context_turns: int = Field(
        description="How many earlier turns of this conversation the model was shown."
    )
    prompt_id: str
    prompt_version: str
    invocation_public_id: uuid.UUID


class ConversationTurnResponse(BaseModel):
    """What creating or continuing a conversation returns."""

    conversation_public_id: uuid.UUID
    turn: ConversationTurn
    turns_remaining: int = Field(
        ge=0,
        le=MAX_TURNS,
        description=f"How many more turns this conversation can hold (of {MAX_TURNS}).",
    )


class StoredTurn(BaseModel):
    """One turn of a transcript, as it was stored."""

    turn: int
    question: str
    answer: str
    complete: bool
    stop_reason: StopReason
    document_evidence: DocumentEvidence
    citations: list[CopilotCitation]
    context_turns: int
    prompt_id: str
    prompt_version: str
    created_at: dt.datetime


class ConversationSummary(BaseModel):
    """One of the caller's conversations, as a list shows it."""

    public_id: uuid.UUID
    created_at: dt.datetime
    last_activity_at: dt.datetime
    expires_at: dt.datetime = Field(
        description="When the conversation and every turn in it will be deleted, unless used again."
    )
    turn_count: int
    first_question_preview: str = Field(
        max_length=100, description="The first 100 characters of the first question."
    )


class ConversationTranscript(BaseModel):
    """A conversation and every stored turn, in turn order."""

    public_id: uuid.UUID
    created_at: dt.datetime
    last_activity_at: dt.datetime
    expires_at: dt.datetime
    turn_count: int
    turns_remaining: int
    turns: list[StoredTurn]


__all__ = [
    "ConversationQuestionCreate",
    "ConversationSummary",
    "ConversationTranscript",
    "ConversationTurn",
    "ConversationTurnResponse",
    "StoredTurn",
]
