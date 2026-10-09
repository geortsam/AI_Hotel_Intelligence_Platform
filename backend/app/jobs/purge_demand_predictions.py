"""Physically delete every expired stored demand prediction, at every hotel.

    docker compose run --rm api python -m app.jobs.purge_demand_predictions

The operator entry point for
:func:`app.services.demand_prediction_retention.purge_expired_predictions`. It uses the
application's settings -- including ``DEMAND_PREDICTION_RETENTION_DAYS`` (730 by default) -- and
its database configuration, runs the job, and prints one line of counts.

**Why it exists.** Every served forecast is stored (Stage 6.8), and nothing else deletes one: no
request purges this table. Without this command stored predictions accumulate indefinitely.

**It does not schedule itself.** Run it periodically -- daily is the recommended cadence -- from
cron, a systemd timer or the platform's scheduler. How often it runs bounds how long a prediction
can remain after its target date left the retention period.

**What it prints.** Counts and the retention period, and on failure only the exception's type:
never an identifier, a hotel, a date, a value or a connection string. It records no audit event
and loads no model; see the job function's docstring.

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
from app.services.demand_prediction_retention import purge_expired_predictions


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
        prog="python -m app.jobs.purge_demand_predictions",
        description="Physically delete every expired stored demand prediction, at every hotel.",
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
                result = purge_expired_predictions(session, settings)
            finally:
                session.close()
        finally:
            engine.dispose()
    except Exception as exc:
        # An operator command reports every failure, and reports the type only: a driver
        # message can carry SQL, and a configuration error the URL.
        print(
            f"demand prediction purge FAILED ({type(exc).__name__}); nothing past the last "
            "completed batch was deleted",
            file=sys.stderr,
        )
        return 1

    print(
        f"demand prediction purge: {result.predictions_deleted} expired prediction(s) "
        f"deleted at {result.hotels} hotel(s) in {result.batches} batch(es); "
        f"retention {result.retention_days} day(s)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the tests
    raise SystemExit(main(configure_logs=True))
