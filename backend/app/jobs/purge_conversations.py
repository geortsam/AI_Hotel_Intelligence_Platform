"""Physically delete every expired copilot conversation, at every hotel.

    docker compose run --rm api python -m app.jobs.purge_conversations

The operator entry point for :func:`app.services.copilot_conversation.purge_expired_conversations`.
It uses the application's settings -- including ``COPILOT_CONVERSATION_RETENTION_DAYS`` -- and its
database configuration, runs the job, and prints one line of counts.

**Why it exists.** An expired conversation is unreachable the moment it expires: every query
requires ``last_activity_at > now() - retention``. Physical deletion is separate. A conversation
start or continuation purges its own hotel's expired conversations, so at a hotel nobody uses
nothing would ever remove them. This command removes them everywhere.

**It does not schedule itself.** Run it periodically -- daily is the recommended cadence -- from
cron, a systemd timer or the platform's scheduler. How often it runs bounds how long an expired
conversation's text can remain in the database after it became unreachable.

**What it prints.** Counts and the retention period, and on failure only the exception's type:
never an identifier, a question, an answer or a connection string. It records no audit event,
calls no model and charges no copilot allowance; see the job function's docstring.

Exit status: 0 when the purge completed, 1 when it failed (nothing is left half-deleted: every
batch is its own transaction), 2 for a usage error.
"""

from __future__ import annotations

import argparse
import sys

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_db_engine, create_session_factory
from app.services.copilot_conversation import purge_expired_conversations


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
        prog="python -m app.jobs.purge_conversations",
        description="Physically delete every expired copilot conversation, at every hotel.",
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
                result = purge_expired_conversations(session, settings)
            finally:
                session.close()
        finally:
            engine.dispose()
    except Exception as exc:
        # An operator command reports every failure, and reports the type only: a driver
        # message can carry SQL, and a configuration error the URL.
        print(
            f"copilot conversation purge FAILED ({type(exc).__name__}); nothing past the last "
            "completed batch was deleted",
            file=sys.stderr,
        )
        return 1

    print(
        f"copilot conversation purge: {result.conversations_deleted} expired conversation(s) "
        f"deleted in {result.batches} batch(es); retention {result.retention_days} day(s)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the tests
    raise SystemExit(main(configure_logs=True))
