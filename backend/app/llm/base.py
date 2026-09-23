"""The one seam every language-model call passes through. No vendor is named below this line.

Stage 7.5, implementing `docs/v2-architecture.md` §5.1. One protocol, in this module, that
business code depends on:

    application code
        |
    ChatModel.complete(ChatRequest) -> ChatResponse      <- this file
        |
    provider adapter                                     <- app/llm/providers/, the only
                                                            place a vendor SDK is imported

## What is deliberately absent from the request

`ChatRequest` carries **no provider name, no model name and no API key**. §5.1 is explicit that
those "are resolved from settings by the provider factory, so no service ever names a vendor". A
service that could pass a model name would be a service that has to know which models exist, and
replacing the provider would then mean editing every caller — which is the thing this seam is
for.

It also carries **no session, no connection, no SQL, no row and no tenant identifier**. That is
§4's security boundary and it is structural here rather than advisory: there is no field any of
them could travel in, so "the LLM cannot reach the database" is a property of the type rather
than a rule someone remembers. A test asserts the field set exactly, so a seventh field cannot
arrive without someone choosing it.

## What is deliberately absent for now

**No tool catalogue.** §5.1 lists one on `ChatRequest`, and Stage 7.6 is the stage that builds
the tool boundary. Adding the field now would mean shipping a parameter nothing can populate and
no adapter can translate, and the first real tool would almost certainly reshape it. The field is
7.6's to add, against a registry that exists.

**No streaming.** Nothing in V2's documented architecture streams, and a protocol with a second
method that no caller uses is a protocol with a second method to keep working.

## Why `Protocol` rather than an abstract base class

Structural typing, so a test double is a `ChatModel` by having the method rather than by
inheriting from this module. That keeps `app/llm/testing.py` free of a dependency on this file's
class hierarchy and means a caller cannot distinguish a double from an adapter by type — which is
exactly the property that makes the doubles worth having.

`@runtime_checkable` so a test can assert conformance, and nothing else uses `isinstance` here:
structural checks at runtime verify method *names* only, which is a reminder and not a contract.
The contract is the shared behavioural suite every adapter and double is run against.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

#: Who a message is from. Three, matching what every chat provider models; a fourth would be
#: this seam inventing a role that has to be mapped to something on the way out.
Role = Literal["system", "user", "assistant"]

#: Why a completion stopped. Normalised here so a caller never branches on a vendor's spelling:
#: an adapter maps whatever it was given onto exactly these, and `unknown` is the honest answer
#: for a value this seam has not seen rather than a guess at the nearest match.
FinishReason = Literal["stop", "length", "content_filter", "unknown"]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn. Content is text; this seam does not model images or documents."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class Budget:
    """The ceiling one request may spend. §5.1: "a `budget` (timeout, max tokens)".

    Both are required rather than defaulted. A default timeout is the one a caller forgets to
    think about, and the failure this seam exists to contain — §2: the LLM call "is slow,
    external, paid for, and can hang" — is precisely the one an absent deadline lets through.
    """

    #: Wall-clock seconds for one attempt. The retry that §5.7 mandates gets its own.
    timeout_seconds: float
    #: The most output tokens this request may produce.
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero.")


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Everything an adapter needs, and nothing it must not have.

    The prompt is identified by id **and** version, not by its text: the text is in the registry
    under that identity, and a response that records which version produced it can be attributed
    when behaviour changes. §5.3.
    """

    #: The registry identity of the prompt these messages were rendered from.
    prompt_id: str
    prompt_version: str
    #: Already rendered. This seam does no templating -- see `app.llm.prompts`.
    messages: tuple[Message, ...]
    budget: Budget
    #: A Pydantic model the answer must validate against, when structured output is wanted.
    #: `None` means a text answer. §5.5: a validation failure is a failure, never a coerced
    #: guess.
    response_schema: type[BaseModel] | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("A request must carry at least one message.")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What the provider said the call cost. Reported, never estimated here."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """What came back, and enough to account for it.

    `provider` and `model` name what actually answered. They are metadata for attribution and a
    log line, not a branch: a caller that switched on them would have re-coupled itself to the
    vendor this seam exists to hide, and an architecture test asserts no service reads them.
    """

    text: str
    #: The validated object, when the request declared a schema. `None` for a text answer.
    parsed: BaseModel | None
    usage: TokenUsage
    latency_ms: float
    provider: str
    model: str
    finish_reason: FinishReason
    #: The prompt identity this answer is attributable to, carried back from the request.
    prompt_id: str
    prompt_version: str
    #: Attempts the boundary made, including the first. 2 means the retry §5.7 mandates ran.
    attempts: int = 1
    #: When the answer was produced. Supplied rather than read from a clock here, so a test can
    #: assert an exact value and two identical runs are comparable.
    completed_at: dt.datetime | None = None
    #: Room for an adapter to record something vendor-shaped for an operator's log. Never read
    #: by application code; a test asserts no service touches it.
    diagnostics: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ChatModel(Protocol):
    """The whole contract. One method.

    Implementations may raise only the declared failures of `app.llm.errors` — the behavioural
    suite in the tests runs every adapter and every double against that rule, so a caller can
    write one set of `except` clauses and have it be complete.
    """

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Answer one request, or raise one of the declared failures."""
        ...


__all__ = [
    "Budget",
    "ChatModel",
    "ChatRequest",
    "ChatResponse",
    "FinishReason",
    "Message",
    "Role",
    "TokenUsage",
]
