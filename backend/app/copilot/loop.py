"""The bounded tool loop. §5.4's "bounded and explicit" orchestration, and §5.7 row 4.

Stage 7.6. The whole of it:

    ask the model (with the catalogue)
      ├─ it answers without tools ───────────────────────────► completed
      ├─ it asks for tools after MAX_TOOL_ROUNDS rounds ─────► max_rounds      (partial)
      ├─ it asks for more than MAX_CALLS_PER_ROUND at once ──► tool_call_cap   (partial)
      ├─ the model call raises a declared LLM failure ───────► model_failed    (partial)
      └─ run each call through `execute`, in order
           ├─ success ── result returned to the model
           ├─ 1st failure ── error returned to the model, flagged is_error
           └─ 2nd failure ───────────────────────────────────► tool_failed     (partial)
      └─ ask again

## Bounded by construction, not by a counter someone has to remember

The body runs inside `for _ in range(max_rounds + 1)`: at most `max_rounds` rounds of tools and
one final ask. There is no `while`, no recursion and no retry of a tool. Every branch of the body
either returns or continues to the next iteration, and the iteration that would exceed the bound
returns `max_rounds` instead of executing anything. The worst case is fixed before the loop
starts: `max_rounds + 1` model calls and `max_rounds * max_calls_per_round` tool calls.

## §5.7 row 4, exactly

"The tool's error is returned to the model once; a second failure ends the loop." Failures are
counted across the whole request, not per round and not per tool: the first is sent back to the
model as a `tool` message with `is_error=True` — never as a success, never as silence — and the
second ends the loop immediately. Calls later in the same round are **not** run, because the loop
has already decided to stop and running them would only spend and audit work nobody will read.

What counts as a failure is every outcome but `succeeded`: an unknown tool, a refused role,
arguments that failed validation, a typed service error and an unexpected fault alike. From the
model's side they are all "that did not work", and a model probing names or arguments gets one
free attempt, not an unlimited supply.

## Hard stops return a labelled partial result

§5.4: "a hard stop that returns a partial, labelled answer rather than looping". `LoopResult` is
that label: `complete` is True only for `completed`, `stop_reason` says which bound was hit, and
`outcomes` carries every invocation — failures included — so a caller cannot mistake a partial
result for an answer, and nothing that went wrong is hidden from it.

A declared LLM failure from the model call (timeout, rate limit, invalid output, budget, disabled,
circuit open) is a hard stop too. The loop does not retry it — the boundary already applied
§5.7's retries — and does not swallow it: the result is labelled `model_failed` and carries the
error itself, so the stage that adds an endpoint can raise it to get §5.7's status and code, or
return the partial work. That choice depends on the response contract the endpoint has, which
this stage does not define.

## What the loop does not do

It does not know which hotel it is working for, what the tools do, or who is asking. `execute`
is handed each `ToolCall` and returns a `ToolOutcome`; binding the hotel, checking the role,
validating the arguments, running the tool and auditing it is `ToolInvocationService`'s job. So
there is no path by which a model's output reaches a hotel id through this module: it never
holds one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.copilot.contracts import ToolOutcome
from app.llm.base import Budget, ChatModel, ChatRequest, Message, ToolCall, ToolSpec
from app.llm.errors import LlmError

logger = logging.getLogger(__name__)

#: §5.4: "a maximum number of tool rounds per request (proposed: 3)". Adopted as proposed.
MAX_TOOL_ROUNDS = 3
#: §5.4 asks for "a maximum number of tool calls per round" and names no number. Four is enough
#: for a model to fetch KPIs, a series, a breakdown and a forecast together, and small enough that
#: a model emitting a burst of calls is stopped before any of them runs. Recorded in Amendment A1,
#: when the catalogue had five tools; Stage 7.10's sixth does not move it.
MAX_CALLS_PER_ROUND = 4
#: §5.7 row 4: returned to the model once; the second failure ends the loop.
MAX_TOOL_FAILURES = 2

StopReason = Literal["completed", "tool_failed", "max_rounds", "tool_call_cap", "model_failed"]

#: Runs one call and reports how it ended. In production, `ToolInvocationService.invoke` bound
#: to the resolved hotel and the permitted names.
ToolExecutor = Callable[[ToolCall], ToolOutcome]


@dataclass(frozen=True, slots=True)
class LoopResult:
    """How the loop ended. `complete` is the label; everything else is the account of it."""

    stop_reason: StopReason
    #: The model's last text. The final answer when complete; whatever it had said so far when
    #: not, which a caller must present as partial if it presents it at all.
    text: str
    #: Tool rounds executed.
    rounds: int
    model_calls: int
    #: Every invocation, in order, failures included.
    outcomes: tuple[ToolOutcome, ...]
    prompt_id: str
    prompt_version: str
    #: The declared failure that stopped the loop, when `stop_reason == "model_failed"`.
    model_error: LlmError | None = None
    #: Stage 7.7, additive. Token usage summed over every model call that returned an answer,
    #: as the provider reported it. A call that raised reported nothing, so it adds nothing:
    #: these are the tokens this request is known to have spent, not an estimate of the rest.
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def complete(self) -> bool:
        return self.stop_reason == "completed"

    @property
    def failures(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.succeeded)


class ToolLoop:
    """Runs one request's tool loop against a `ChatModel`. Stateless between runs."""

    def __init__(
        self,
        model: ChatModel,
        *,
        max_rounds: int = MAX_TOOL_ROUNDS,
        max_calls_per_round: int = MAX_CALLS_PER_ROUND,
        max_failures: int = MAX_TOOL_FAILURES,
    ) -> None:
        if max_rounds < 0 or max_calls_per_round < 1 or max_failures < 1:
            raise ValueError("Loop bounds must be non-negative rounds and positive caps.")
        self._model = model
        self._max_rounds = max_rounds
        self._max_calls_per_round = max_calls_per_round
        self._max_failures = max_failures

    def run(
        self,
        *,
        prompt_id: str,
        prompt_version: str,
        messages: tuple[Message, ...],
        budget: Budget,
        tools: tuple[ToolSpec, ...],
        execute: ToolExecutor,
    ) -> LoopResult:
        history = list(messages)
        outcomes: list[ToolOutcome] = []
        failures = 0
        rounds = 0
        model_calls = 0
        input_tokens = 0
        output_tokens = 0
        text = ""

        def finish(reason: StopReason, error: LlmError | None = None) -> LoopResult:
            result = LoopResult(
                stop_reason=reason,
                text=text,
                rounds=rounds,
                model_calls=model_calls,
                outcomes=tuple(outcomes),
                prompt_id=prompt_id,
                prompt_version=prompt_version,
                model_error=error,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            self._observe(result)
            return result

        for _ in range(self._max_rounds + 1):
            request = ChatRequest(
                prompt_id=prompt_id,
                prompt_version=prompt_version,
                messages=tuple(history),
                budget=budget,
                tools=tools,
            )
            model_calls += 1
            try:
                response = self._model.complete(request)
            except LlmError as failed:
                return finish("model_failed", failed)
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            text = response.text

            if not response.tool_calls:
                return finish("completed")
            if rounds == self._max_rounds:
                return finish("max_rounds")
            if len(response.tool_calls) > self._max_calls_per_round:
                return finish("tool_call_cap")

            rounds += 1
            history.append(
                Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
            )
            for call in response.tool_calls:
                outcome = execute(call)
                outcomes.append(outcome)
                if not outcome.succeeded:
                    failures += 1
                    if failures >= self._max_failures:
                        return finish("tool_failed")
                history.append(
                    Message(
                        role="tool",
                        content=outcome.model_content(),
                        tool_call_id=call.id,
                        is_error=not outcome.succeeded,
                    )
                )

        # Unreachable: the final iteration either completes or returns `max_rounds`.
        raise AssertionError("the tool loop exceeded its own bound")

    @staticmethod
    def _observe(result: LoopResult) -> None:
        """One event per run: the stop reason and four counts. No content, no names, no hotel."""
        logger.info(
            "tool loop %s after %s round(s), %s model call(s), %s failure(s)",
            result.stop_reason,
            result.rounds,
            result.model_calls,
            result.failures,
            extra={
                "stop_reason": result.stop_reason,
                "rounds": result.rounds,
                "model_calls": result.model_calls,
                "tool_calls": len(result.outcomes),
                "tool_failures": result.failures,
                "prompt_id": result.prompt_id,
                "prompt_version": result.prompt_version,
            },
        )


__all__ = [
    "MAX_CALLS_PER_ROUND",
    "MAX_TOOL_FAILURES",
    "MAX_TOOL_ROUNDS",
    "LoopResult",
    "StopReason",
    "ToolExecutor",
    "ToolLoop",
]
