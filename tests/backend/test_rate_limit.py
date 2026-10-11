"""`FixedWindowRateLimiter`, on its own: no HTTP, no database, an injected clock.

The integration suites test each route's limit end to end. These test the limiter's arithmetic,
and above all the prune: one limiter serves policies with different windows (login's minute,
the copilot's and knowledge writes' hour), and pruning must judge each bucket by ITS OWN window.
Judged by the window of whichever call triggered the prune, a flood against the login route
would reset live hour-long counters -- precisely when the table is full because someone is
abusing it.
"""

from __future__ import annotations

from app.core.rate_limit import FixedWindowRateLimiter, RateLimit

MINUTE = RateLimit(limit=5, window_seconds=60)
HOUR = RateLimit(limit=2, window_seconds=3600)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_requests_up_to_the_limit_are_allowed_and_the_next_is_refused() -> None:
    clock = Clock()
    limiter = FixedWindowRateLimiter(clock=clock)

    verdicts = [limiter.check("k", MINUTE) for _ in range(6)]

    assert [v.allowed for v in verdicts] == [True] * 5 + [False]
    assert verdicts[-1].retry_after == 60


def test_the_window_ends_where_it_began_and_then_resets() -> None:
    clock = Clock()
    limiter = FixedWindowRateLimiter(clock=clock)
    for _ in range(5):
        limiter.check("k", MINUTE)

    clock.now += 45
    refused = limiter.check("k", MINUTE)
    assert (refused.allowed, refused.retry_after) == (False, 15)

    clock.now += 15
    assert limiter.check("k", MINUTE).allowed


def test_retry_after_is_never_zero() -> None:
    clock = Clock()
    limiter = FixedWindowRateLimiter(clock=clock)
    for _ in range(5):
        limiter.check("k", MINUTE)

    clock.now += 59.9
    assert limiter.check("k", MINUTE).retry_after == 1


def test_keys_are_counted_separately() -> None:
    limiter = FixedWindowRateLimiter(clock=Clock())
    for _ in range(2):
        limiter.check("a", HOUR)

    assert not limiter.check("a", HOUR).allowed
    assert limiter.check("b", HOUR).allowed


def test_a_short_window_prune_does_not_reset_a_live_long_window_counter() -> None:
    """The regression. An hour-long bucket, exhausted; the table filled with minute-long buckets;
    two minutes later a minute-long call hits the cap and prunes. The hour bucket is still live,
    so it must survive -- and the caller must still be refused."""
    clock = Clock()
    limiter = FixedWindowRateLimiter(clock=clock, max_keys=4)
    for _ in range(3):
        limiter.check("copilot", HOUR)
    assert not limiter.check("copilot", HOUR).allowed
    for index in range(3):
        limiter.check(f"login:{index}", MINUTE)

    clock.now += 120
    limiter.check("login:new", MINUTE)  # the table is at its cap: this call prunes

    assert set(limiter._buckets) == {"copilot", "login:new"}
    refused = limiter.check("copilot", HOUR)
    assert not refused.allowed
    assert refused.retry_after == 3600 - 120


def test_a_long_window_prune_does_not_keep_expired_short_buckets() -> None:
    """The other direction: an hour-long call prunes minute-long buckets that have expired."""
    clock = Clock()
    limiter = FixedWindowRateLimiter(clock=clock, max_keys=3)
    limiter.check("login:a", MINUTE)
    limiter.check("login:b", MINUTE)
    limiter.check("copilot", HOUR)

    clock.now += 61
    limiter.check("copilot:other", HOUR)

    assert set(limiter._buckets) == {"copilot", "copilot:other"}


def test_a_prune_keeps_every_live_bucket_whatever_its_window() -> None:
    clock = Clock()
    limiter = FixedWindowRateLimiter(clock=clock, max_keys=3)
    limiter.check("login:a", MINUTE)
    limiter.check("copilot", HOUR)
    limiter.check("login:b", MINUTE)

    clock.now += 30  # nothing has expired
    limiter.check("login:c", MINUTE)

    assert set(limiter._buckets) == {"login:a", "copilot", "login:b", "login:c"}


def test_an_expired_long_window_bucket_is_pruned() -> None:
    clock = Clock()
    limiter = FixedWindowRateLimiter(clock=clock, max_keys=2)
    limiter.check("copilot", HOUR)
    limiter.check("login:a", MINUTE)

    clock.now += 3600
    limiter.check("login:b", MINUTE)

    assert set(limiter._buckets) == {"login:b"}


def test_reset_forgets_every_counter() -> None:
    limiter = FixedWindowRateLimiter(clock=Clock())
    for _ in range(3):
        limiter.check("k", HOUR)

    limiter.reset()

    assert limiter.check("k", HOUR).allowed
