"""Audit retention and archival against real PostgreSQL.

Stage 4.5.14. Three claims are made here and none of them can be settled anywhere but against a
live database.

**The archive is immutable, and so is the source.** Both tables carry a trigger that raises on
UPDATE and DELETE. A mock cannot refuse a write; only PostgreSQL can, and the immutability
section below makes it refuse four times.

**Archival is idempotent and concurrency-safe in the database, not in Python.** The primary key
of the archive is the original event id, so a repeat run and a second worker are both refused
by ``pk_audit_events_archive`` rather than by a check somebody could remove. The concurrency
section opens two real connections and makes them contend.

**Nothing is deleted.** Every test that archives also asserts the active table still holds the
row. That is the stage's central decision -- see :mod:`app.services.retention` -- and it is
worth failing loudly if it is ever quietly reversed.

Events are INSERTed directly with a chosen ``occurred_at``: the retention window is measured in
days and no API can produce a two-year-old event. A plain INSERT is available for the same
reason TRUNCATE is -- migration 0007 forbids UPDATE and DELETE on the audit table, not writes
to it.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.repositories.audit_archive import AuditArchiveRepository
from app.services.retention import (
    ArchivalResult,
    ArchiveVerificationError,
    AuditRetentionService,
    RetentionPolicy,
    archive_audit_events,
)
from tests.integration.conftest import requires_postgres

pytestmark = requires_postgres

#: A fixed "now", so every cutoff in this file is exact rather than relative to the clock.
NOW = dt.datetime(2027, 6, 1, 12, 0, 0, tzinfo=dt.UTC)


# ======================================================================================
# Scaffolding
# ======================================================================================


@pytest.fixture(autouse=True)
def _clean(engine: Engine) -> Iterator[None]:
    """Both audit tables, emptied around every test in this module.

    TRUNCATE is neither an UPDATE nor a DELETE, so neither append-only trigger fires -- the
    same distinction migrations 0007 and 0008 both rely on.
    """

    def wipe() -> None:
        with sessionmaker(bind=engine, future=True)() as s:
            s.execute(sa.text("TRUNCATE audit_events_archive"))
            s.execute(sa.text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
            s.commit()

    wipe()
    yield
    wipe()


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "audit_retention_days": 730,
        "audit_archive_batch_size": 1000,
        "audit_archive_max_batches": 100,
    }
    base.update(overrides)
    return Settings(**base)


def seed(
    session: Session,
    *,
    count: int,
    age_days: int,
    hotel_id: int | None = None,
    actor_user_id: int | None = None,
    action: str = "amenity.created",
    resource_type: str = "amenity",
    prefix: str = "EV",
    request_id: str | None = None,
    details: str = "{}",
) -> None:
    """Insert *count* audit events aged *age_days* before :data:`NOW`."""
    session.execute(
        sa.text(
            "INSERT INTO audit_events "
            "(hotel_id, actor_user_id, action, resource_type, resource_reference, "
            " request_id, details, occurred_at) "
            "SELECT :hotel, :actor, :action, :rtype, :prefix || g, :req, "
            "       CAST(:details AS jsonb), CAST(:when AS timestamptz) "
            "FROM generate_series(1, :count) g"
        ),
        {
            "hotel": hotel_id,
            "actor": actor_user_id,
            "action": action,
            "rtype": resource_type,
            "prefix": prefix,
            "req": request_id,
            "details": details,
            "when": NOW - dt.timedelta(days=age_days),
            "count": count,
        },
    )
    session.commit()


def make_hotel_row(session: Session, slug: str = "retention-hotel") -> tuple[int, uuid.UUID]:
    row = session.execute(
        sa.text(
            "INSERT INTO hotels (name, slug, address_line1, city, country_code, timezone, "
            "currency) VALUES (:n, :s, '1 Test Street', 'Athens', 'GR', 'Europe/Athens', 'EUR') "
            "RETURNING id, public_id"
        ),
        {"n": f"Hotel {slug}", "s": slug},
    ).one()
    session.commit()
    return int(row[0]), row[1]


def make_user_row(session: Session, email: str = "retention@example.test") -> tuple[int, uuid.UUID]:
    row = session.execute(
        sa.text(
            "INSERT INTO users (email, password_hash, full_name) "
            "VALUES (:e, 'argon2-digest-placeholder', 'Retention Suite') "
            "RETURNING id, public_id"
        ),
        {"e": email},
    ).one()
    session.commit()
    return int(row[0]), row[1]


def run(session: Session, **overrides: Any) -> ArchivalResult:
    return archive_audit_events(session, settings(**overrides), now=NOW)


def archive_rows(session: Session) -> list[dict[str, Any]]:
    session.rollback()
    return [
        dict(row)
        for row in session.execute(
            sa.text("SELECT * FROM audit_events_archive ORDER BY audit_event_id")
        ).mappings()
    ]


def counts(session: Session) -> tuple[int, int]:
    session.rollback()
    return (
        int(session.scalar(sa.text("SELECT count(*) FROM audit_events")) or 0),
        int(session.scalar(sa.text("SELECT count(*) FROM audit_events_archive")) or 0),
    )


# ======================================================================================
# Schema
# ======================================================================================


def test_the_archive_table_exists_with_the_expected_columns(session: Session) -> None:
    columns = {
        row[0]
        for row in session.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'audit_events_archive'"
            )
        )
    }

    assert columns == {
        "audit_event_id",
        "public_id",
        "hotel_id",
        "hotel_public_id",
        "actor_user_id",
        "actor_public_id",
        "action",
        "resource_type",
        "resource_reference",
        "request_id",
        "details",
        "occurred_at",
        "archived_at",
    }


def test_the_primary_key_is_the_original_event_id(session: Session) -> None:
    """Not a surrogate. This is what makes idempotency a database guarantee."""
    key = [
        row[0]
        for row in session.execute(
            sa.text(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indrelid "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
                "WHERE c.relname = 'audit_events_archive' AND i.indisprimary"
            )
        )
    ]

    assert key == ["audit_event_id"]


def test_the_public_id_is_unique(session: Session) -> None:
    names = {
        row[0]
        for row in session.execute(
            sa.text(
                "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'audit_events_archive' AND c.contype = 'u'"
            )
        )
    }

    assert names == {"uq_audit_events_archive_public_id"}


def test_the_time_window_index_exists(session: Session) -> None:
    definition = session.scalar(
        sa.text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_audit_events_archive_occurred_at'"
        )
    )

    assert definition is not None
    assert "occurred_at DESC" in str(definition)
    assert "audit_event_id DESC" in str(definition)


def test_the_archive_has_no_foreign_keys(session: Session) -> None:
    """Deliberate. An archive that cannot outlive the operational rows it describes is not
    forensic evidence, and RESTRICT here would buy nothing the source table does not already."""
    count = session.scalar(
        sa.text(
            "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = 'audit_events_archive' AND c.contype = 'f'"
        )
    )

    assert count == 0


def test_the_append_only_trigger_exists(session: Session) -> None:
    triggers = {
        row[0]
        for row in session.execute(
            sa.text(
                "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'audit_events_archive' AND NOT t.tgisinternal"
            )
        )
    }

    assert triggers == {"trg_audit_events_archive_append_only"}


# ======================================================================================
# Policy
# ======================================================================================


def test_the_cutoff_is_the_configured_number_of_days_back() -> None:
    policy = RetentionPolicy.from_settings(settings(audit_retention_days=730))

    assert policy.cutoff(NOW) == NOW - dt.timedelta(days=730)


def test_an_event_exactly_on_the_boundary_is_retained(session: Session) -> None:
    """Strictly ``<``. "Retained for N days" must mean at least N, not almost N."""
    seed(session, count=1, age_days=730)

    result = run(session, audit_retention_days=730)

    assert result.examined == 0
    assert counts(session) == (1, 0)


def test_an_event_one_second_past_the_boundary_is_eligible(session: Session) -> None:
    session.execute(
        sa.text(
            "INSERT INTO audit_events (action, resource_type, resource_reference, occurred_at) "
            "VALUES ('amenity.created', 'amenity', 'EDGE', CAST(:when AS timestamptz))"
        ),
        {"when": NOW - dt.timedelta(days=730, seconds=1)},
    )
    session.commit()

    result = run(session, audit_retention_days=730)

    assert result.examined == 1
    assert result.archived == 1


def test_a_shorter_configured_retention_makes_more_events_eligible(session: Session) -> None:
    seed(session, count=3, age_days=100, prefix="OLD")
    seed(session, count=2, age_days=10, prefix="NEW")

    assert run(session, audit_retention_days=730).archived == 0
    assert run(session, audit_retention_days=50).archived == 3
    assert run(session, audit_retention_days=5).archived == 2


# ======================================================================================
# Archival
# ======================================================================================


def test_an_eligible_event_is_archived(session: Session) -> None:
    seed(session, count=1, age_days=900)

    result = run(session)

    assert result.archived == 1
    assert counts(session) == (1, 1)


def test_a_non_eligible_event_is_untouched(session: Session) -> None:
    seed(session, count=4, age_days=10)

    result = run(session)

    assert result.examined == 0
    assert result.archived == 0
    assert counts(session) == (4, 0)


def test_the_original_event_is_never_deleted(session: Session) -> None:
    """The stage's central decision, asserted directly.

    ``audit_events`` is append-only at the database level, so retention here means "secured a
    durable copy", not "freed the space". If this ever fails, the append-only guarantee has
    been weakened to implement retention -- which Stage 4.5.14 was explicitly told not to do.
    """
    seed(session, count=6, age_days=900)

    run(session)

    active, archived = counts(session)
    assert active == 6, "an audit event was removed from the active table"
    assert archived == 6


def test_a_platform_scoped_event_archives_with_no_hotel(session: Session) -> None:
    """``hotel_id IS NULL`` is carried through, never filled in to satisfy a constraint."""
    seed(
        session,
        count=2,
        age_days=900,
        hotel_id=None,
        action="auth.password_changed",
        resource_type="user",
        prefix="PLAT",
    )

    run(session)

    rows = archive_rows(session)
    assert len(rows) == 2
    assert all(row["hotel_id"] is None for row in rows)
    assert all(row["hotel_public_id"] is None for row in rows)
    assert {row["action"] for row in rows} == {"auth.password_changed"}


def test_a_hotel_scoped_event_archives_with_its_hotel(session: Session) -> None:
    hotel_id, hotel_public_id = make_hotel_row(session)
    seed(
        session,
        count=2,
        age_days=900,
        hotel_id=hotel_id,
        action="booking.created",
        resource_type="booking",
        prefix="HOT",
    )

    run(session)

    rows = archive_rows(session)
    assert len(rows) == 2
    assert {row["hotel_id"] for row in rows} == {hotel_id}
    assert {row["hotel_public_id"] for row in rows} == {hotel_public_id}


def test_both_scopes_archive_together_without_being_confused(session: Session) -> None:
    """One run, both kinds, and the line between them survives it."""
    hotel_id, hotel_public_id = make_hotel_row(session)
    seed(session, count=3, age_days=900, hotel_id=hotel_id, prefix="HOT")
    seed(session, count=2, age_days=900, hotel_id=None, prefix="PLAT")

    result = run(session)

    rows = archive_rows(session)
    assert result.archived == 5
    assert sum(1 for row in rows if row["hotel_id"] == hotel_id) == 3
    assert sum(1 for row in rows if row["hotel_id"] is None) == 2
    assert all(
        (row["hotel_public_id"] == hotel_public_id) == (row["hotel_id"] == hotel_id) for row in rows
    )


def test_the_actor_is_preserved_and_denormalised(session: Session) -> None:
    user_id, user_public_id = make_user_row(session)
    seed(session, count=1, age_days=900, actor_user_id=user_id)

    run(session)

    row = archive_rows(session)[0]
    assert row["actor_user_id"] == user_id
    assert row["actor_public_id"] == user_public_id


def test_an_unattributed_event_archives_with_no_actor(session: Session) -> None:
    """An OUTER join, so an event nobody caused is archived rather than silently dropped."""
    seed(session, count=1, age_days=900, actor_user_id=None)

    run(session)

    row = archive_rows(session)[0]
    assert row["actor_user_id"] is None
    assert row["actor_public_id"] is None


def test_an_empty_eligible_set_is_a_valid_result_not_an_error(session: Session) -> None:
    result = run(session)

    assert isinstance(result, ArchivalResult)
    assert (result.examined, result.archived, result.remaining) == (0, 0, 0)
    assert result.complete
    assert counts(session) == (0, 0)


def test_an_empty_table_is_a_valid_result(session: Session) -> None:
    result = run(session)

    assert result.archived == 0
    assert result.batches_run == 1


# ======================================================================================
# Batching
# ======================================================================================


def test_the_batch_size_bounds_each_statement(session: Session) -> None:
    seed(session, count=10, age_days=900)
    repository = AuditArchiveRepository(session)

    copied = repository.archive_batch(NOW - dt.timedelta(days=730), limit=3)
    session.commit()

    assert len(copied) == 3


def test_the_job_loops_until_the_backlog_is_cleared(session: Session) -> None:
    seed(session, count=10, age_days=900)

    result = run(session, audit_archive_batch_size=3)

    assert result.archived == 10
    # 3 + 3 + 3 + 1 productive batches, then a fifth that copies nothing and breaks the loop.
    # The terminator is counted because it really did run: `batches_run` reports work done,
    # not work that succeeded, and one bounded empty query is the price of a loop that stops
    # on the database's answer rather than on arithmetic this code did itself.
    assert result.batches_run == 5
    assert result.remaining == 0
    assert result.complete


def test_the_batch_ceiling_stops_a_run_and_reports_the_remainder(session: Session) -> None:
    """A stop condition that does not depend on the data, so a large backlog cannot hold a
    process indefinitely. The result says the work is unfinished rather than pretending."""
    seed(session, count=10, age_days=900)

    result = run(session, audit_archive_batch_size=2, audit_archive_max_batches=3)

    assert result.archived == 6
    assert result.batches_run == 3
    assert result.remaining == 4
    assert not result.complete


def test_a_second_run_finishes_what_the_ceiling_left(session: Session) -> None:
    seed(session, count=10, age_days=900)
    first = run(session, audit_archive_batch_size=2, audit_archive_max_batches=3)

    second = run(session, audit_archive_batch_size=2, audit_archive_max_batches=3)

    assert first.archived + second.archived == 10
    assert counts(session) == (10, 10)


def test_the_eligible_set_is_never_loaded_into_python(session: Session, engine: Engine) -> None:
    """A retention job that fetched its backlog into a list would fail exactly when it was
    most needed. The archival statement is one INSERT ... SELECT with a LIMIT.
    """
    seed(session, count=25, age_days=900)

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        if "audit_events" in statement:
            statements.append(" ".join(statement.split()))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        # One batch only, so this measures the COPY rather than the loop around it. With the
        # loop enabled there is always a second, empty INSERT -- the one that discovers there
        # is nothing left and breaks.
        result = run(session, audit_archive_batch_size=25, audit_archive_max_batches=1)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    assert result.archived == 25
    inserts = [s for s in statements if s.upper().startswith("INSERT")]
    assert len(inserts) == 1, inserts
    assert "SELECT" in inserts[0], "the copy is not an INSERT ... SELECT"
    assert "LIMIT" in inserts[0].upper(), "the copy is unbounded"
    # No statement selects whole audit rows back into the process.
    assert not any(s.upper().startswith("SELECT AUDIT_EVENTS.") for s in statements), statements


def test_archiving_is_a_bounded_number_of_statements(session: Session, engine: Engine) -> None:
    """No N+1: the per-batch cost does not grow with the batch's size."""
    seed(session, count=40, age_days=900)

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        if "audit_events" in statement:
            statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        run(session, audit_archive_batch_size=40)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    # 2 opening counts + (insert + verify) + (empty insert) + 1 closing count.
    assert len(statements) <= 7, statements


# ======================================================================================
# Idempotency
# ======================================================================================


def test_running_twice_archives_nothing_the_second_time(session: Session) -> None:
    seed(session, count=5, age_days=900)

    first = run(session)
    second = run(session)

    assert (first.archived, first.already_archived) == (5, 0)
    assert (second.archived, second.already_archived) == (0, 5)
    assert counts(session) == (5, 5)


def test_repeated_runs_do_not_duplicate_or_alter_archived_rows(session: Session) -> None:
    seed(session, count=5, age_days=900)
    run(session)
    before = archive_rows(session)

    for _ in range(3):
        run(session)

    after = archive_rows(session)
    assert after == before, "a repeat run changed the archive"


def test_the_database_refuses_a_duplicate_identity(session: Session) -> None:
    """The invariant is the primary key's, not Python's -- so it holds even against a caller
    that bypasses the repository entirely."""
    seed(session, count=1, age_days=900)
    run(session)
    row = archive_rows(session)[0]

    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
            sa.text(
                "INSERT INTO audit_events_archive "
                "(audit_event_id, public_id, action, resource_type, resource_reference, "
                " occurred_at) VALUES (:id, :pid, 'amenity.created', 'amenity', 'DUP', now())"
            ),
            {"id": row["audit_event_id"], "pid": str(uuid.uuid4())},
        )
    session.rollback()


def test_a_duplicate_public_id_is_refused_independently(session: Session) -> None:
    """A second guard on the same invariant, from the other direction."""
    seed(session, count=1, age_days=900)
    run(session)
    row = archive_rows(session)[0]

    with pytest.raises(sa.exc.IntegrityError):
        session.execute(
            sa.text(
                "INSERT INTO audit_events_archive "
                "(audit_event_id, public_id, action, resource_type, resource_reference, "
                " occurred_at) VALUES (999999, :pid, 'amenity.created', 'amenity', 'DUP', now())"
            ),
            {"pid": str(row["public_id"])},
        )
    session.rollback()


# ======================================================================================
# Immutability -- both tables, all four operations
# ======================================================================================


def test_updating_the_archive_is_refused(session: Session) -> None:
    seed(session, count=1, age_days=900)
    run(session)

    with pytest.raises(sa.exc.DatabaseError, match="append-only"):
        session.execute(sa.text("UPDATE audit_events_archive SET action = 'amenity.deleted'"))
    session.rollback()


def test_deleting_from_the_archive_is_refused(session: Session) -> None:
    seed(session, count=1, age_days=900)
    run(session)

    with pytest.raises(sa.exc.DatabaseError, match="append-only"):
        session.execute(sa.text("DELETE FROM audit_events_archive"))
    session.rollback()


def test_updating_the_source_is_still_refused(session: Session) -> None:
    """Stage 4.5.12's guarantee, re-asserted after this stage touched the audit domain."""
    seed(session, count=1, age_days=900)
    run(session)

    with pytest.raises(sa.exc.DatabaseError, match="append-only"):
        session.execute(sa.text("UPDATE audit_events SET action = 'amenity.deleted'"))
    session.rollback()


def test_deleting_from_the_source_is_still_refused(session: Session) -> None:
    """The reason nothing is physically removed. If this ever passes, retention has been
    implemented by weakening the audit trail."""
    seed(session, count=1, age_days=900)
    run(session)

    with pytest.raises(sa.exc.DatabaseError, match="append-only"):
        session.execute(sa.text("DELETE FROM audit_events"))
    session.rollback()


def test_the_archive_refusal_names_no_row(session: Session) -> None:
    seed(session, count=1, age_days=900, details='{"role": "manager"}')
    run(session)

    with pytest.raises(sa.exc.DatabaseError) as caught:
        session.execute(sa.text("DELETE FROM audit_events_archive"))
    session.rollback()

    assert "manager" not in str(caught.value)


# ======================================================================================
# Integrity -- the copy is faithful
# ======================================================================================


def test_the_archived_row_preserves_every_field(session: Session) -> None:
    hotel_id, hotel_public_id = make_hotel_row(session)
    user_id, user_public_id = make_user_row(session)
    seed(
        session,
        count=1,
        age_days=900,
        hotel_id=hotel_id,
        actor_user_id=user_id,
        action="booking.status_changed",
        resource_type="booking",
        prefix="BK",
        request_id="retention-correlation-1",
        details='{"old_status": "confirmed", "new_status": "cancelled"}',
    )
    source = session.execute(sa.text("SELECT * FROM audit_events")).mappings().one()

    run(session)

    row = archive_rows(session)[0]
    assert row["audit_event_id"] == source["id"]
    assert row["public_id"] == source["public_id"]
    assert row["hotel_id"] == source["hotel_id"]
    assert row["actor_user_id"] == source["actor_user_id"]
    assert row["action"] == source["action"]
    assert row["resource_type"] == source["resource_type"]
    assert row["resource_reference"] == source["resource_reference"]
    assert row["request_id"] == source["request_id"]
    assert row["details"] == source["details"]
    assert row["occurred_at"] == source["occurred_at"]
    assert row["hotel_public_id"] == hotel_public_id
    assert row["actor_public_id"] == user_public_id
    assert row["archived_at"] is not None


def test_verification_counts_a_faithful_copy(session: Session) -> None:
    seed(session, count=3, age_days=900)
    repository = AuditArchiveRepository(session)
    copied = repository.archive_batch(NOW - dt.timedelta(days=730), limit=10)

    assert repository.count_faithful_copies(copied) == 3
    session.commit()


def test_verification_rejects_a_batch_that_does_not_match(session: Session) -> None:
    """The guard that stops an unverified copy becoming the state of record.

    A stub repository returns ids it did not archive, standing in for any way a copy could be
    partial or wrong. The service must refuse to commit and must roll back.
    """
    seed(session, count=2, age_days=900)

    class _Unfaithful(AuditArchiveRepository):
        def archive_batch(self, cutoff: dt.datetime, *, limit: int) -> list[int]:
            super().archive_batch(cutoff, limit=limit)
            return [999_001, 999_002]

    service = AuditRetentionService(
        session, _Unfaithful(session), RetentionPolicy.from_settings(settings())
    )

    with pytest.raises(ArchiveVerificationError, match="do not match their source"):
        service.run(now=NOW)

    assert counts(session) == (2, 0), "an unverified batch was committed"


def test_the_verification_error_names_no_audit_event(session: Session) -> None:
    seed(session, count=1, age_days=900, details='{"role": "owner"}')

    class _Unfaithful(AuditArchiveRepository):
        def archive_batch(self, cutoff: dt.datetime, *, limit: int) -> list[int]:
            super().archive_batch(cutoff, limit=limit)
            return [999_001]

    service = AuditRetentionService(
        session, _Unfaithful(session), RetentionPolicy.from_settings(settings())
    )

    with pytest.raises(ArchiveVerificationError) as caught:
        service.run(now=NOW)

    message = str(caught.value)
    assert "999001" not in message
    assert "owner" not in message


def test_the_details_payload_survives_intact(session: Session) -> None:
    seed(session, count=1, age_days=900, details='{"amount": "150.00", "currency": "EUR"}')

    run(session)

    assert archive_rows(session)[0]["details"] == {"amount": "150.00", "currency": "EUR"}


def test_the_archive_introduces_no_sensitive_field(session: Session) -> None:
    """The archive stores no email, even though the read APIs join for one."""
    user_id, _ = make_user_row(session, email="retention-privacy@example.test")
    seed(session, count=1, age_days=900, actor_user_id=user_id)

    run(session)

    rendered = str(archive_rows(session))
    for banned in ("retention-privacy@example.test", "password", "argon2", "Bearer ", "secret"):
        assert banned not in rendered, f"{banned!r} reached the archive"


# ======================================================================================
# Concurrency -- real PostgreSQL, two connections
# ======================================================================================


def concurrent_runs(engine: Engine, *, count: int, batch_size: int) -> list[ArchivalResult]:
    """Two archival workers over the same eligible set, forced to interleave."""
    both_ready = threading.Barrier(2, timeout=30)
    results: dict[int, ArchivalResult] = {}
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def worker(index: int) -> None:
        with factory() as own:
            both_ready.wait()
            results[index] = archive_audit_events(
                own, settings(audit_archive_batch_size=batch_size), now=NOW
            )

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert len(results) == 2, "a worker did not finish"
    return [results[0], results[1]]


def test_two_concurrent_workers_produce_no_duplicate_archive_rows(
    session: Session, engine: Engine
) -> None:
    """The invariant this stage must hold under contention.

    Both workers select overlapping sets and both insert. ``ON CONFLICT DO NOTHING`` against a
    primary key that IS the event id means each row lands exactly once, whichever worker gets
    there first -- and because both insert in ``audit_events.id`` order, they queue rather than
    deadlock.
    """
    seed(session, count=40, age_days=900)

    concurrent_runs(engine, count=40, batch_size=5)

    active, archived = counts(session)
    assert archived == 40, "the archive does not hold exactly one row per eligible event"
    assert active == 40, "the active table lost a row"
    distinct = session.scalar(
        sa.text("SELECT count(DISTINCT audit_event_id) FROM audit_events_archive")
    )
    assert distinct == 40


def test_concurrent_workers_report_a_consistent_split(session: Session, engine: Engine) -> None:
    """The two runs together account for every event exactly once.

    Neither claims to have archived a row the other actually inserted: ``archived`` counts what
    ``RETURNING`` gave back, so a row lost to ``ON CONFLICT DO NOTHING`` is not counted by the
    worker that lost it.

    ``remaining`` is deliberately NOT asserted per worker. It is that worker's own observation
    at the moment it finished, and a worker whose batch was taken from under it stops early --
    by design, since the work is being done by somebody else -- and reports what it could still
    see. The finished state of the database is the shared conclusion, and that is what is
    checked.
    """
    seed(session, count=30, age_days=900)

    results = concurrent_runs(engine, count=30, batch_size=4)

    assert sum(result.archived for result in results) == 30
    assert counts(session) == (30, 30)
    assert AuditArchiveRepository(session).count_pending(NOW - dt.timedelta(days=730)) == 0


def test_concurrency_leaves_the_archive_identical_to_a_serial_run(
    session: Session, engine: Engine
) -> None:
    """Determinism: contention changes who did the work, never what the archive contains."""
    seed(session, count=20, age_days=900)
    concurrent_runs(engine, count=20, batch_size=3)
    concurrent = [
        {k: v for k, v in row.items() if k != "archived_at"} for row in archive_rows(session)
    ]

    session.execute(sa.text("TRUNCATE audit_events_archive"))
    session.commit()
    run(session)
    serial = [{k: v for k, v in row.items() if k != "archived_at"} for row in archive_rows(session)]

    assert concurrent == serial


def test_a_concurrent_worker_cannot_archive_a_retained_event(
    session: Session, engine: Engine
) -> None:
    """Contention must not widen the eligible set: both workers compute the same deterministic
    cutoff from the same *now*."""
    seed(session, count=10, age_days=900, prefix="OLD")
    seed(session, count=6, age_days=10, prefix="NEW")

    concurrent_runs(engine, count=10, batch_size=2)

    rows = archive_rows(session)
    assert len(rows) == 10
    assert all(str(row["resource_reference"]).startswith("OLD") for row in rows)


# ======================================================================================
# Security -- there is no way in from outside
# ======================================================================================


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/audit-events",
        "/api/v1/platform/audit-events",
        "/api/v1/platform/audit-events/archive",
        "/api/v1/audit/retention",
        "/api/v1/platform/retention",
    ],
)
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_destructive_retention_endpoint_exists(engine: Engine, path: str, method: str) -> None:
    """Archival is a job, not an API. Every mutating verb against every plausible retention
    URL is either not routed at all (404) or not allowed (405) -- never authorized."""
    from fastapi.testclient import TestClient

    from tests.integration.conftest import create_test_app

    client = TestClient(create_test_app(engine))

    response = getattr(client, method)(path)

    assert response.status_code in (404, 405), f"{method.upper()} {path} -> {response.status_code}"


def test_the_platform_audit_endpoint_still_offers_only_get(engine: Engine) -> None:
    """The existing read surface is unchanged by this stage."""
    from fastapi.testclient import TestClient

    from tests.integration.conftest import create_test_app

    app = create_test_app(engine)
    paths = TestClient(app).app.openapi()["paths"]  # type: ignore[attr-defined]

    assert set(paths["/api/v1/platform/audit-events"]) == {"get"}
    assert set(paths["/api/v1/hotels/{hotel_public_id}/audit-events"]) == {"get"}


def test_archival_does_not_change_what_the_read_apis_return(session: Session) -> None:
    """Stage 4.5.14 keeps archived events on the active read surface, deliberately: nothing is
    deleted, so the hotel and platform endpoints see exactly what they saw before."""
    seed(session, count=4, age_days=900)
    before = int(session.scalar(sa.text("SELECT count(*) FROM audit_events")) or 0)

    run(session)

    assert int(session.scalar(sa.text("SELECT count(*) FROM audit_events")) or 0) == before


# ======================================================================================
# Migration
# ======================================================================================


def test_the_migration_downgrades_and_upgrades_cleanly(engine: Engine) -> None:
    """0008 applied, reversed, and applied again, against the real database.

    Wrapped in try/finally: a failure part-way through must not leave the schema at 0007 and
    break every test after this one.
    """
    from alembic import command
    from alembic.config import Config

    from tests.integration.conftest import REPO_ROOT

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "database" / "migrations"))
    cfg.cmd_opts = None

    def archive_exists() -> bool:
        with engine.connect() as connection:
            return bool(
                connection.scalar(sa.text("SELECT to_regclass('public.audit_events_archive')"))
            )

    def function_exists() -> bool:
        with engine.connect() as connection:
            return bool(
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_proc "
                        "WHERE proname = 'audit_events_archive_append_only'"
                    )
                )
            )

    assert archive_exists()
    try:
        command.downgrade(cfg, "0007_audit_events")
        assert not archive_exists(), "downgrade left the archive table behind"
        assert not function_exists(), "downgrade left the trigger function behind"

        command.upgrade(cfg, "head")
        assert archive_exists()
        assert function_exists()
    finally:
        command.upgrade(cfg, "head")

    # And the schema is whole again: the trigger came back with the table.
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname = 'audit_events_archive' AND NOT t.tgisinternal"
                )
            )
        }
    assert triggers == {"trg_audit_events_archive_append_only"}


def test_the_source_table_survives_the_round_trip(session: Session) -> None:
    """0008's downgrade must not touch ``audit_events`` or its trigger."""
    triggers = {
        row[0]
        for row in session.execute(
            sa.text(
                "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'audit_events' AND NOT t.tgisinternal"
            )
        )
    }

    assert triggers == {"trg_audit_events_append_only"}
