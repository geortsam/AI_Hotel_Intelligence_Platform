"""The global conversation purge: the job function and its operator command, over real PostgreSQL.

`purge_expired_conversations(session, settings)` physically deletes every expired copilot
conversation, at every hotel, in bounded batches; `python -m app.jobs.purge_conversations` is how
an operator (or a scheduler) runs it. These tests prove the deletion is physical and complete,
that nothing inside the retention window is touched, that the retention setting is the cutoff,
that the job is repeatable and bounded, that it reads and writes no conversation text anywhere
else, calls no model and charges no allowance -- and that the per-hotel purge on start and
continue is exactly what it was.

Expired conversations are written directly with back-dated timestamps (the application never
sets them), using the Stage 7.11 fixtures.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.jobs.purge_conversations import main
from app.llm.testing import ScriptedModel
from app.repositories.copilot_conversation import CopilotConversationRepository
from app.services.copilot_conversation import (
    PURGE_BATCH,
    ConversationPurgeResult,
    CopilotConversationRetention,
    purge_expired_conversations,
)
from tests.integration.conftest import TEST_DATABASE_URL
from tests.integration.test_copilot_conversations_api import (
    World,
    build,
    insert_conversation,
    rows,
    scalar,
    started,
    turns_of,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OUTPUT = re.compile(
    r"^copilot conversation purge: (\d+) expired conversation\(s\) deleted in (\d+) batch\(es\); "
    r"retention (\d+) day\(s\)$"
)


@pytest.fixture
def world(engine: Engine, session: Session) -> World:
    return build(engine, session)


def settings(retention_days: int = 30) -> Settings:
    return Settings(
        environment="test",
        database_url=TEST_DATABASE_URL,
        copilot_conversation_retention_days=retention_days,
    )


def purge(world: World, retention_days: int = 30, **kwargs: Any) -> ConversationPurgeResult:
    return purge_expired_conversations(world.session, settings(retention_days), **kwargs)


def exists(world: World, public_id: str) -> bool:
    return bool(
        rows(world.session, "SELECT 1 FROM copilot_conversations WHERE public_id = :p", p=public_id)
    )


def message_count(world: World) -> int:
    return int(scalar(world.session, "SELECT count(*) FROM copilot_messages"))


# --- A. every hotel ------------------------------------------------------------------------


def test_expired_conversations_at_every_hotel_are_deleted(world: World) -> None:
    at_a = insert_conversation(world, "owner", days_ago=40)
    at_b = insert_conversation(world, "owner_b", days_ago=45, hotel=world.b)
    dual_b = insert_conversation(world, "dual", days_ago=60, hotel=world.b)

    result = purge(world)

    assert result.conversations_deleted == 3
    for public_id in (at_a, at_b, dual_b):
        assert not exists(world, public_id)


# --- B. turns go with their conversation ---------------------------------------------------------


def test_every_turn_of_an_expired_conversation_is_physically_deleted(world: World) -> None:
    expired = insert_conversation(world, "viewer2", days_ago=35, turns=4)
    elsewhere = insert_conversation(world, "owner_b", days_ago=35, turns=3, hotel=world.b)
    assert len(turns_of(world, expired)) == 4
    assert message_count(world) == 7

    purge(world)

    assert turns_of(world, expired) == []
    assert turns_of(world, elsewhere) == []
    assert message_count(world) == 0


# --- C. the retention boundary -----------------------------------------------------------------


def test_only_expired_conversations_are_deleted(world: World) -> None:
    # Started first: starting a conversation runs the per-hotel purge, which would otherwise
    # remove the expired one before the job under test ever saw it.
    live = started(world, "manager")
    expired = insert_conversation(world, "owner", days_ago=31, turns=2)
    just_inside = insert_conversation(world, "viewer2", days_ago=29, turns=2)
    recent = insert_conversation(world, "owner_b", days_ago=0, hotel=world.b)

    result = purge(world)

    assert result.conversations_deleted == 1
    assert not exists(world, expired)
    for survivor in (just_inside, recent, live):
        assert exists(world, survivor)
    assert len(turns_of(world, just_inside)) == 2
    # The survivors are still reachable, exactly as before the purge.
    assert world.read("viewer2", world.a, just_inside).status_code == 200


# --- D. the retention setting is the cutoff ----------------------------------------------------


def test_the_configured_retention_period_decides_what_is_deleted(world: World) -> None:
    fifteen = insert_conversation(world, "owner", days_ago=15)
    five = insert_conversation(world, "viewer2", days_ago=5)

    assert purge(world, retention_days=20).conversations_deleted == 0
    assert exists(world, fifteen) and exists(world, five)

    result = purge(world, retention_days=10)
    assert (result.conversations_deleted, result.retention_days) == (1, 10)
    assert not exists(world, fifteen)
    assert exists(world, five)


# --- E. repeatable -------------------------------------------------------------------------------


def test_running_the_purge_again_is_safe_and_finds_nothing(world: World) -> None:
    insert_conversation(world, "owner", days_ago=50, turns=2)
    live = insert_conversation(world, "viewer2", days_ago=1)

    first = purge(world)
    second = purge(world)
    third = purge(world)

    assert first.conversations_deleted == 1
    assert (second.conversations_deleted, second.batches) == (0, 1)
    assert third == second
    assert exists(world, live)


# --- F. bounded batches --------------------------------------------------------------------------


def test_more_than_one_batch_is_processed_to_completion(world: World) -> None:
    for days, hotel in [(40, world.a), (41, world.b), (42, world.a), (43, world.b), (44, world.a)]:
        insert_conversation(world, "dual", days_ago=days, turns=2, hotel=hotel)
    live = insert_conversation(world, "dual", days_ago=2)

    result = purge(world, batch_size=2)

    assert (result.conversations_deleted, result.batches) == (5, 3)  # 2 + 2 + 1
    remaining = rows(world.session, "SELECT public_id FROM copilot_conversations")
    assert [str(row["public_id"]) for row in remaining] == [live]
    assert message_count(world) == 1


def test_a_full_last_batch_is_followed_by_one_that_confirms_nothing_is_left(world: World) -> None:
    for days in (40, 41, 42, 43):
        insert_conversation(world, "owner", days_ago=days)

    result = purge(world, batch_size=2)

    assert (result.conversations_deleted, result.batches) == (4, 3)  # 2 + 2 + 0


def test_the_default_batch_is_the_per_hotel_bound(world: World) -> None:
    assert PURGE_BATCH == 100
    assert inspect.signature(purge_expired_conversations).parameters["batch_size"].default == 100
    with pytest.raises(ValueError):
        purge(world, batch_size=0)


# --- G. privacy ----------------------------------------------------------------------------------


def _counts(world: World) -> tuple[int, int]:
    return (
        int(scalar(world.session, "SELECT count(*) FROM audit_events")),
        int(scalar(world.session, "SELECT count(*) FROM llm_invocations")),
    )


def test_the_purge_writes_no_audit_event_and_no_invocation_record(world: World) -> None:
    # The fixture's turns carry "old question N" / "old answer": the text that must not travel.
    expired = insert_conversation(world, "owner", days_ago=40, turns=2)
    before = _counts(world)

    purge(world)

    assert _counts(world) == before
    assert not exists(world, expired)
    for table in ("audit_events", "llm_invocations"):
        dumped = json.dumps(rows(world.session, f"SELECT * FROM {table}"), default=str)
        assert "old question" not in dumped and "old answer" not in dumped, table


def test_the_command_output_is_counts_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    expired = insert_conversation(world, "owner", days_ago=40, turns=2)
    live = insert_conversation(world, "viewer2", days_ago=1)

    assert main([], settings=settings()) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    line = captured.out.strip()
    match = OUTPUT.match(line)
    assert match is not None, line
    assert match.groups() == ("1", "1", "30")
    for secret in (expired, live, "old question", "old answer", str(world.a.public_id)):
        assert secret not in captured.out
    assert TEST_DATABASE_URL is not None and TEST_DATABASE_URL not in captured.out


# --- H. no model, no allowance, no actor ------------------------------------------------------


def test_the_purge_calls_no_model_and_charges_no_allowance(
    engine: Engine, session: Session
) -> None:
    world = build(engine, session, copilot_actor_rate_limit=1, copilot_hotel_rate_limit=1)
    model = ScriptedModel("The pool opens at 07:30.")
    world.use(model)
    insert_conversation(world, "viewer2", days_ago=40)
    insert_conversation(world, "owner_b", days_ago=40, hotel=world.b)

    assert purge(world).conversations_deleted == 2

    assert model.calls == []
    # One question per actor and per hotel is the whole allowance: had the purge spent any of
    # it, this would be a 429.
    assert world.start("owner", world.a).status_code == 201
    assert world.start("owner_b", world.b).status_code == 201


def test_the_job_takes_no_actor() -> None:
    parameters = list(inspect.signature(purge_expired_conversations).parameters)
    assert parameters == ["session", "settings", "batch_size"]
    source = inspect.getsource(purge_expired_conversations)
    for forbidden in ("actor", "current_user", "AuditTrail", "ChatModel", "budget"):
        assert forbidden not in source.split('"""')[-1], forbidden


# --- I. the per-hotel purge is unchanged -----------------------------------------------------


def test_starting_a_conversation_still_purges_only_that_hotel(world: World) -> None:
    here = insert_conversation(world, "viewer2", days_ago=40, turns=2)
    elsewhere = insert_conversation(world, "owner_b", days_ago=40, hotel=world.b)

    started(world, "owner")

    assert not exists(world, here)
    assert exists(world, elsewhere)


def test_continuing_a_conversation_still_purges_only_that_hotel(world: World) -> None:
    conversation = started(world, "owner")
    here = insert_conversation(world, "viewer2", days_ago=40, turns=2)
    elsewhere = insert_conversation(world, "owner_b", days_ago=40, hotel=world.b)

    assert world.say("owner", world.a, conversation).status_code == 200

    assert not exists(world, here)
    assert exists(world, elsewhere)


def test_the_per_hotel_purge_keeps_its_bound(world: World) -> None:
    for days in range(40, 45):
        insert_conversation(world, "owner", days_ago=days)
    retention = CopilotConversationRetention(
        world.session, CopilotConversationRepository(world.session), retention_days=30
    )
    assert retention.purge(limit=3, hotel_id=world.a.id) == 3
    assert retention.purge(hotel_id=world.a.id) == 2
    assert inspect.signature(retention.purge).parameters["limit"].default == PURGE_BATCH


# --- J. the command ----------------------------------------------------------------------------


def run_command(
    tmp_path: Path, database_url: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    """``python -m app.jobs.purge_conversations`` as an operator runs it, in a fresh process.

    Run from an empty directory so no ``.env`` is read, with every ``POSTGRES_*`` variable removed
    and ``DATABASE_URL`` set explicitly: the command can reach this test database and nothing else.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("POSTGRES_", "DATABASE_URL"))
    }
    environment["DATABASE_URL"] = database_url
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "backend")
    return subprocess.run(
        [sys.executable, "-m", "app.jobs.purge_conversations", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_module_command_purges_and_exits_zero(world: World, tmp_path: Path) -> None:
    expired = insert_conversation(world, "owner", days_ago=40, turns=3)
    elsewhere = insert_conversation(world, "owner_b", days_ago=40, hotel=world.b)
    live = insert_conversation(world, "viewer2", days_ago=1)
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL)

    assert completed.returncode == 0, completed.stderr
    match = OUTPUT.match(completed.stdout.strip())
    assert match is not None, completed.stdout
    assert match.groups() == ("2", "1", "30")
    assert not exists(world, expired) and not exists(world, elsewhere)
    assert exists(world, live)
    for secret in (expired, elsewhere, live, "old question", "old answer", TEST_DATABASE_URL):
        assert secret not in completed.stdout
        assert secret not in completed.stderr


def test_the_module_command_exits_non_zero_without_leaking_the_connection(tmp_path: Path) -> None:
    # A closed port, with a connect timeout: on Windows a refused connection is retried for
    # minutes otherwise, and the point of the test is the exit status, not the wait.
    unreachable = (
        "postgresql+psycopg://purge-user:PASSWORD-SENTINEL@127.0.0.1:1/nowhere?connect_timeout=3"
    )

    completed = run_command(tmp_path, unreachable)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "FAILED (OperationalError)" in completed.stderr
    for secret in ("PASSWORD-SENTINEL", "purge-user", "nowhere", "Traceback"):
        assert secret not in completed.stderr


def test_the_module_command_refuses_an_unknown_argument(tmp_path: Path) -> None:
    """A usage error is refused before any connection: the URL is never used, so none is real."""
    unused = "postgresql+psycopg://nobody:unused@127.0.0.1:1/unused?connect_timeout=1"
    completed = run_command(tmp_path, unused, "--hotel", "anything")
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "unrecognized arguments" in completed.stderr
