"""Copy every audit event past retention into the archive, verified, at every hotel.

    docker compose run --rm api python -m app.jobs.archive_audit_events

The operator entry point for :func:`app.services.retention.archive_audit_events` (R4.2), which
until now had no command: it was "a function, not an endpoint", called from a Python shell. It
uses the application's settings -- ``AUDIT_RETENTION_DAYS`` (730 by default),
``AUDIT_ARCHIVE_BATCH_SIZE`` and ``AUDIT_ARCHIVE_MAX_BATCHES`` -- and its database configuration,
runs the job once, and prints one line of counts.

**It copies; it never deletes.** ``audit_events`` is append-only (migration 0007), and archival
copies an eligible event into ``audit_events_archive``, verifies the copy field by field and only
then commits. See :mod:`app.services.retention` for why nothing is removed.

**Its own schedule.** ``python -m app.jobs.scheduler`` runs it daily with the purges; or run it
from cron or a systemd timer. Daily is the recommended cadence.

**What it prints.** Counts, the retention period and whether the batch ceiling stopped the run
before the backlog was cleared (running it again continues); on failure only the exception's
type: never an event, an identifier, an actor or a connection string. Like the job, it records
no audit event of its own.

Exit status: 0 when the run completed, including a run the batch ceiling stopped (it says so);
1 when it failed (every batch is its own transaction, verified before it commits, so a failure
leaves no unverified copy, and running again resumes); 2 for a usage error.
"""

from __future__ import annotations

import argparse
import sys

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_db_engine, create_session_factory
from app.services.retention import RetentionPolicy, archive_audit_events


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    configure_logs: bool = False,
) -> int:
    """Run one archival pass.

    ``settings`` is injectable for tests; the command reads the process's. Loading them sits
    inside the failure boundary too: a configuration error's traceback can quote the value it
    rejected, and that value may be a connection string.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.archive_audit_events",
        description="Copy every audit event past retention into the archive, verified.",
    )
    parser.parse_args(argv)

    try:
        settings = settings or get_settings()
        if configure_logs:
            configure_logging(settings)
        engine = create_db_engine(settings)
        try:
            session = create_session_factory(engine)()
            try:
                result = archive_audit_events(session, settings)
            finally:
                session.close()
        finally:
            engine.dispose()
    except Exception as exc:
        # An operator command reports every failure, and reports the type only: a driver
        # message can carry SQL -- here, the audit rows being copied -- and a configuration
        # error the URL.
        print(
            f"audit archival FAILED ({type(exc).__name__}); every committed batch was verified, "
            "and nothing unverified was kept",
            file=sys.stderr,
        )
        return 1

    state = "complete" if result.complete else "stopped at the batch ceiling; run it again"
    print(
        f"audit archival: {result.archived} event(s) archived in {result.batches_run} "
        f"batch(es), {result.already_archived} already archived, {result.remaining} remaining "
        f"({state}); retention {RetentionPolicy.from_settings(settings).retention_days} day(s)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the tests
    raise SystemExit(main(configure_logs=True))
