"""Declare, withdraw and list the spans for which a hotel's demand is observed.

    docker compose run --rm api python -m app.jobs.demand_observation declare \\
        --hotel <hotel public id> --from 2026-01-01 --to 2026-09-30
    docker compose run --rm api python -m app.jobs.demand_observation list --hotel <id>
    docker compose run --rm api python -m app.jobs.demand_observation withdraw \\
        --hotel <id> --from 2026-01-01 --to 2026-09-30

The operator entry point for :class:`app.services.demand_observation.DemandObservationService`.

**Why it exists.** Nothing in the booking tables can show that a date with no occupied nights was
observed rather than unrecorded. So the model's features, the dataset it is built from and the
intelligence forecasts read a date as a zero only inside a span declared here, and as unknown
everywhere else. A hotel with no declared span has no observed date: the demand model answers
``422 INSUFFICIENT_HISTORY`` and the forecasts report too little history, until it is declared.

**Declare only what is true.** A span says the hotel's complete booking record for every one of
its dates is in this database -- typically from the day the platform became the hotel's booking
system (or the first day an import made complete), through yesterday. It must end before the
hotel's today. Spans are closed: to keep observation current, declare the next span as days
pass -- daily from cron or a platform scheduler, for example ``--from`` and ``--to`` both
yesterday. A declaration that is not renewed fails safe: later days read as unknown, never as
zero. A gap in the record (an outage, a partial import) is left undeclared.

**What it prints.** The outcome and the spans, and on a refusal the reason; on any other failure
only the exception's type -- never a connection string. It writes no audit event, for the reason
the service's docstring gives.

Exit status: 0 when the command did what was asked (an identical span already declared counts),
1 when it was refused or failed (nothing is written), 2 for a usage error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import uuid

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.session import create_db_engine, create_session_factory
from app.repositories.demand_observation import DemandObservationRepository
from app.repositories.hotel import HotelRepository
from app.services.demand_observation import Clock, DemandObservationService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.demand_observation",
        description="Declare, withdraw and list the spans for which a hotel's demand is observed.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, purpose in (
        ("declare", "declare one span, both dates inclusive"),
        ("withdraw", "withdraw the span with exactly these dates"),
    ):
        command = commands.add_parser(name, help=purpose)
        command.add_argument("--hotel", type=uuid.UUID, required=True, help="hotel public id")
        command.add_argument(
            "--from", dest="observed_from", type=dt.date.fromisoformat, required=True
        )
        command.add_argument("--to", dest="observed_to", type=dt.date.fromisoformat, required=True)
    listing = commands.add_parser("list", help="list a hotel's declared spans")
    listing.add_argument("--hotel", type=uuid.UUID, required=True, help="hotel public id")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    configure_logs: bool = False,
) -> int:
    """Run one command. ``settings`` and ``clock`` are injectable for tests."""
    args = _parser().parse_args(argv)

    try:
        settings = settings or get_settings()
        if configure_logs:
            configure_logging(settings)
        engine = create_db_engine(settings)
        try:
            session = create_session_factory(engine)()
            try:
                service = DemandObservationService(
                    session,
                    HotelRepository(session),
                    DemandObservationRepository(session),
                    clock=clock,
                )
                if args.command == "declare":
                    added = service.declare(args.hotel, args.observed_from, args.observed_to)
                    outcome = "declared" if added else "already declared; nothing changed"
                elif args.command == "withdraw":
                    service.withdraw(args.hotel, args.observed_from, args.observed_to)
                    outcome = "withdrawn"
                else:
                    outcome = "listed"
                spans = service.spans(args.hotel)
            finally:
                session.close()
        finally:
            engine.dispose()
    except AppError as refusal:
        # Written for an operator to read: what was refused and why, naming no table or value
        # beyond the dates the operator typed.
        print(f"demand observation: refused -- {refusal.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"demand observation FAILED ({type(exc).__name__}); nothing was written",
            file=sys.stderr,
        )
        return 1

    print(f"demand observation for hotel {args.hotel}: {outcome}; {len(spans)} span(s) declared")
    for span in spans:
        days = (span.observed_to - span.observed_from).days + 1
        print(f"  {span.observed_from.isoformat()} .. {span.observed_to.isoformat()} ({days} days)")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the tests
    raise SystemExit(main(configure_logs=True))
