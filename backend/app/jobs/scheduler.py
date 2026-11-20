"""Run the retention jobs on a schedule: the three purges and the audit archival (R4.2).

    python -m app.jobs.scheduler              # every 24 hours, until stopped
    python -m app.jobs.scheduler --once       # each job once, then exit (cron, a systemd timer)

Each job is the operator command of the same name, called in this process with these settings:

- ``app.jobs.archive_audit_events``      audit events past retention, copied and verified;
- ``app.jobs.purge_conversations``       expired copilot conversations, deleted;
- ``app.jobs.purge_llm_invocations``     expired copilot accounting records, deleted;
- ``app.jobs.purge_demand_predictions``  expired stored demand predictions, deleted.

**Opt-in.** Nothing starts it by default. ``docker-compose.scheduler.yml`` adds it to the stack
as a separate service (``docker compose -f docker-compose.yml -f docker-compose.scheduler.yml up``);
without that file the stack is exactly what it was, and the commands can still be run by hand or
from the host's own scheduler.

**A failed job does not stop the others**, and does not stop the schedule: it is reported, and
the next cycle tries again. Every job is idempotent and resumes where it stopped -- each batch is
its own transaction -- so a missed or failed cycle loses nothing but time. Stopping the process
(``docker stop`` sends SIGTERM) is safe at any point for the same reason.

**What it prints** is each job's own line of counts, and one summary line per cycle. Like the
jobs, it never prints an identifier, a row or a connection string.

Exit status for ``--once``: 0 when every job succeeded, 1 when any failed, 2 for a usage error.
Without ``--once`` it runs until stopped.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.jobs import (
    archive_audit_events,
    purge_conversations,
    purge_demand_predictions,
    purge_llm_invocations,
)

#: The recommended cadence of every job it runs (each command's docstring says daily).
DEFAULT_INTERVAL_HOURS = 24


@dataclass(frozen=True, slots=True)
class Job:
    """A scheduled command: its name in the output, and its ``main(argv, *, settings=...)``."""

    name: str
    run: Callable[..., int]


JOBS: tuple[Job, ...] = (
    Job("audit archival", archive_audit_events.main),
    Job("copilot conversation purge", purge_conversations.main),
    Job("llm invocation purge", purge_llm_invocations.main),
    Job("demand prediction purge", purge_demand_predictions.main),
)


def run_once(settings: Settings, jobs: Sequence[Job] = JOBS) -> int:
    """Run every job once, in order, each regardless of the others. 0 if all succeeded, else 1."""
    failed: list[str] = []
    for job in jobs:
        try:
            status = job.run([], settings=settings)
        except Exception as exc:
            # Each command already reports its own failures and returns 1; this is for one that
            # raised past its boundary. The type only, as everywhere in these commands.
            print(f"{job.name} FAILED ({type(exc).__name__})", file=sys.stderr)
            status = 1
        if status != 0:
            failed.append(job.name)
    if failed:
        print(
            f"scheduler: {len(jobs) - len(failed)} of {len(jobs)} job(s) succeeded; failed: "
            + ", ".join(failed)
        )
        return 1
    print(f"scheduler: {len(jobs)} of {len(jobs)} job(s) succeeded")
    return 0


def interval_seconds(value: str) -> int:
    """``--interval-hours`` as whole seconds; a finite number of hours of at least one second.

    Parsed as a decimal, so "0.5" is exactly half an hour and no binary rounding is involved.
    Zero, a negative number and anything under a second are all refused by the one floor.
    """
    try:
        hours = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("must be a number of hours") from None
    if not hours.is_finite():
        raise argparse.ArgumentTypeError("must be a finite number of hours")
    seconds = int(hours * 3600)
    if seconds < 1:
        raise argparse.ArgumentTypeError("must be a positive number of hours, a second or more")
    return seconds


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    configure_logs: bool = False,
    jobs: Sequence[Job] = JOBS,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
) -> int:
    """Parse the arguments, then run once or on the interval.

    ``settings``, ``jobs``, ``sleep`` and ``max_cycles`` are injectable for tests; the command
    passes none of them, so it reads the process's settings and runs until stopped.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.scheduler",
        description="Run the audit archival and the retention purges on a schedule.",
    )
    parser.add_argument("--once", action="store_true", help="run each job once, then exit")
    parser.add_argument(
        "--interval-hours",
        dest="interval",
        type=interval_seconds,
        default=DEFAULT_INTERVAL_HOURS * 3600,
        help=f"hours between the starts of two cycles (default {DEFAULT_INTERVAL_HOURS:g})",
    )
    args = parser.parse_args(argv)

    try:
        settings = settings or get_settings()
    except Exception as exc:
        # A configuration error's message can quote the value it rejected, a URL among them.
        print(f"scheduler FAILED ({type(exc).__name__}); no job was run", file=sys.stderr)
        return 1
    if configure_logs:
        configure_logging(settings)

    if args.once:
        return run_once(settings, jobs)

    interval: int = args.interval
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        started = time.monotonic()
        run_once(settings, jobs)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        # From the start of one cycle to the start of the next, so a slow cycle does not push
        # every later one back; a cycle longer than the interval starts the next at once.
        sleep(max(0, interval - (time.monotonic() - started)))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the tests
    raise SystemExit(main(configure_logs=True))
