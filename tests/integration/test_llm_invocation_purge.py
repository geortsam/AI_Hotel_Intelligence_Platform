"""The `llm_invocations` retention contract (Issue 4), over real PostgreSQL.

A record is kept `llm_invocation_retention_days` (365 by default) after `created_at`, a day being
exactly 24 hours, and is then physically deleted by `purge_expired_invocations(session, settings)`
-- `python -m app.jobs.purge_llm_invocations` -- one hotel and one bounded batch at a time.
Migration 0017's trigger still refuses every UPDATE, and refuses every DELETE except one the
transaction has declared a retention period for, of a row at least that old.

These tests pin: the boundary to the microsecond and to the second, the configured period, every
hotel and every user, tenant scope, empty and already-clean tables, repetition, batching,
failure and retry, the trigger's refusals, the support reference after expiry, hotel and user
deletion, independence from the conversation purge, privacy, and the command.

Records are written directly with back-dated `created_at` (the application never back-dates
one), on the Stage 7.11 conversation fixtures: hotels A and B, and their callers.
"""

from __future__ import annotations

import datetime as dt
import inspect
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.jobs.purge_llm_invocations import main
from app.llm.testing import ScriptedModel
from app.models.hotel import Hotel
from app.repositories.llm_invocation import RETENTION_DECLARATION, LlmInvocationRepository
from app.services.copilot_conversation import purge_expired_conversations
from app.services.llm_invocation_retention import (
    PURGE_BATCH,
    InvocationPurgeResult,
    purge_expired_invocations,
)
from tests.integration.conftest import TEST_DATABASE_URL, make_hotel, requires_postgres
from tests.integration.test_copilot_conversations_api import (
    World,
    build,
    insert_conversation,
    rows,
    scalar,
    started,
)

pytestmark = requires_postgres

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DAY = 24  # hours: a retention day is exactly 24 of them
YEAR = 365 * DAY
OUTPUT = re.compile(
    r"^llm invocation purge: (\d+) expired invocation record\(s\) deleted at (\d+) hotel\(s\) "
    r"in (\d+) batch\(es\); retention (\d+) day\(s\)$"
)
RESTRICT_VIOLATION = "23001"
HOTEL_KEY = "fk_llm_invocations_hotel_id_hotels"
ACTOR_KEY = "fk_llm_invocations_actor_user_id_users"


@pytest.fixture
def world(engine: Engine, session: Session) -> World:
    return build(engine, session)


def settings(retention_days: int = 365, **overrides: Any) -> Settings:
    return Settings(
        environment="test",
        database_url=TEST_DATABASE_URL,
        llm_invocation_retention_days=retention_days,
        **overrides,
    )


def purge(world: World, retention_days: int = 365, **kwargs: Any) -> InvocationPurgeResult:
    return purge_expired_invocations(world.session, settings(retention_days), **kwargs)


def insert_sql(created_at: str) -> str:
    """An INSERT of one record whose `created_at` is the SQL expression *created_at*."""
    return (
        "INSERT INTO llm_invocations (hotel_id, actor_user_id, prompt_id, prompt_version, "
        "provider, model, stop_reason, complete, rounds, model_calls, tool_calls, tool_failures, "
        "input_tokens, output_tokens, latency_ms, request_id, created_at) VALUES (:h, :u, "
        "'copilot_answer', 'v2', 'p', 'm', 'completed', true, 0, 1, 0, 0, 10, 5, 7, :r, "
        f"{created_at}) RETURNING public_id"
    )


INSERT = insert_sql("now() - make_interval(hours => :hours, secs => :secs)")


def insert_invocation(
    world: World,
    name: str = "owner",
    *,
    hours: int,
    seconds: int = 0,
    hotel: Hotel | None = None,
    request_id: str | None = None,
) -> str:
    """One committed record, `hours` hours and `seconds` seconds old. A test fixture only."""
    hotel = hotel or world.a
    with world.engine.begin() as connection:
        public_id = connection.execute(
            sa.text(INSERT),
            {
                "h": hotel.id,
                "u": world.user_id(name),
                "r": request_id,
                "hours": hours,
                "secs": seconds,
            },
        ).scalar_one()
    return str(public_id)


def exists(world: World, public_id: str) -> bool:
    return bool(
        rows(world.session, "SELECT 1 FROM llm_invocations WHERE public_id = :p", p=public_id)
    )


def count(world: World, hotel: Hotel | None = None) -> int:
    if hotel is None:
        return int(scalar(world.session, "SELECT count(*) FROM llm_invocations"))
    return int(
        scalar(
            world.session, "SELECT count(*) FROM llm_invocations WHERE hotel_id = :h", h=hotel.id
        )
    )


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


@pytest.fixture
def connection(engine: Engine) -> Iterator[sa.Connection]:
    """A raw connection whose single transaction is rolled back: the trigger, seen directly."""
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


# ======================================================================================
# A. What expires: the boundary
# ======================================================================================


def test_a_record_one_second_past_the_period_is_deleted_and_one_second_inside_is_kept(
    world: World,
) -> None:
    outside = insert_invocation(world, hours=YEAR, seconds=1)
    inside = insert_invocation(world, hours=YEAR, seconds=-1)

    result = purge(world)

    assert result.invocations_deleted == 1
    assert not exists(world, outside)
    assert exists(world, inside)


def test_a_record_one_day_past_the_period_is_deleted_and_one_day_inside_is_kept(
    world: World,
) -> None:
    outside = insert_invocation(world, hours=YEAR + DAY)
    inside = insert_invocation(world, hours=YEAR - DAY)
    recent = insert_invocation(world, hours=0)

    assert purge(world).invocations_deleted == 1

    assert not exists(world, outside)
    assert exists(world, inside) and exists(world, recent)


def test_the_boundary_is_exact_and_inclusive(world: World) -> None:
    """Inside one transaction `now()` does not move, so the cutoff can be hit to the microsecond:
    a record exactly `retention` old is expired, one a microsecond younger is not -- the
    repository's rule and the trigger's agree on both."""
    session = world.session
    hotel_id = world.a.id
    actor = world.user_id("owner")
    at_offset = insert_sql("now() - make_interval(hours => :hours) + CAST(:offset AS interval)")
    for offset in ("0", "1 microsecond"):
        session.execute(
            sa.text(at_offset),
            {"h": hotel_id, "u": actor, "r": None, "hours": YEAR, "offset": offset},
        )

    deleted = LlmInvocationRepository(session).purge_expired(hotel_id, 365, limit=10)

    assert deleted == 1
    [age] = session.execute(sa.text("SELECT now() - created_at FROM llm_invocations")).scalars()
    assert age == dt.timedelta(days=365) - dt.timedelta(microseconds=1)
    session.rollback()


@pytest.mark.parametrize("zone", ["UTC", "Europe/Athens", "America/New_York", "Pacific/Chatham"])
def test_a_retention_day_is_24_hours_whatever_the_session_time_zone(
    world: World, zone: str
) -> None:
    """The cutoff is `now() - retention * 24 hours`, an absolute instant: the session's zone,
    and any daylight-saving change in the window, cannot move it."""
    session = world.session
    session.execute(sa.text(f"SET LOCAL TIME ZONE '{zone}'"))
    hotel_id = world.a.id
    actor = world.user_id("owner")
    for seconds in (1, -1):
        session.execute(
            sa.text(INSERT),
            {"h": hotel_id, "u": actor, "r": None, "hours": YEAR, "secs": seconds},
        )

    assert LlmInvocationRepository(session).purge_expired(hotel_id, 365, limit=10) == 1
    assert int(session.execute(sa.text("SELECT count(*) FROM llm_invocations")).scalar_one()) == 1
    session.rollback()


# ======================================================================================
# B. The configured period is the cutoff
# ======================================================================================


def test_the_default_period_is_365_days(world: World) -> None:
    insert_invocation(world, hours=YEAR + 1)
    insert_invocation(world, hours=YEAR - 1)

    result = purge_expired_invocations(
        world.session, Settings(environment="test", database_url=TEST_DATABASE_URL)
    )

    assert (result.invocations_deleted, result.retention_days) == (1, 365)


def test_the_configured_retention_period_decides_what_is_deleted(world: World) -> None:
    hundred = insert_invocation(world, hours=100 * DAY)
    forty = insert_invocation(world, hours=40 * DAY)

    assert purge(world, retention_days=365).invocations_deleted == 0
    assert purge(world, retention_days=120).invocations_deleted == 0
    assert exists(world, hundred) and exists(world, forty)

    result = purge(world, retention_days=60)
    assert (result.invocations_deleted, result.retention_days) == (1, 60)
    assert not exists(world, hundred)
    assert exists(world, forty)


# ======================================================================================
# C. Every hotel, every user -- and one hotel per statement
# ======================================================================================


def test_expired_records_at_every_hotel_and_of_every_user_are_deleted(world: World) -> None:
    expired = [
        insert_invocation(world, "owner", hours=YEAR + DAY),
        insert_invocation(world, "viewer2", hours=YEAR + 2 * DAY),
        insert_invocation(world, "dual", hours=YEAR + 3 * DAY),
        insert_invocation(world, "owner_b", hours=YEAR + DAY, hotel=world.b),
        insert_invocation(world, "dual", hours=YEAR + 9 * DAY, hotel=world.b),
    ]
    live = [
        insert_invocation(world, "owner", hours=DAY),
        insert_invocation(world, "dual", hours=DAY, hotel=world.b),
    ]

    result = purge(world)

    assert (result.invocations_deleted, result.hotels) == (5, 2)
    assert not any(exists(world, p) for p in expired)
    assert all(exists(world, p) for p in live)


def test_one_hotels_purge_never_touches_another_hotel(world: World) -> None:
    at_a = insert_invocation(world, "dual", hours=YEAR + DAY)
    at_b = insert_invocation(world, "dual", hours=YEAR + DAY, hotel=world.b)
    repository = LlmInvocationRepository(world.session)

    assert repository.purge_expired(world.a.id, 365, limit=PURGE_BATCH) == 1
    world.session.commit()

    assert not exists(world, at_a)
    assert exists(world, at_b)


def test_the_hotels_with_work_are_found_by_their_expired_records_alone(world: World) -> None:
    insert_invocation(world, hours=YEAR + DAY, hotel=world.b)
    insert_invocation(world, hours=DAY)  # A holds only a live record

    assert list(LlmInvocationRepository(world.session).hotels_with_expired(365)) == [world.b.id]
    world.session.rollback()


# ======================================================================================
# D. Empty, already clean, repeated
# ======================================================================================


def test_an_empty_table_is_a_successful_purge_of_nothing(world: World) -> None:
    assert count(world) == 0
    assert purge(world) == InvocationPurgeResult(
        invocations_deleted=0, hotels=0, batches=0, retention_days=365
    )


def test_a_table_with_nothing_expired_is_left_exactly_as_it_was(world: World) -> None:
    kept = [insert_invocation(world, hours=h) for h in (0, DAY, YEAR - 1)]

    assert purge(world).invocations_deleted == 0
    assert all(exists(world, p) for p in kept)


def test_running_the_purge_again_is_safe_and_finds_nothing(world: World) -> None:
    insert_invocation(world, hours=YEAR + DAY)
    live = insert_invocation(world, hours=DAY)

    first = purge(world)
    second = purge(world)
    third = purge(world)

    assert first.invocations_deleted == 1
    assert second == third == InvocationPurgeResult(0, 0, 0, 365)
    assert exists(world, live)


# ======================================================================================
# E. Bounded batches, oldest first, failure and retry
# ======================================================================================


def test_more_than_one_batch_is_processed_to_completion_at_every_hotel(world: World) -> None:
    for extra in range(5):
        insert_invocation(world, hours=YEAR + DAY + extra)
    for extra in range(3):
        insert_invocation(world, "owner_b", hours=YEAR + DAY + extra, hotel=world.b)
    live = insert_invocation(world, hours=DAY)

    result = purge(world, batch_size=2)

    # A: 2 + 2 + 1, B: 2 + 1.
    assert (result.invocations_deleted, result.hotels, result.batches) == (8, 2, 5)
    assert [
        str(r["public_id"]) for r in rows(world.session, "SELECT public_id FROM llm_invocations")
    ] == [live]


def test_a_full_last_batch_is_followed_by_one_that_confirms_nothing_is_left(world: World) -> None:
    for extra in range(4):
        insert_invocation(world, hours=YEAR + DAY + extra)

    result = purge(world, batch_size=2)

    assert (result.invocations_deleted, result.batches) == (4, 3)  # 2 + 2 + 0


def test_a_batch_deletes_the_oldest_expired_records_first(world: World) -> None:
    ages = {extra: insert_invocation(world, hours=YEAR + extra * DAY) for extra in (1, 2, 3, 4)}

    assert LlmInvocationRepository(world.session).purge_expired(world.a.id, 365, limit=2) == 2
    world.session.commit()

    assert not exists(world, ages[4]) and not exists(world, ages[3])
    assert exists(world, ages[2]) and exists(world, ages[1])


def test_a_failed_purge_keeps_every_completed_batch_and_a_retry_finishes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for extra in range(5):
        insert_invocation(world, hours=YEAR + DAY + extra)
    live = insert_invocation(world, hours=DAY)
    real = LlmInvocationRepository.purge_expired
    calls = {"n": 0}

    def fail_on_second_batch(self: LlmInvocationRepository, *args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("connection lost mid-purge")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(LlmInvocationRepository, "purge_expired", fail_on_second_batch)
    with pytest.raises(RuntimeError):
        purge(world, batch_size=2)

    # The first batch committed; nothing past it was deleted, and nothing was half-deleted.
    assert count(world) == 4
    monkeypatch.setattr(LlmInvocationRepository, "purge_expired", real)

    retry = purge(world, batch_size=2)

    assert retry.invocations_deleted == 3
    assert [
        str(r["public_id"]) for r in rows(world.session, "SELECT public_id FROM llm_invocations")
    ] == [live]


def test_the_default_batch_is_a_thousand_and_zero_is_refused(world: World) -> None:
    assert PURGE_BATCH == 1000
    assert inspect.signature(purge_expired_invocations).parameters["batch_size"].default == 1000
    with pytest.raises(ValueError):
        purge(world, batch_size=0)


# ======================================================================================
# F. The database's own rule (migration 0017)
# ======================================================================================


def _row(connection: sa.Connection, hotel: Hotel, actor: int, hours: int) -> None:
    connection.execute(
        sa.text(INSERT), {"h": hotel.id, "u": actor, "r": None, "hours": hours, "secs": 0}
    )


def _declare(connection: sa.Connection, hours: str) -> None:
    connection.execute(
        sa.text("SELECT set_config(:name, :hours, true)"),
        {"name": RETENTION_DECLARATION, "hours": hours},
    )


def test_an_undeclared_delete_is_refused_however_old_the_record(
    world: World, connection: sa.Connection
) -> None:
    _row(connection, world.a, world.user_id("owner"), YEAR * 5)
    with pytest.raises(DBAPIError, match="append-only: DELETE") as refused:
        connection.execute(sa.text("DELETE FROM llm_invocations"))
    assert sqlstate(refused.value) == RESTRICT_VIOLATION


def test_a_declared_delete_of_a_record_inside_its_period_is_refused(
    world: World, connection: sa.Connection
) -> None:
    _row(connection, world.a, world.user_id("owner"), YEAR - 1)
    _declare(connection, str(YEAR))
    with pytest.raises(DBAPIError, match="inside its retention period") as refused:
        connection.execute(sa.text("DELETE FROM llm_invocations"))
    assert sqlstate(refused.value) == RESTRICT_VIOLATION


def test_a_declared_period_shorter_than_a_day_is_refused_whatever_the_record(
    world: World, connection: sa.Connection
) -> None:
    _row(connection, world.a, world.user_id("owner"), YEAR * 2)
    _declare(connection, "23")
    with pytest.raises(DBAPIError, match="at least 24 hours") as refused:
        connection.execute(sa.text("DELETE FROM llm_invocations"))
    assert sqlstate(refused.value) == RESTRICT_VIOLATION


def test_a_declared_period_of_exactly_a_day_deletes_what_is_older(
    world: World, connection: sa.Connection
) -> None:
    _row(connection, world.a, world.user_id("owner"), DAY + 1)
    _declare(connection, str(DAY))
    assert connection.execute(sa.text("DELETE FROM llm_invocations")).rowcount == 1


def test_an_update_is_refused_even_inside_a_declared_purge(
    world: World, connection: sa.Connection
) -> None:
    _row(connection, world.a, world.user_id("owner"), YEAR * 2)
    _declare(connection, str(YEAR))
    with pytest.raises(DBAPIError, match="append-only: UPDATE") as refused:
        connection.execute(sa.text("UPDATE llm_invocations SET latency_ms = 0"))
    assert sqlstate(refused.value) == RESTRICT_VIOLATION


def test_a_declaration_ends_with_its_transaction(world: World, engine: Engine) -> None:
    insert_invocation(world, hours=YEAR * 2)
    with engine.connect() as conn:
        declaring = conn.begin()
        _declare(conn, str(YEAR))
        declaring.commit()
        later = conn.begin()  # the same connection, a new transaction: nothing declared
        try:
            assert conn.execute(
                sa.text("SELECT current_setting(:n, true)"), {"n": RETENTION_DECLARATION}
            ).scalar_one() in (None, "")
            with pytest.raises(DBAPIError, match="append-only: DELETE"):
                conn.execute(sa.text("DELETE FROM llm_invocations"))
        finally:
            later.rollback()
    assert count(world) == 1


def test_a_record_written_by_a_real_request_is_still_append_only(world: World) -> None:
    """The Stage 7.7 guarantee, unchanged for every record the purge has not expired."""
    started(world, "owner")
    assert count(world) == 1
    with pytest.raises(DBAPIError):
        world.session.execute(sa.text("UPDATE llm_invocations SET latency_ms = 0"))
    world.session.rollback()
    with pytest.raises(DBAPIError):
        world.session.execute(sa.text("DELETE FROM llm_invocations"))
    world.session.rollback()
    assert purge(world).invocations_deleted == 0
    assert count(world) == 1


# ======================================================================================
# G. The support reference after expiry
# ======================================================================================


def test_a_support_reference_resolves_until_its_record_expires_and_then_to_nothing(
    world: World,
) -> None:
    conversation = world.start("owner", world.a)
    assert conversation.status_code == 201, conversation.text
    reference = conversation.json()["turn"]["invocation_public_id"]
    expired = insert_invocation(world, hours=YEAR + DAY)
    for public_id in (reference, expired):
        assert exists(world, public_id)

    purge(world)

    assert exists(world, reference)  # a fresh answer's reference still names its record
    assert not exists(world, expired)  # an expired one names nothing


# ======================================================================================
# H. Hotel and user deletion
# ======================================================================================


def _delete(session: Session, sql: str, **params: Any) -> str | None:
    """Try a DELETE in a savepoint: the constraint that refused it, or None if it succeeded."""
    savepoint = session.begin_nested()
    try:
        session.execute(sa.text(sql), params)
    except IntegrityError as error:
        savepoint.rollback()
        return str(error.orig.diag.constraint_name)  # type: ignore[union-attr]
    savepoint.commit()
    return None


def test_a_hotel_whose_only_history_is_expired_becomes_deletable_after_the_purge(
    world: World,
) -> None:
    quiet = make_hotel(world.session, slug="retired-hotel")
    world.session.commit()
    insert_invocation(world, hours=YEAR + DAY, hotel=quiet)

    assert _delete(world.session, "DELETE FROM hotels WHERE id = :h", h=quiet.id) == HOTEL_KEY
    world.session.rollback()

    purge(world)

    assert _delete(world.session, "DELETE FROM hotels WHERE id = :h", h=quiet.id) is None
    world.session.rollback()


def test_a_hotel_with_an_unexpired_record_stays_undeletable(world: World) -> None:
    busy = make_hotel(world.session, slug="busy-hotel")
    world.session.commit()
    insert_invocation(world, hours=YEAR + DAY, hotel=busy)
    insert_invocation(world, hours=DAY, hotel=busy)

    purge(world)

    assert count(world, busy) == 1
    assert _delete(world.session, "DELETE FROM hotels WHERE id = :h", h=busy.id) == HOTEL_KEY
    world.session.rollback()


def test_a_user_whose_only_records_are_expired_stops_being_held_by_this_table(
    world: World,
) -> None:
    leaver = world.user_id("outsider")  # a member of nothing
    insert_invocation(world, "outsider", hours=YEAR + DAY)

    assert _delete(world.session, "DELETE FROM users WHERE id = :u", u=leaver) == ACTOR_KEY
    world.session.rollback()

    purge(world)

    assert _delete(world.session, "DELETE FROM users WHERE id = :u", u=leaver) is None
    world.session.rollback()


# ======================================================================================
# I. Independent of the conversation purge
# ======================================================================================


def test_the_conversation_purge_never_touches_an_invocation_record(world: World) -> None:
    expired_record = insert_invocation(world, hours=YEAR + DAY)
    insert_conversation(world, "owner", days_ago=40, turns=2)

    assert purge_expired_conversations(world.session, settings()).conversations_deleted == 1

    assert exists(world, expired_record)


def test_the_invocation_purge_never_touches_a_conversation_or_the_audit_trail(
    world: World,
) -> None:
    expired_conversation = insert_conversation(world, "owner", days_ago=40, turns=2)
    live = started(world, "viewer2")
    insert_invocation(world, hours=YEAR + DAY)
    before = (
        int(scalar(world.session, "SELECT count(*) FROM copilot_conversations")),
        int(scalar(world.session, "SELECT count(*) FROM copilot_messages")),
        int(scalar(world.session, "SELECT count(*) FROM audit_events")),
    )

    assert purge(world).invocations_deleted == 1

    after = (
        int(scalar(world.session, "SELECT count(*) FROM copilot_conversations")),
        int(scalar(world.session, "SELECT count(*) FROM copilot_messages")),
        int(scalar(world.session, "SELECT count(*) FROM audit_events")),
    )
    assert after == before
    assert rows(
        world.session,
        "SELECT 1 FROM copilot_conversations WHERE public_id IN (:a, :b)",
        a=expired_conversation,
        b=live,
    )


def test_the_purge_calls_no_model_and_charges_no_allowance(
    engine: Engine, session: Session
) -> None:
    world = build(engine, session, copilot_actor_rate_limit=1, copilot_hotel_rate_limit=1)
    model = ScriptedModel("The pool opens at 07:30.")
    world.use(model)
    insert_invocation(world, hours=YEAR + DAY)
    insert_invocation(world, "owner_b", hours=YEAR + DAY, hotel=world.b)

    assert purge(world).invocations_deleted == 2

    assert model.calls == []
    # One question per actor and per hotel is the whole allowance: had the purge spent any of
    # it, this would be a 429.
    assert world.start("owner", world.a).status_code == 201
    assert world.start("owner_b", world.b).status_code == 201


def test_the_job_takes_no_actor() -> None:
    assert list(inspect.signature(purge_expired_invocations).parameters) == [
        "session",
        "settings",
        "batch_size",
    ]
    source = inspect.getsource(purge_expired_invocations).split('"""')[-1]
    for forbidden in ("actor", "current_user", "AuditTrail", "ChatModel", "budget"):
        assert forbidden not in source, forbidden


# ======================================================================================
# J. The command
# ======================================================================================


def test_the_command_output_is_counts_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    expired = insert_invocation(world, hours=YEAR + DAY, request_id="REQUEST-SENTINEL-1")
    live = insert_invocation(world, hours=DAY, request_id="REQUEST-SENTINEL-2")

    assert main([], settings=settings()) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    match = OUTPUT.match(captured.out.strip())
    assert match is not None, captured.out
    assert match.groups() == ("1", "1", "1", "365")
    for secret in (expired, live, "REQUEST-SENTINEL", str(world.a.public_id)):
        assert secret not in captured.out
    assert TEST_DATABASE_URL is not None and TEST_DATABASE_URL not in captured.out


def run_command(
    tmp_path: Path, database_url: str, *arguments: str, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    """``python -m app.jobs.purge_llm_invocations`` as an operator runs it, in a fresh process.

    Run from an empty directory so no ``.env`` is read, with every ``POSTGRES_*`` variable removed
    and ``DATABASE_URL`` set explicitly: the command can reach this test database and nothing else.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("POSTGRES_", "DATABASE_URL", "LLM_", "COPILOT_"))
    }
    environment["DATABASE_URL"] = database_url
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "backend")
    environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "app.jobs.purge_llm_invocations", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_module_command_purges_and_exits_zero(world: World, tmp_path: Path) -> None:
    expired = insert_invocation(world, hours=YEAR + DAY)
    elsewhere = insert_invocation(world, "owner_b", hours=YEAR + DAY, hotel=world.b)
    live = insert_invocation(world, hours=DAY)
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL)

    assert completed.returncode == 0, completed.stderr
    match = OUTPUT.match(completed.stdout.strip())
    assert match is not None, completed.stdout
    assert match.groups() == ("2", "2", "2", "365")
    assert not exists(world, expired) and not exists(world, elsewhere)
    assert exists(world, live)
    for secret in (expired, elsewhere, live, TEST_DATABASE_URL):
        assert secret not in completed.stdout
        assert secret not in completed.stderr


def test_the_module_command_reads_the_retention_from_the_environment(
    world: World, tmp_path: Path
) -> None:
    forty = insert_invocation(world, hours=40 * DAY)
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL, LLM_INVOCATION_RETENTION_DAYS="35")

    assert completed.returncode == 0, completed.stderr
    match = OUTPUT.match(completed.stdout.strip())
    assert match is not None and match.groups() == ("1", "1", "1", "35"), completed.stdout
    assert not exists(world, forty)


def test_the_module_command_refuses_an_invalid_retention_without_deleting(
    world: World, tmp_path: Path
) -> None:
    """Shorter than the conversation retention: a configuration error, exit 1, nothing deleted."""
    expired = insert_invocation(world, hours=YEAR + DAY)
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL, LLM_INVOCATION_RETENTION_DAYS="7")

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "FAILED (ValidationError)" in completed.stderr
    assert exists(world, expired)


def test_the_module_command_exits_non_zero_without_leaking_the_connection(tmp_path: Path) -> None:
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
