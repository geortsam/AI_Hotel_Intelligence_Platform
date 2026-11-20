"""R4.2: `python -m app.jobs.archive_audit_events`, the archival operator command, on PostgreSQL.

The job it runs, :func:`app.services.retention.archive_audit_events`, is pinned in depth by
tests/integration/test_audit_retention.py. These tests pin the command: it archives what is past
retention and only that, reads its settings from the environment, says when the batch ceiling
stopped it, repeats harmlessly, fails without leaking, and prints counts only.

Events are written directly with back-dated ``occurred_at`` relative to the database's clock (the
application never back-dates one).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from tests.integration.conftest import TEST_DATABASE_URL, requires_postgres

pytestmark = requires_postgres

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OUTPUT = re.compile(
    r"^audit archival: (\d+) event\(s\) archived in (\d+) batch\(es\), (\d+) already archived, "
    r"(\d+) remaining \((complete|stopped at the batch ceiling; run it again)\); "
    r"retention (\d+) day\(s\)$"
)
SENTINEL = "AUDIT-DETAIL-SENTINEL"


@pytest.fixture(autouse=True)
def _clean(engine: Engine) -> Iterator[None]:
    """Both audit tables emptied around every test; TRUNCATE fires neither append-only trigger."""

    def wipe() -> None:
        with sessionmaker(bind=engine, future=True)() as s:
            s.execute(sa.text("TRUNCATE audit_events_archive"))
            s.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
            s.commit()

    wipe()
    yield
    wipe()


def seed(engine: Engine, *, count: int, age_days: int, prefix: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO audit_events (action, resource_type, resource_reference, details, "
                "occurred_at) SELECT 'amenity.created', 'amenity', :prefix || g, "
                "CAST(:details AS jsonb), now() - make_interval(days => :age) "
                "FROM generate_series(1, :count) g"
            ),
            {
                "prefix": prefix,
                "details": f'{{"note": "{SENTINEL}"}}',
                "age": age_days,
                "count": count,
            },
        )


def counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        active = connection.scalar(sa.text("SELECT count(*) FROM audit_events"))
        archived = connection.scalar(sa.text("SELECT count(*) FROM audit_events_archive"))
    return int(active or 0), int(archived or 0)


def run_command(
    tmp_path: Path, database_url: str, *arguments: str, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    """The command as an operator runs it, in a fresh process with only this database's URL.

    From an empty directory so no ``.env`` is read, with every ``POSTGRES_*`` and ``AUDIT_*``
    variable removed and ``DATABASE_URL`` set explicitly.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("POSTGRES_", "DATABASE_URL", "AUDIT_"))
    }
    environment["DATABASE_URL"] = database_url
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "backend")
    environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "app.jobs.archive_audit_events", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def parsed(completed: subprocess.CompletedProcess[str]) -> tuple[str, ...]:
    assert completed.returncode == 0, completed.stderr
    match = OUTPUT.match(completed.stdout.strip())
    assert match is not None, completed.stdout
    return match.groups()


def test_it_archives_what_is_past_retention_and_only_that(engine: Engine, tmp_path: Path) -> None:
    seed(engine, count=3, age_days=731, prefix="OLD-")
    seed(engine, count=2, age_days=10, prefix="NEW-")
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL)

    assert parsed(completed) == ("3", "2", "0", "0", "complete", "730")
    assert counts(engine) == (5, 3)  # copied, never deleted


def test_a_second_run_archives_nothing_new(engine: Engine, tmp_path: Path) -> None:
    seed(engine, count=2, age_days=800, prefix="OLD-")
    assert TEST_DATABASE_URL is not None

    first = run_command(tmp_path, TEST_DATABASE_URL)
    second = run_command(tmp_path, TEST_DATABASE_URL)

    assert parsed(first)[:4] == ("2", "2", "0", "0")
    assert parsed(second)[:4] == ("0", "1", "2", "0")
    assert counts(engine) == (2, 2)


def test_it_reads_the_retention_from_the_environment(engine: Engine, tmp_path: Path) -> None:
    seed(engine, count=1, age_days=40, prefix="FORTY-")
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL, AUDIT_RETENTION_DAYS="35")

    assert parsed(completed) == ("1", "2", "0", "0", "complete", "35")


def test_it_says_when_the_batch_ceiling_stopped_it(engine: Engine, tmp_path: Path) -> None:
    seed(engine, count=5, age_days=800, prefix="OLD-")
    assert TEST_DATABASE_URL is not None

    completed = run_command(
        tmp_path,
        TEST_DATABASE_URL,
        AUDIT_ARCHIVE_BATCH_SIZE="2",
        AUDIT_ARCHIVE_MAX_BATCHES="1",
    )

    assert parsed(completed) == (
        "2",
        "1",
        "0",
        "3",
        "stopped at the batch ceiling; run it again",
        "730",
    )
    assert counts(engine) == (5, 2)


def test_the_output_is_counts_only(engine: Engine, tmp_path: Path) -> None:
    seed(engine, count=1, age_days=800, prefix="REFERENCE-SENTINEL-")
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL)

    parsed(completed)
    for secret in (SENTINEL, "REFERENCE-SENTINEL", TEST_DATABASE_URL):
        assert secret not in completed.stdout
        assert secret not in completed.stderr


def test_it_exits_non_zero_without_leaking_the_connection(tmp_path: Path) -> None:
    unreachable = (
        "postgresql+psycopg://archive-user:PASSWORD-SENTINEL@127.0.0.1:1/nowhere?connect_timeout=3"
    )

    completed = run_command(tmp_path, unreachable)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "audit archival FAILED (OperationalError)" in completed.stderr
    for secret in ("PASSWORD-SENTINEL", "archive-user", "nowhere", "Traceback"):
        assert secret not in completed.stderr


def test_an_invalid_setting_is_refused_without_archiving(engine: Engine, tmp_path: Path) -> None:
    seed(engine, count=1, age_days=800, prefix="OLD-")
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL, AUDIT_RETENTION_DAYS="0")

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "FAILED (ValidationError)" in completed.stderr
    assert counts(engine) == (1, 0)


def test_it_refuses_an_unknown_argument(tmp_path: Path) -> None:
    """A usage error is refused before any connection: the URL is never used, so none is real."""
    unused = "postgresql+psycopg://nobody:unused@127.0.0.1:1/unused?connect_timeout=1"
    completed = run_command(tmp_path, unused, "--hotel", "anything")
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "unrecognized arguments" in completed.stderr


def test_the_scheduler_runs_it_once_with_the_purges(engine: Engine, tmp_path: Path) -> None:
    """``python -m app.jobs.scheduler --once``: every job, this one among them, then exit 0."""
    seed(engine, count=2, age_days=800, prefix="OLD-")
    assert TEST_DATABASE_URL is not None
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("POSTGRES_", "DATABASE_URL", "AUDIT_", "LLM_", "COPILOT_"))
    }
    environment.update(DATABASE_URL=TEST_DATABASE_URL, PYTHONPATH=str(REPOSITORY_ROOT / "backend"))

    completed = subprocess.run(
        [sys.executable, "-m", "app.jobs.scheduler", "--once"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert OUTPUT.match(lines[0]) is not None, completed.stdout
    assert lines[1].startswith("copilot conversation purge: ")
    assert lines[2].startswith("llm invocation purge: ")
    assert lines[3].startswith("demand prediction purge")
    assert lines[-1] == "scheduler: 4 of 4 job(s) succeeded"
    assert counts(engine) == (2, 2)
    assert TEST_DATABASE_URL not in completed.stdout + completed.stderr
