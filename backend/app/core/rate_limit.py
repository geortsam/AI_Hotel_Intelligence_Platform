"""In-process request rate limiting.

Stage 4.5.3. Argon2id makes a password guess expensive for the *server* -- 64 MiB and a few
tens of milliseconds each -- which protects the password but turns unmetered login into a
memory-pressure lever. This bounds how often a single source may ask.

**Deliberately knows nothing.** No HTTP, no FastAPI, no database, no user, no hotel, no token.
It is handed an opaque key and a policy and answers yes or no; the caller decides what the key
means. That is what keeps it out of the layers below it -- see ``app.api.deps.rate_limited``
for the only place it meets a request.

**Fixed window, not sliding.** A fixed window admits at most ``2 x limit`` across a window
boundary -- ten attempts in the two seconds either side of a minute mark, at 5/minute. For
brute-force protection that is immaterial: the attacker's sustained rate is still the limit,
and the alternative costs a timestamp list per key for a bound nobody is relying on.

**In-process, and therefore per-worker.** Each application process keeps its own counters, so
N workers admit up to N times the configured rate in aggregate. That is a real limitation and
is documented rather than papered over; a shared store (Redis or similar) is what fixes it,
and this stage deliberately does not introduce one. The protection is still meaningful:
it turns unbounded guessing into a rate proportional to the number of workers, not to the
attacker's bandwidth.

``time.monotonic`` is the clock, not wall time: a limiter that could be reset by an NTP step or
a daylight-saving jump would be a limiter an attacker could wait out.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

#: Stop the bucket table growing without bound when a source rotates addresses. Reached only
#: under deliberate abuse; at that point every expired entry is dropped, which is cheap and
#: leaves live counters intact. Nothing here is security-critical -- the cap protects memory,
#: not correctness.
MAX_TRACKED_KEYS = 10_000


@dataclass(frozen=True, slots=True)
class RateLimit:
    """How many requests a key may make, and over what period."""

    limit: int
    window_seconds: int


@dataclass(frozen=True, slots=True)
class Verdict:
    """The limiter's answer, and how long to wait if it said no."""

    allowed: bool
    #: Whole seconds until the current window ends. Zero when allowed. Always at least 1 when
    #: refused, because ``Retry-After: 0`` invites an immediate retry that would also fail.
    retry_after: int


class FixedWindowRateLimiter:
    """Counts requests per key within a fixed window.

    Thread-safe: FastAPI runs synchronous endpoints in a worker threadpool, so two requests
    genuinely can read-modify-write the same bucket at once. The lock is held for a dictionary
    lookup and an integer increment.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        #: Injectable so tests can advance time deterministically instead of sleeping through
        #: a real minute. Nothing else in the application passes it.
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, policy: RateLimit) -> Verdict:
        """Record one request against *key* and say whether it is allowed.

        The refused request is counted too. That is intentional: a client that keeps hammering
        does not extend its own window -- the window still ends where it began -- but neither
        does it get a free retry for each rejection.
        """
        now = self._clock()
        with self._lock:
            if len(self._buckets) >= self._max_keys:
                self._prune(now, policy.window_seconds)

            started, count = self._buckets.get(key, (now, 0))
            if now - started >= policy.window_seconds:
                started, count = now, 0
            count += 1
            self._buckets[key] = (started, count)

        if count <= policy.limit:
            return Verdict(allowed=True, retry_after=0)

        remaining = started + policy.window_seconds - now
        return Verdict(allowed=False, retry_after=max(1, math.ceil(remaining)))

    def reset(self) -> None:
        """Forget every counter. For tests; nothing in the application calls it."""
        with self._lock:
            self._buckets.clear()

    def _prune(self, now: float, window_seconds: float) -> None:
        """Drop windows that have already expired. Caller holds the lock."""
        expired = [
            key for key, (started, _) in self._buckets.items() if now - started >= window_seconds
        ]
        for key in expired:
            del self._buckets[key]


__all__ = ["MAX_TRACKED_KEYS", "FixedWindowRateLimiter", "RateLimit", "Verdict"]
