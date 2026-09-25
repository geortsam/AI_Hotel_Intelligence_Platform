"""The copilot's HTTP contract (Stage 7.7). One request shape, one response shape.

## The request carries a question and nothing else

`CopilotAskCreate` has one field and refuses any other. No hotel field: the hotel is the path
segment the caller was authorized for. No prompt, model, provider, tool list or budget: each is
the server's decision. `extra="forbid"` makes an attempt to send one a 422 rather than a field
quietly ignored.

## The response labels what it is

A copilot answer is either complete or it is not, and the response says which in two fields that
cannot disagree: `complete` and `stop_reason`. When it is not complete, `notice` is a fixed
sentence saying so, so a client that renders only `answer` and `notice` still cannot present a
partial answer as a finished one.

`tools_used` names each tool that ran by its REGISTERED name and says how it ended; a call to a
name no tool has is reported as `null`, never as the text the model wrote. No figure appears in
the response that is not inside `answer`, and no provider name, model name, key, token count or
tenant identifier appears at all. `invocation_public_id` names the stored `llm_invocations` row,
so an answer can be quoted in a support request.

## Citations (Stage 7.10, additive)

`citations` lists the document excerpts the served `answer` cites, in order of first citation:
each with the `[S1]`-style label written in the answer, the chunk's and the document's
`public_id`, the document title and its version. Every one was returned by a document search in
THIS request, for THIS hotel, from an active version -- the service resolves each label against
the request's own ledger and never trusts what the model wrote. `document_evidence` says how the
answer relates to the hotel's documents; `not_found` and `citation_rejected` both replace the
answer with the fixed "Not found in this hotel's documents." (or withhold a partial one). No new
stop reason exists for either -- the persisted vocabulary is unchanged.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: The longest question accepted, in characters. Long enough for a real question; short enough
#: that a request cannot smuggle a document into the provider's context.
MAX_QUESTION_LENGTH = 2000

StopReason = Literal[
    "completed",
    "tool_failed",
    "max_rounds",
    "tool_call_cap",
    "model_failed",
    "ungrounded_figures",
]

#: How an answer relates to the hotel's documents. Closed, and never persisted.
#:
#: - `none`: no document search succeeded in this request, and the answer cites nothing.
#: - `cited`: the answer cites at least one excerpt, and every citation resolves.
#: - `not_found`: a document search succeeded but the answer cited none of its excerpts; the
#:   answer is replaced by the fixed not-found sentence (withheld, when partial).
#: - `citation_rejected`: the answer cited a source no search in this request returned; replaced
#:   (or withheld) the same way. A fabricated citation is not stripped and served around.
DocumentEvidence = Literal["none", "cited", "not_found", "citation_rejected"]

ToolOutcomeName = Literal[
    "succeeded",
    "unknown_tool",
    "forbidden",
    "invalid_arguments",
    "failed",
    "error",
]


class CopilotAskCreate(BaseModel):
    """A question about the hotel named in the path."""

    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_LENGTH),
    ] = Field(
        description=(
            f"The question, 1-{MAX_QUESTION_LENGTH} characters after trimming. It is sent to the "
            "configured language-model provider together with the results of the tools the "
            "model calls, and it is not stored."
        )
    )


class CopilotToolUse(BaseModel):
    """One tool call the model made while answering, and how it ended."""

    tool: str | None = Field(
        description="The registered tool name, or null when the model named no available tool."
    )
    outcome: ToolOutcomeName


class CopilotCitation(BaseModel):
    """One excerpt the answer cites, as the answer cites it and as this server stores it."""

    source: str = Field(description="The label written in the answer, e.g. S1.")
    chunk_public_id: uuid.UUID = Field(description="The cited chunk.")
    document_public_id: uuid.UUID = Field(description="The document version the chunk belongs to.")
    title: str = Field(description="That version's title.")
    version: int = Field(description="That version's number.")


class CopilotAnswerResponse(BaseModel):
    """The copilot's answer, labelled complete or partial."""

    answer: str = Field(
        description=(
            "The answer text. Empty when nothing could be answered; withheld (empty) when it "
            "contained a figure no tool returned. Never contains a figure no tool returned."
        )
    )
    complete: bool = Field(
        description="True only when the model finished answering within every bound."
    )
    stop_reason: StopReason = Field(
        description=(
            "How the request ended. `completed` is the only complete outcome; every other value "
            "labels a partial answer."
        )
    )
    notice: str | None = Field(
        description="A fixed sentence explaining a partial answer. Null when `complete` is true."
    )
    tools_used: list[CopilotToolUse] = Field(
        description="Every tool call the model made, in order, including the ones that failed."
    )
    prompt_id: str = Field(description="The registered prompt the answer was produced under.")
    prompt_version: str = Field(description="That prompt's version.")
    invocation_public_id: uuid.UUID = Field(
        description="Identifies the stored accounting record of this request."
    )
    citations: list[CopilotCitation] = Field(
        description=(
            "The document excerpts the answer cites, in order. Each was returned by a document "
            "search in this request, for this hotel, from an active version. Empty when the "
            "answer cites nothing or was replaced or withheld."
        )
    )
    document_evidence: DocumentEvidence = Field(
        description=(
            "How the answer relates to this hotel's documents: `none`, `cited`, `not_found` "
            "(a search ran but nothing was cited) or `citation_rejected` (the answer cited a "
            "source no search in this request returned)."
        )
    )


__all__ = [
    "MAX_QUESTION_LENGTH",
    "CopilotAnswerResponse",
    "CopilotAskCreate",
    "CopilotCitation",
    "CopilotToolUse",
    "DocumentEvidence",
    "StopReason",
    "ToolOutcomeName",
]
