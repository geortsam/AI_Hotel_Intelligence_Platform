"""Test doubles for the `ChatModel` seam. §5.6: "no test in CI ever makes a network call".

Stage 7.5. These ship in `app/` rather than in `tests/` on purpose: they are part of the seam's
contract, every later stage's tests will want them, and a double that lives in one stage's test
directory is a double the next stage copies. `app.core.rate_limit` takes the same view of its
injectable clock.

## The four

| Double | Exists to exercise |
|---|---|
| `ScriptedModel` | the happy path, deterministically — orchestration and wiring tests |
| `RecordedModel` | a real provider exchange, replayed from a fixture — adapter tests |
| `FailingModel` | every declared failure of §5.7, raised on demand |
| `SlowModel` | a call that does not return, so the timeout can be observed rather than assumed |

§5.6 names the first three. `SlowModel` is the fourth and is not in that table: raising a timeout
is not the same event as *being* slow, and §5.7's timeout rule — "one retry with jitter, then 503"
— is a property of the boundary's deadline, which only a call that actually hangs can demonstrate.
`FailingModel(LlmUnavailableError)` proves the error propagates; `SlowModel` proves the deadline
is enforced. Both are worth having and they test different things. This divergence from §5.6 is
recorded in `docs/v2-roadmap.md` under Stage 7.5.

## None of them touches a network, a clock or a file

`SlowModel` blocks on a `threading.Event` with a timeout rather than calling `time.sleep`, so a
test can release it early and never waits for real. `RecordedModel` is handed its fixture as data
by the caller — it opens nothing, so there is no fixture path to get wrong and no I/O in a unit
test.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from app.llm.base import ChatRequest, ChatResponse, FinishReason, TokenUsage, ToolCall
from app.llm.errors import LlmError

#: What a double reports as the provider and model that answered. Deliberately not a real
#: vendor's name: a fixture that said "anthropic" would eventually be read as evidence that a
#: real call happened.
DOUBLE_PROVIDER = "double"
DOUBLE_MODEL = "double-model"


def response_for(
    request: ChatRequest,
    text: str,
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    finish_reason: FinishReason = "stop",
    provider: str = DOUBLE_PROVIDER,
    model: str = DOUBLE_MODEL,
    tool_calls: tuple[ToolCall, ...] = (),
) -> ChatResponse:
    """A well-formed `ChatResponse` for a request, with no clock read.

    `latency_ms` is zero rather than measured: a double did not take any time, and reporting a
    plausible-looking duration would put a fabricated number where tests compare exact values.
    The boundary overwrites it with the real elapsed time anyway.

    The prompt identity is carried back from the request, because that is the adapter's job and
    a double that dropped it would let a boundary bug through.
    """
    return ChatResponse(
        text=text,
        parsed=None,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        latency_ms=0.0,
        provider=provider,
        model=model,
        finish_reason=finish_reason,
        prompt_id=request.prompt_id,
        prompt_version=request.prompt_version,
        tool_calls=tool_calls,
    )


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    """One scripted answer that asks for tools (Stage 7.6).

    A plain string scripts a final text answer; a `ScriptedTurn` scripts a turn in which the model
    stops to request calls. The calls are whatever the test says — including a name no registry
    holds or arguments naming another property — because a double for untrusted output has to be
    able to produce hostile output.
    """

    tool_calls: tuple[ToolCall, ...]
    text: str = ""


class ScriptedModel:
    """Returns what it was told to, in order. The deterministic double.

    Given one text it answers with that text forever; given several it answers with each in
    turn, which is what a retry test needs — "fail, then succeed" is two scripted answers and
    not a mock framework. A `ScriptedTurn` in the sequence answers with tool calls instead.

    Every request is recorded, so a test can assert what the seam sent without the double
    having to know why.
    """

    def __init__(
        self,
        texts: str | Iterable[str | ScriptedTurn] = "ok",
        *,
        output_tokens: int = 5,
        finish_reason: FinishReason = "stop",
    ) -> None:
        self._texts: Iterator[str | ScriptedTurn] = (
            itertools.repeat(texts) if isinstance(texts, str) else iter(list(texts))
        )
        self._output_tokens = output_tokens
        self._finish_reason = finish_reason
        #: Every request this double was given, in order.
        self.calls: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        try:
            scripted = next(self._texts)
        except StopIteration:
            raise AssertionError(
                "ScriptedModel ran out of scripted answers: the code under test called it more "
                "times than the test expected, which is itself the finding."
            ) from None
        if isinstance(scripted, ScriptedTurn):
            return response_for(
                request,
                scripted.text,
                output_tokens=self._output_tokens,
                finish_reason="tool_use",
                tool_calls=scripted.tool_calls,
            )
        return response_for(
            request,
            scripted,
            output_tokens=self._output_tokens,
            finish_reason=self._finish_reason,
        )


class RecordedModel:
    """Replays a captured provider exchange. §5.6's double for adapter tests.

    The recording is passed in as data — request text to response text — so this class opens no
    file and a fixture lives wherever its test wants it. A request whose rendered user turn is
    not in the recording raises, rather than returning a default: a silent fallback would let an
    adapter test pass while sending something the recording never contained.
    """

    def __init__(self, exchanges: dict[str, str], *, output_tokens: int = 5) -> None:
        self._exchanges = dict(exchanges)
        self._output_tokens = output_tokens
        self.calls: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        key = request.messages[-1].content
        if key not in self._exchanges:
            raise AssertionError(
                f"RecordedModel has no recorded answer for {key!r}. The code under test sent "
                "something the recording does not cover."
            )
        return response_for(request, self._exchanges[key], output_tokens=self._output_tokens)


class FailingModel:
    """Raises a declared failure. The double for every row of §5.7 that is an exception.

    Takes the error *class* rather than an instance, so one double cannot be exhausted by being
    raised twice, and takes `succeed_after` so "fails once, then works" — the shape every retry
    rule in §5.7 has — is expressible without a second double.
    """

    def __init__(
        self,
        error: type[LlmError],
        *,
        succeed_after: int | None = None,
        text: str = "ok",
    ) -> None:
        self._error = error
        self._succeed_after = succeed_after
        self._text = text
        self.calls: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        if self._succeed_after is not None and len(self.calls) > self._succeed_after:
            return response_for(request, self._text)
        raise self._error()


class SlowModel:
    """Blocks until released or until the caller gives up. The double for the timeout rule.

    Waits on an `Event` rather than sleeping, for two reasons: a test can `release()` it and get
    its thread back immediately instead of waiting out a real duration, and the wait is
    interruptible, so a suite that fails mid-test does not hang.

    `block_seconds` is the ceiling on that wait — a safety net so a bug in a test cannot wedge
    the suite, not a duration any test is expected to spend.
    """

    def __init__(self, *, block_seconds: float = 30.0, text: str = "ok") -> None:
        self._block_seconds = block_seconds
        self._text = text
        self._gate = threading.Event()
        self.calls: list[ChatRequest] = []

    def release(self) -> None:
        """Let any blocked call -- and every later one -- return immediately."""
        self._gate.set()

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        self._gate.wait(timeout=self._block_seconds)
        return response_for(request, self._text)


__all__ = [
    "DOUBLE_MODEL",
    "DOUBLE_PROVIDER",
    "FailingModel",
    "RecordedModel",
    "ScriptedModel",
    "ScriptedTurn",
    "SlowModel",
    "response_for",
]
