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


__all__ = [
    "MAX_QUESTION_LENGTH",
    "CopilotAnswerResponse",
    "CopilotAskCreate",
    "CopilotToolUse",
    "StopReason",
    "ToolOutcomeName",
]
