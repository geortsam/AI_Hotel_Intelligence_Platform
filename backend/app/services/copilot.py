"""The copilot: one question in, one labelled answer out (Stage 7.7).

Architecture §4.1's `CopilotService`. It composes what Stages 7.5 and 7.6 built and adds three
things of its own -- the figure check, the accounting row, and the mapping to a response -- in
exactly this order:

    (a-c) the hotel is authorized and both budgets charged BEFORE this service runs, by the
          route's dependency chain (`app.api.deps.copilot_budget`)
    a.    the hotel is resolved again here, as every service resolves its own scope
    d.    the tools the caller's role permits      ToolInvocationService.permitted_tools
    e.    the deterministic catalogue of those     build_catalogue
    f.    copilot_answer@v1 rendered               the checksummed prompt registry
    g.    the bounded tool loop                    ToolLoop, its executor bound to THIS hotel
    h.    usage totals                             LoopResult.input_tokens / output_tokens
    i.    latency                                  an injected monotonic clock
    -     the figure check                         app.copilot.grounding
    j-l.  one llm_invocations row, committed; an integrity failure is this server's fault
    m.    the result mapped to the response, or a model failure with nothing done re-raised

## The hotel never comes from the model

The loop's executor is a closure over this method's `hotel_public_id` -- the path parameter the
caller was authorized for -- and over the names that were offered. A model's `ToolCall` supplies
a name and arguments; `ToolInvocationService.invoke` binds the hotel, checks the role, validates
the arguments (which have no field a hotel could travel in), runs the tool and audits it. Nothing
here reads a hotel from anything the model produced. The prompt says nothing about access.

## Partial answers are answers, labelled

Stage 7.6's loop stops in five ways and only `completed` is complete. Every other stop is a
200 with `complete: false`, the stop reason, and a fixed notice -- never a 500 or 502, because
the bound that stopped it is this server's policy working as designed, and the tool calls that
did succeed were real and audited.

**The one exception is a model failure before anything happened.** `model_failed` with no tool
outcomes means the model was never usable for this request: there is nothing partial to return.
The invocation is still recorded, and then the declared `LlmError` is re-raised so the client
gets §5.7's status and code (`503 LLM_DISABLED`, `503 LLM_UNAVAILABLE`, `429 LLM_RATE_LIMITED`,
`502 LLM_INVALID_RESPONSE`, `429 LLM_BUDGET_EXHAUSTED`). A model failure AFTER a tool ran is a
labelled partial, like the loop's other hard stops.

## Figures are checked, not trusted

The prompt asks for figures only from tool results; `ungrounded_figures` verifies it. An answer
containing a number no tool returned (and the caller did not write) is **withheld**: the
response carries no answer text, `complete: false` and, when the loop had otherwise completed,
`stop_reason: ungrounded_figures`. See `app.copilot.grounding` for exactly what is and is not
proven.

## What is stored, and what is sent away

One `llm_invocations` row per request: prompt identity, the upstream it was routed to, how the
loop ended, token and latency cost, the caller and the hotel. **No question, no answer, no prompt
text, no tool output.** The question and the tool results ARE sent to the configured external
provider -- that is what answering requires -- and that is stated in the API description rather
than left for a reader to infer.

## The transaction

Each tool call commits its own `tool.invoked` event as it happens (Stage 7.6). The invocation row
is staged and committed here, after the loop. An integrity failure at that commit can only be
this server's -- every column is built here, none from a request body -- so it is reported as
the generic internal fault, never attributed to the caller.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.copilot.catalogue import build_catalogue
from app.copilot.contracts import ToolOutcome
from app.copilot.grounding import ungrounded_figures
from app.copilot.loop import LoopResult, ToolLoop
from app.copilot.registry import ToolRegistry
from app.core.errors import internal_fault, sqlstate_of
from app.llm.base import Budget, ChatModel, ToolCall
from app.llm.prompts.registry import COPILOT_ANSWER_V1
from app.schemas.copilot import CopilotAnswerResponse, CopilotToolUse, StopReason
from app.services.llm_invocation_log import LlmInvocationLog
from app.services.scope import HotelScopeResolver
from app.services.tool_invocation import ToolInvocationService

logger = logging.getLogger(__name__)

#: What a partial answer's `notice` says, by stop reason. Fixed sentences: a notice that echoed
#: model text or a tool name would be a second, unchecked channel for content.
NOTICES: dict[str, str] = {
    "tool_failed": (
        "This answer is incomplete: the assistant stopped after two of its data lookups failed."
    ),
    "max_rounds": (
        "This answer is incomplete: the assistant reached its limit of lookup rounds before "
        "finishing."
    ),
    "tool_call_cap": (
        "This answer is incomplete: the assistant asked for more lookups at once than are "
        "allowed, so none of that step was run."
    ),
    "model_failed": (
        "This answer is incomplete: the assistant became unavailable after some lookups had "
        "already run."
    ),
    "ungrounded_figures": (
        "The answer was withheld because it contained figures that none of the data lookups "
        "returned."
    ),
}

#: Added to a partial answer's notice when its text was also withheld for ungrounded figures.
WITHHELD_SENTENCE = "Its text was withheld because it contained figures no lookup returned."


class CopilotService:
    """Answers one question about one hotel, bounded, audited and accounted for."""

    def __init__(
        self,
        session: Session,
        model: ChatModel,
        registry: ToolRegistry,
        invocations: ToolInvocationService,
        log: LlmInvocationLog,
        scope: HotelScopeResolver,
        *,
        budget: Budget,
        provider_name: str,
        model_name: str,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._session = session
        self._model = model
        self._registry = registry
        self._invocations = invocations
        self._log = log
        self._scope = scope
        self._budget = budget
        #: The upstream this deployment routes to, handed in by the composition root for the
        #: accounting row. Opaque strings here: this service never branches on them, never reads
        #: them off a response, and never returns them to a client.
        self._provider_name = provider_name
        self._model_name = model_name
        self._monotonic = monotonic

    def ask(self, hotel_public_id: uuid.UUID, question: str) -> CopilotAnswerResponse:
        """Answer *question* about the hotel the caller was authorized for. See the module."""
        hotel = self._scope.require_hotel(hotel_public_id)
        hotel_id = hotel.id

        offered = self._invocations.permitted_tools(hotel_public_id)
        catalogue = build_catalogue(self._registry, offered)
        messages = COPILOT_ANSWER_V1.render(question=question)

        def execute(call: ToolCall) -> ToolOutcome:
            # Bound to the path hotel and the offered names. The model supplies neither.
            return self._invocations.invoke(
                hotel_public_id, call.name, call.arguments, offered=offered
            )

        started = self._monotonic()
        result = ToolLoop(self._model).run(
            prompt_id=COPILOT_ANSWER_V1.prompt_id,
            prompt_version=COPILOT_ANSWER_V1.version,
            messages=messages,
            budget=self._budget,
            tools=catalogue,
            execute=execute,
        )
        latency_ms = max(0, round((self._monotonic() - started) * 1000))

        offending = ungrounded_figures(
            result.text,
            outputs=[o.output for o in result.outcomes if o.succeeded and o.output is not None],
            question=question,
        )
        stop_reason: StopReason = result.stop_reason
        if offending and result.complete:
            stop_reason = "ungrounded_figures"

        error = result.model_error if result.stop_reason == "model_failed" else None
        invocation_public_id = self._record(
            hotel_id, result, stop_reason, error.code if error is not None else None, latency_ms
        )
        self._observe(result, stop_reason, latency_ms, withheld=bool(offending))

        if error is not None and not result.outcomes:
            # Nothing partial exists to return. §5.7's declared status and code, unchanged.
            raise error

        return CopilotAnswerResponse(
            answer="" if offending else result.text,
            complete=stop_reason == "completed",
            stop_reason=stop_reason,
            notice=self._notice(stop_reason, withheld=bool(offending)),
            tools_used=[
                CopilotToolUse(tool=outcome.tool, outcome=outcome.outcome)
                for outcome in result.outcomes
            ],
            prompt_id=COPILOT_ANSWER_V1.prompt_id,
            prompt_version=COPILOT_ANSWER_V1.version,
            invocation_public_id=invocation_public_id,
        )

    # --- internals ----------------------------------------------------------------------------

    def _record(
        self,
        hotel_id: int,
        result: LoopResult,
        stop_reason: str,
        error_code: str | None,
        latency_ms: int,
    ) -> uuid.UUID:
        """Stage the accounting row and commit it. Returns the row's public id."""
        try:
            row = self._log.record(
                hotel_id=hotel_id,
                prompt_id=result.prompt_id,
                prompt_version=result.prompt_version,
                provider=self._provider_name,
                model=self._model_name,
                stop_reason=stop_reason,
                error_code=error_code,
                rounds=result.rounds,
                model_calls=result.model_calls,
                tool_calls=len(result.outcomes),
                tool_failures=result.failures,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=latency_ms,
            )
            public_id = row.public_id
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        except Exception:
            self._session.rollback()
            raise
        return public_id

    @staticmethod
    def _translate(exc: IntegrityError) -> Exception:
        """Every column of the row is built by this server, so a violation is this server's.

        Reported as the shared internal fault -- a 500 naming no relation or constraint -- and
        never as a conflict the caller could act on. No ``exc_info``: the driver renders the
        offending row into its message.
        """
        logger.warning("Copilot invocation integrity error (sqlstate=%s)", sqlstate_of(exc))
        return internal_fault(exc)

    @staticmethod
    def _notice(stop_reason: str, *, withheld: bool) -> str | None:
        if stop_reason == "completed":
            return None
        notice = NOTICES[stop_reason]
        if withheld and stop_reason != "ungrounded_figures":
            notice = f"{notice} {WITHHELD_SENTENCE}"
        return notice

    @staticmethod
    def _observe(result: LoopResult, stop_reason: str, latency_ms: int, *, withheld: bool) -> None:
        """One event per question: how it ended and what it cost. No text of any kind."""
        logger.info(
            "copilot %s after %s round(s), %s tool call(s), %s ms",
            stop_reason,
            result.rounds,
            len(result.outcomes),
            latency_ms,
            extra={
                "stop_reason": stop_reason,
                "rounds": result.rounds,
                "model_calls": result.model_calls,
                "tool_calls": len(result.outcomes),
                "tool_failures": result.failures,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": latency_ms,
                "answer_withheld": withheld,
                "prompt_id": result.prompt_id,
                "prompt_version": result.prompt_version,
            },
        )


__all__ = ["NOTICES", "WITHHELD_SENTENCE", "CopilotService"]
