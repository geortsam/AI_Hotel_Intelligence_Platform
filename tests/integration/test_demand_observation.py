"""Declared observation spans (migration 0016) against real PostgreSQL.

What only the database can prove: that two spans of one hotel cannot share a date (the GiST
exclusion constraint, closed at both ends), that a reversed span is refused by the CHECK, that
spans go with their hotel, and that the operator command declares, lists and withdraws through
the service with the exit statuses it documents -- without printing a connection string.

The semantic consequences (observed zero versus unknown) are pinned in the dataset, serving and
intelligence suites, and the pure rules in ``tests/backend/test_demand_observation.py``.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationError, constraint_name_of
from app.jobs.demand_observation import main
from app.models.hotel import Hotel
from app.repositories.demand_observation import DemandObservationRepository
from app.repositories.hotel import HotelRepository
from app.services.demand_observation import OVERLAP_CONSTRAINT, DemandObservationService
from tests.integration.conftest import TEST_DATABASE_URL, make_hotel, observe, requires_postgres

pytestmark = requires_postgres

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: Mid-afternoon in Athens, so "yesterday" is unambiguous: 2026-10-01.
NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)
JAN = (dt.date(2026, 1, 1), dt.date(2026, 1, 31))


def committed_hotel(session: Session, slug: str = "observed") -> Hotel:
    hotel = make_hotel(session, slug=slug)
    session.commit()
    return hotel


def service(session: Session) -> DemandObservationService:
    return DemandObservationService(
        session, HotelRepository(session), DemandObservationRepository(session), clock=lambda: NOW
    )


def spans(session: Session, hotel: Hotel) -> list[tuple[dt.date, dt.date]]:
    return DemandObservationRepository(session).periods(hotel.id)


# --- the constraints ----------------------------------------------------------------------------


def test_two_spans_of_one_hotel_cannot_share_a_date(session: Session) -> None:
    hotel = make_hotel(session)
    observe(session, hotel, *JAN)

    with pytest.raises(IntegrityError) as refused:
        observe(session, hotel, dt.date(2026, 1, 31), dt.date(2026, 2, 10))

    assert constraint_name_of(refused.value) == OVERLAP_CONSTRAINT


def test_a_span_inside_another_is_refused_too(session: Session) -> None:
    hotel = make_hotel(session)
    observe(session, hotel, *JAN)

    with pytest.raises(IntegrityError):
        observe(session, hotel, dt.date(2026, 1, 10), dt.date(2026, 1, 12))


def test_adjacent_spans_share_no_date_and_are_accepted(session: Session) -> None:
    """Closed at both ends: 31 January and 1 February are different dates."""
    hotel = make_hotel(session)
    observe(session, hotel, *JAN)
    observe(session, hotel, dt.date(2026, 2, 1), dt.date(2026, 2, 28))

    assert spans(session, hotel) == [JAN, (dt.date(2026, 2, 1), dt.date(2026, 2, 28))]


def test_two_hotels_may_declare_the_same_dates(session: Session) -> None:
    first, second = make_hotel(session, slug="obs-one"), make_hotel(session, slug="obs-two")
    observe(session, first, *JAN)
    observe(session, second, *JAN)

    assert spans(session, first) == spans(session, second) == [JAN]


def test_a_reversed_span_is_refused_by_the_database(session: Session) -> None:
    hotel = make_hotel(session)

    with pytest.raises(IntegrityError) as refused:
        observe(session, hotel, dt.date(2026, 1, 2), dt.date(2026, 1, 1))

    assert constraint_name_of(refused.value) == "ck_demand_observation_periods_ordered"


def test_a_single_day_span_is_accepted(session: Session) -> None:
    hotel = make_hotel(session)
    observe(session, hotel, dt.date(2026, 1, 1), dt.date(2026, 1, 1))

    assert spans(session, hotel) == [(dt.date(2026, 1, 1), dt.date(2026, 1, 1))]


def test_spans_go_with_their_hotel(session: Session) -> None:
    hotel = make_hotel(session)
    observe(session, hotel, *JAN)
    session.execute(sa.text("DELETE FROM hotels WHERE id = :id"), {"id": hotel.id})

    left = session.execute(sa.text("SELECT count(*) FROM demand_observation_periods")).scalar_one()
    assert left == 0


# --- the service, over the real constraint ------------------------------------------------------


def test_the_service_turns_an_overlap_into_a_conflict_and_keeps_the_first_span(
    session: Session,
) -> None:
    hotel = committed_hotel(session)
    observations = service(session)
    assert observations.declare(hotel.public_id, *JAN) is True

    with pytest.raises(ConflictError, match="shares a date"):
        observations.declare(hotel.public_id, dt.date(2026, 1, 15), dt.date(2026, 2, 15))

    assert spans(session, hotel) == [JAN]


def test_the_service_refuses_today_and_accepts_yesterday(session: Session) -> None:
    hotel = committed_hotel(session)
    observations = service(session)

    with pytest.raises(ValidationError):
        observations.declare(hotel.public_id, dt.date(2026, 9, 1), dt.date(2026, 10, 2))
    assert observations.declare(hotel.public_id, dt.date(2026, 9, 1), dt.date(2026, 10, 1))

    assert spans(session, hotel) == [(dt.date(2026, 9, 1), dt.date(2026, 10, 1))]


def test_withdrawing_makes_the_dates_unknown_again(session: Session) -> None:
    hotel = committed_hotel(session)
    observations = service(session)
    observations.declare(hotel.public_id, *JAN)

    observations.withdraw(hotel.public_id, *JAN)

    assert spans(session, hotel) == []
    with pytest.raises(NotFoundError):
        observations.withdraw(hotel.public_id, *JAN)


# --- the operator command -----------------------------------------------------------------------


def settings() -> Settings:
    return Settings(environment="test", database_url=TEST_DATABASE_URL)


def run(arguments: list[str]) -> int:
    return main(arguments, settings=settings(), clock=lambda: NOW)


def command(verb: str, hotel: Hotel, span: tuple[dt.date, dt.date] = JAN) -> list[str]:
    return [verb, "--hotel", str(hotel.public_id), "--from", str(span[0]), "--to", str(span[1])]


def test_the_command_declares_lists_and_withdraws(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    hotel = committed_hotel(session)

    assert run(command("declare", hotel)) == 0
    declared = capsys.readouterr()
    assert declared.err == ""
    assert f"demand observation for hotel {hotel.public_id}: declared; 1 span(s)" in declared.out
    assert "2026-01-01 .. 2026-01-31 (31 days)" in declared.out

    assert run(command("declare", hotel)) == 0
    assert "already declared; nothing changed; 1 span(s)" in capsys.readouterr().out

    assert run(["list", "--hotel", str(hotel.public_id)]) == 0
    assert "listed; 1 span(s)" in capsys.readouterr().out

    assert run(command("withdraw", hotel)) == 0
    assert "withdrawn; 0 span(s)" in capsys.readouterr().out
    assert spans(session, hotel) == []


def test_the_command_reports_a_refusal_and_exits_one(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    hotel = committed_hotel(session)
    assert run(command("declare", hotel)) == 0
    capsys.readouterr()

    overlapping = (dt.date(2026, 1, 20), dt.date(2026, 2, 5))
    assert run(command("declare", hotel, overlapping)) == 1
    refused = capsys.readouterr()
    assert refused.out == ""
    assert refused.err.startswith("demand observation: refused -- That period shares a date")

    assert run(command("declare", hotel, (dt.date(2026, 10, 2), dt.date(2026, 10, 2)))) == 1
    assert "today" in capsys.readouterr().err

    assert run(["list", "--hotel", str(uuid.uuid4())]) == 1
    assert "refused -- Hotel not found." in capsys.readouterr().err
    assert spans(session, hotel) == [JAN]


def test_a_usage_error_exits_two_before_any_connection(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["declare", "--hotel", "not-a-uuid", "--from", "2026-01-01", "--to", "2026-01-02"])

    assert stopped.value.code == 2
    assert capsys.readouterr().out == ""


def test_the_module_command_fails_without_leaking_the_connection(tmp_path: Path) -> None:
    unreachable = (
        "postgresql+psycopg://obs-user:PASSWORD-SENTINEL@127.0.0.1:1/nowhere?connect_timeout=3"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("POSTGRES_", "DATABASE_URL"))
    }
    environment["DATABASE_URL"] = unreachable
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "backend")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.jobs.demand_observation",
            "list",
            "--hotel",
            str(uuid.uuid4()),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "FAILED (OperationalError); nothing was written" in completed.stderr
    for secret in ("PASSWORD-SENTINEL", "obs-user", "nowhere", "Traceback"):
        assert secret not in completed.stderr


def test_the_command_writes_no_audit_event(session: Session) -> None:
    hotel = committed_hotel(session)
    assert run(command("declare", hotel)) == 0

    events = session.execute(sa.text("SELECT count(*) FROM audit_events")).scalar_one()
    assert events == 0
