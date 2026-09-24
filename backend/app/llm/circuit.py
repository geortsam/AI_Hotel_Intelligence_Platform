"""The circuit breaker §2 asks for, with the semantics §5.7 did not give. Stage 7.6.

§2: the LLM call "is slow, external, paid for, and can hang. That is an argument for a timeout, a
budget and a circuit-breaker **inside** the process". Stage 7.5 built the first two and left this
one out, because §5.7 specified no threshold, window, open duration, probe rule or error code and
inventing them would have put undocumented numbers in the runtime path. Stage 7.6 was asked to
decide them. The decision is recorded in `docs/v2-architecture.md` §5.8 (Amendment A1) and
implemented here, and the two are asserted against each other by a test.

## The semantics, in full

    CLOSED ──5 availability failures within 60 s──► OPEN
      ▲                                              │
      │                                         30 s elapse
      │                                              ▼
      └──────── the one probe succeeds ─────── HALF_OPEN ──probe fails──► OPEN (30 s again)
                                                     │
                            probe ends any other way: probe released, stays HALF_OPEN

- **Threshold** — 5 failures.
- **Window** — a rolling 60 seconds, measured on a monotonic clock.
- **OPEN duration** — 30 seconds, then HALF_OPEN.
- **HALF_OPEN** — exactly one call is let through as a probe; every other call is refused while
  it runs.
- **Failure** — a call that ends in `LlmUnavailableError`: the provider could not be reached or
  did not answer within the deadline, **after** the boundary's own retry. One call is one
  failure, not two.
- **Not a failure** — rate limit, budget, disabled, invalid response, and any success. None of
  those says the provider is down: a rate limit is the provider answering, and an invalid
  response is a provider that is up and wrong.
- **Success** — a call that returned an answer. It closes the breaker only when it was the probe;
  a success in CLOSED does not erase earlier failures in the window.
- **Owner** — `GuardedChatModel`, checked after the disabled flag and the budget, before a thread
  is spent.
- **While OPEN** — the call is refused immediately and the provider is never reached.
- **Error while OPEN** — `LlmCircuitOpenError`, a subclass of `LlmUnavailableError`, so **the
  public code is `LLM_UNAVAILABLE` and the status 503**: a caller sees the same declared failure
  a timeout produces, which is the truth — the provider is unavailable.
- **Test control** — the clock is injected (`monotonic`), and every transition is a pure
  function of recorded outcomes and that clock. No test sleeps.

## Why these numbers, and why conservative

Five in a minute is deliberately slow to trip. A breaker that opens on two timeouts turns a
provider's brief wobble into thirty seconds of refusals for everyone; one that needs five
in a minute opens only when the provider is plainly down, which is when refusing fast is kinder
than making every caller wait out two deadlines. Thirty seconds is short enough that recovery is
noticed promptly and long enough that the probe is not itself a stampede.

## What the breaker knows, and what it does not

Counts and timestamps. It holds no request, no prompt, no answer, no caller and no property. It is
process-wide per provider and model — one breaker for everyone using the same upstream, because
the thing it measures is the upstream's health, which is the same for every caller. That is also
why it cannot weaken isolation between properties: it carries nothing about any of them, and its
only effect is to refuse a call earlier than the provider would have failed it.

It is in-process and not shared between workers. Each process learns independently that the
provider is down, which costs at most five failed calls per process — and needs no Redis, no
second service and no shared store, which §2 decided against.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.llm.errors import LlmCircuitOpenError

logger = logging.getLogger(__name__)

CircuitState = Literal["closed", "open", "half_open"]

#: Availability failures inside the window that open the breaker. See the module docstring.
FAILURE_THRESHOLD = 5
#: The rolling window those failures are counted over, in seconds.
WINDOW_SECONDS = 60.0
#: How long the breaker stays OPEN before letting a single probe through, in seconds.
OPEN_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class CircuitPolicy:
    """The three numbers. Frozen, so a breaker's rules cannot change while it is running."""

    failure_threshold: int = FAILURE_THRESHOLD
    window_seconds: float = WINDOW_SECONDS
    open_seconds: float = OPEN_SECONDS

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1.")
        if self.window_seconds <= 0 or self.open_seconds <= 0:
            raise ValueError("window_seconds and open_seconds must be greater than zero.")


class CircuitBreaker:
    """A three-state breaker over an injected monotonic clock. Thread-safe.

    The protocol a caller follows is fixed, and `GuardedChatModel` is the only caller:

        probe = breaker.acquire()          # raises LlmCircuitOpenError when refusing
        ... make the call ...
        breaker.record_success(probe)      # or record_failure(probe), or release(probe)

    `acquire` returns whether this call is the HALF_OPEN probe, so that only the probe's outcome
    can close or re-open the breaker. A call that started while CLOSED and finished after the
    breaker opened says nothing about whether the provider has recovered since.
    """

    def __init__(
        self,
        policy: CircuitPolicy | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy or CircuitPolicy()
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._state: CircuitState = "closed"
        self._failures: deque[float] = deque()
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def policy(self) -> CircuitPolicy:
        return self._policy

    @property
    def state(self) -> CircuitState:
        """The current state, with any time-driven OPEN -> HALF_OPEN transition applied."""
        with self._lock:
            self._advance(self._monotonic())
            return self._state

    def acquire(self) -> bool:
        """Permit one call, or refuse it. Returns True when the permitted call is the probe."""
        with self._lock:
            self._advance(self._monotonic())
            if self._state == "closed":
                return False
            if self._state == "half_open" and not self._probe_in_flight:
                self._probe_in_flight = True
                return True
        # OPEN, or HALF_OPEN with the probe already out. Raised outside the lock.
        raise LlmCircuitOpenError()

    def record_success(self, probe: bool) -> None:
        with self._lock:
            if probe:
                self._probe_in_flight = False
                self._failures.clear()
                self._transition("closed")

    def record_failure(self, probe: bool) -> None:
        """One availability failure. Opens the breaker at the threshold, or after a failed probe."""
        with self._lock:
            now = self._monotonic()
            if probe:
                self._probe_in_flight = False
                self._open(now)
                return
            if self._state != "closed":
                return
            self._failures.append(now)
            self._forget_before(now - self._policy.window_seconds)
            if len(self._failures) >= self._policy.failure_threshold:
                self._open(now)

    def release(self, probe: bool) -> None:
        """A call ended in a way that says nothing about availability. Frees the probe slot."""
        with self._lock:
            if probe:
                self._probe_in_flight = False

    # --- internals, always called under the lock ----------------------------------------------

    def _advance(self, now: float) -> None:
        if self._state == "open" and now - self._opened_at >= self._policy.open_seconds:
            self._transition("half_open")
        if self._state == "closed":
            self._forget_before(now - self._policy.window_seconds)

    def _forget_before(self, horizon: float) -> None:
        while self._failures and self._failures[0] <= horizon:
            self._failures.popleft()

    def _open(self, now: float) -> None:
        self._opened_at = now
        self._failures.clear()
        self._transition("open")

    def _transition(self, target: CircuitState) -> None:
        if target == self._state:
            return
        previous, self._state = self._state, target
        # Counts and state names only: the breaker holds nothing else to log.
        logger.warning(
            "llm circuit %s -> %s",
            previous,
            target,
            extra={"circuit_from": previous, "circuit_to": target},
        )


__all__ = [
    "FAILURE_THRESHOLD",
    "OPEN_SECONDS",
    "WINDOW_SECONDS",
    "CircuitBreaker",
    "CircuitPolicy",
    "CircuitState",
]
