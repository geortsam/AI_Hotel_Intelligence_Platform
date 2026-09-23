"""Where the declared failure behaviour of §5.7 is applied, once, for every provider.

Stage 7.5. §2 names the thing this file exists for: the LLM call "is slow, external, paid for,
and can hang. That is an argument for a timeout, a budget and a circuit-breaker **inside** the
process". Two of those three are here. The third is not, and §5.7 is why — see "What is not
here" below.

    caller
       |
    GuardedChatModel.complete()      <- timeout, retry, budget, schema validation
       |
    ChatModel (an adapter, or a double)

It is itself a `ChatModel`, so it composes: a caller cannot tell a guarded provider from a bare
one, and nothing downstream has to know it is wrapped.

## Why the rules live here and not in each adapter

There are six declared failures and there will be more than one provider. Implemented per
adapter, "one retry with jitter, then 503" is a rule that has to be got right once per vendor and
drifts the moment one of them is written by someone reading a different paragraph. Implemented
here, an adapter's whole job is to translate: make the call, map the vendor's errors onto the
declared taxonomy, and return. A contract suite then runs every adapter against the same
behavioural expectations.

## The timeout is enforced, not requested

`Budget.timeout_seconds` is passed to the adapter, which is expected to hand it to its client —
but a client that ignores it, or hangs below its own deadline, would hang this process. So the
call is also run with a deadline **here**, on a worker thread, and abandoned when it expires.

The honest limitation: a Python thread cannot be killed. An abandoned call keeps running until
its own I/O returns, and this boundary stops waiting for it rather than stopping it. That bounds
the *caller's* latency, which is what the request needed, and it leaks a thread for the duration
of a hung call, which is recorded here rather than hidden. A process-level fix is a different
architecture (§2 decided against a second deployable) and a thread pool with a hard cap is the
mitigation a later stage should add if hung calls are ever observed.

## No clock is read for anything a caller can observe

`completed_at` is supplied by the caller's clock injection, and `latency_ms` comes from
`time.perf_counter`, which measures duration rather than telling the time. Two runs over the same
double therefore differ in exactly one field, and a test can pin everything else.

## What is not here: the circuit breaker

§2 calls for "a timeout, a budget and a circuit-breaker inside the process (§5.7)". §5.7
specifies the first two and **says nothing about the third**: no failure threshold, no window, no
open duration, no half-open probe, no error code for a call refused while open, and no reset
rule. It is not in the failure table, so it has no declared behaviour to implement.

It is therefore deliberately absent rather than guessed at. Inventing a threshold would put a
number in the runtime path that no document justifies and that every later stage would inherit as
though it had been decided. The gap is reported in `docs/v2-roadmap.md` under Stage 7.5 and is
the one piece of this stage's architecture that needs a decision before it can be built.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from pydantic import ValidationError as PydanticValidationError

from app.llm.base import ChatModel, ChatRequest, ChatResponse
from app.llm.errors import (
    LlmBudgetExhaustedError,
    LlmDisabledError,
    LlmInvalidResponseError,
    LlmRateLimitedError,
    LlmUnavailableError,
)

logger = logging.getLogger(__name__)

#: How many attempts a retryable failure gets in total, including the first. §5.7 says "one
#: retry" for a timeout and for a malformed structured output; two is that sentence as a number.
MAX_ATTEMPTS = 2

#: The jitter band, in seconds, before a retry. §5.7 says "one retry with jitter" and does not
#: give a range; this is small enough to be invisible to a caller and large enough to break the
#: lockstep that makes a provider's brief outage into a synchronised stampede.
JITTER_SECONDS = (0.05, 0.25)


class GuardedChatModel:
    """A `ChatModel` that applies §5.7 to whatever `ChatModel` it wraps.

    Every collaborator that touches time or randomness is injected, so the behaviour this class
    exists to have is testable without sleeping through it or flaking on it.
    """

    def __init__(
        self,
        inner: ChatModel,
        *,
        enabled: bool = True,
        max_output_tokens: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        monotonic: Callable[[], float] = time.perf_counter,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._inner = inner
        self._enabled = enabled
        #: The per-request ceiling of §4.5. `None` means the deployment set none.
        self._max_output_tokens = max_output_tokens
        self._sleep = sleep
        self._jitter = jitter
        self._monotonic = monotonic
        self._now = now

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Answer, or raise one of the six declared failures. Never anything else."""
        if not self._enabled:
            # Row 6. Checked before anything is spent, including the thread.
            raise LlmDisabledError()

        self._require_within_budget(request)

        started = self._monotonic()
        last_timeout: FutureTimeoutError | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._attempt(request)
            except FutureTimeoutError as expired:
                # Row 1: retry once with jitter, then 503. The retry is not attempted after the
                # final try, so a caller waits at most two budgets rather than three.
                last_timeout = expired
                if attempt < MAX_ATTEMPTS:
                    self._sleep(self._jitter(*JITTER_SECONDS))
                    continue
                break
            except LlmRateLimitedError:
                # Row 2: no retry, explicitly. Re-raised as-is.
                raise
            except PydanticValidationError as invalid:
                # Row 3: one retry, then 502. A second malformed answer is not a third chance.
                if attempt < MAX_ATTEMPTS:
                    self._sleep(self._jitter(*JITTER_SECONDS))
                    continue
                self._observe(request, "invalid_response", attempt)
                raise LlmInvalidResponseError() from invalid

            self._require_usage_within_budget(request, response)
            self._observe(request, "answered", attempt)
            return self._stamp(response, started=started, attempts=attempt)

        self._observe(request, "unavailable", MAX_ATTEMPTS)
        raise LlmUnavailableError() from last_timeout

    # --- one attempt --------------------------------------------------------------------------

    def _attempt(self, request: ChatRequest) -> ChatResponse:
        """Run the inner model under a hard deadline, and validate what comes back.

        The executor is created per attempt and not reused: a shared pool would let one hung
        call occupy a worker that a later request needs, which converts a slow provider into a
        stalled application -- the exact failure this boundary exists to prevent.
        """
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm")
        future: Future[ChatResponse] = executor.submit(self._inner.complete, request)
        try:
            response = future.result(timeout=request.budget.timeout_seconds)
        finally:
            # `cancel_futures` is irrelevant for a task already running and `wait=False` is the
            # point: shutdown must not block on the call this boundary just gave up on.
            executor.shutdown(wait=False)

        if request.response_schema is not None:
            # §5.5: validated, never coerced. A failure here is caught by `complete` above and
            # becomes row 3 after its one retry.
            parsed = request.response_schema.model_validate_json(response.text)
            return ChatResponse(
                text=response.text,
                parsed=parsed,
                usage=response.usage,
                latency_ms=response.latency_ms,
                provider=response.provider,
                model=response.model,
                finish_reason=response.finish_reason,
                prompt_id=response.prompt_id,
                prompt_version=response.prompt_version,
                attempts=response.attempts,
                completed_at=response.completed_at,
                diagnostics=response.diagnostics,
            )
        return response

    # --- budget -------------------------------------------------------------------------------

    def _require_within_budget(self, request: ChatRequest) -> None:
        """Row 5, before the call: a request may not ask for more than the deployment allows."""
        ceiling = self._max_output_tokens
        if ceiling is not None and request.budget.max_output_tokens > ceiling:
            raise LlmBudgetExhaustedError()

    def _require_usage_within_budget(self, request: ChatRequest, response: ChatResponse) -> None:
        """Row 5, after the call: a provider that overspent is a refusal, not a silent pass.

        §4.5: a ceiling that is hit produces "a refusal with a clear error code, never a silent
        degradation". Returning an answer that cost more than was authorised, on the grounds
        that it has already been paid for, is that silent degradation.
        """
        if response.usage.output_tokens > request.budget.max_output_tokens:
            raise LlmBudgetExhaustedError()
        ceiling = self._max_output_tokens
        if ceiling is not None and response.usage.output_tokens > ceiling:
            raise LlmBudgetExhaustedError()

    # --- finishing ----------------------------------------------------------------------------

    def _stamp(self, response: ChatResponse, *, started: float, attempts: int) -> ChatResponse:
        """Record what this boundary knows that the adapter could not: elapsed time, attempts."""
        return ChatResponse(
            text=response.text,
            parsed=response.parsed,
            usage=response.usage,
            latency_ms=(self._monotonic() - started) * 1000,
            provider=response.provider,
            model=response.model,
            finish_reason=response.finish_reason,
            prompt_id=response.prompt_id,
            prompt_version=response.prompt_version,
            attempts=attempts,
            completed_at=None if self._now is None else self._now(),
            diagnostics=response.diagnostics,
        )

    def _observe(self, request: ChatRequest, outcome: str, attempts: int) -> None:
        """One event per call. Five fields, and not one of them is content.

        No prompt text, no rendered message, no answer, no token from either side, no provider
        name, no model name, no key and no tenant identifier. The prompt is named by its
        registry identity, which is a version string this repository publishes, and the rest is
        counts. The request id is not passed: `RequestIdFilter` attaches it already.

        This is the same discipline `DemandAccuracyService._observe` applies, and for the same
        reason: an operational log that accumulates user content is a data-retention liability
        that nobody decided to take on.
        """
        logger.info(
            "llm %s (prompt=%s@%s) after %s attempt(s)",
            outcome,
            request.prompt_id,
            request.prompt_version,
            attempts,
            extra={
                "outcome": outcome,
                "prompt_id": request.prompt_id,
                "prompt_version": request.prompt_version,
                "attempts": attempts,
                "structured": request.response_schema is not None,
            },
        )


__all__ = ["JITTER_SECONDS", "MAX_ATTEMPTS", "GuardedChatModel"]
