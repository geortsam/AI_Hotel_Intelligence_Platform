"""Physically delete every expired copilot accounting record, at every hotel.

    docker compose run --rm api python -m app.jobs.purge_llm_invocations

The operator entry point for
:func:`app.services.llm_invocation_retention.purge_expired_invocations`. It uses the application's
settings -- including ``LLM_INVOCATION_RETENTION_DAYS`` (365 by default) -- and its database
configuration, runs the job, and prints one line of counts.

**Why it exists.** An ``llm_invocations`` record expires ``LLM_INVOCATION_RETENTION_DAYS`` after
it was written, and nothing else deletes it: no request purges this table. Without this command
expired records stay in the database indefinitely.

**It does not schedule itself.** Run it periodically -- daily is the recommended cadence -- from
cron, a systemd timer or the platform's scheduler. How often it runs bounds how long a record can
remain after its retention period ended.

**What it prints.** Counts and the retention period, and on failure only the exception's type:
never an identifier, a hotel, an actor or a connection string. It records no audit event, calls
no model and charges no copilot allowance; see the job function's docstring.

Exit status: 0 when the purge completed, 1 when it failed (nothing is left half-deleted: every
batch is its own transaction, and running the command again finishes the work), 2 for a usage
error.
"""

from __future__ import annotations

import argparse
import sys

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_db_engine, create_session_factory
from app.services.llm_invocation_retention import purge_expired_invocations


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    configure_logs: bool = False,
) -> int:
    """Run one global purge.

    ``settings`` is injectable for tests; the command reads the process's. Loading them sits
    inside the failure boundary too: a configuration error's traceback can quote the value it
    rejected, and that value may be a connection string.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.purge_llm_invocations",
        description="Physically delete every expired copilot accounting record, at every hotel.",
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
                result = purge_expired_invocations(session, settings)
            finally:
                session.close()
        finally:
            engine.dispose()
    except Exception as exc:
        # An operator command reports every failure, and reports the type only: a driver
        # message can carry SQL, and a configuration error the URL.
        print(
            f"llm invocation purge FAILED ({type(exc).__name__}); nothing past the last "
            "completed batch was deleted",
            file=sys.stderr,
        )
        return 1

    print(
        f"llm invocation purge: {result.invocations_deleted} expired invocation record(s) "
        f"deleted at {result.hotels} hotel(s) in {result.batches} batch(es); "
        f"retention {result.retention_days} day(s)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the tests
    raise SystemExit(main(configure_logs=True))
