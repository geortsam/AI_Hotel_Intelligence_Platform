"""Static guards on audit retention and archival.

Stage 4.5.14. The integration suite proves the archival job copies the right rows today. This
file proves the things a behavioural test cannot: that the retention policy has exactly one
definition, that no code path can delete an audit event, that no HTTP surface can invoke or
influence retention, and that the archive cannot quietly grow the ability to be rewritten.

The failure this guards against is specific. Retention is the one feature whose natural
implementation -- "delete what is old" -- would undo eleven stages of making the audit trail
evidence. A future change that added a ``DELETE`` here would look locally reasonable and would
be invisible from outside until somebody needed a record that was gone.

Docstrings are stripped before any source is matched. Both modules audited here document at
length what they deliberately do NOT do, and this project has been caught five times by prose
that reads as the thing it denies.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Table

import app
from app.core.config import Settings
from app.main import create_app
from app.models.audit import AuditEventArchive
from app.repositories.audit_archive import ARCHIVED_COLUMNS, AuditArchiveRepository
from app.services.retention import (
    ArchivalResult,
    AuditRetentionService,
    RetentionPolicy,
    archive_audit_events,
)

APP = Path(app.__file__).resolve().parent
MIGRATIONS = Path(app.__file__).resolve().parents[2] / "database" / "migrations" / "versions"

RETENTION_SERVICE = APP / "services" / "retention.py"
ARCHIVE_REPOSITORY = APP / "repositories" / "audit_archive.py"
MIGRATION = MIGRATIONS / "20260904_0008_audit_retention_archive.py"


def code_only(source: str) -> str:
    """*source* with docstrings removed, so prose cannot be mistaken for code."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def sources() -> dict[str, str]:
    return {
        path.relative_to(APP).as_posix(): code_only(path.read_text(encoding="utf-8"))
        for path in APP.rglob("*.py")
    }


# ======================================================================================
# Nothing deletes an audit event
# ======================================================================================


def test_no_module_deletes_an_audit_event() -> None:
    """The central guarantee of this stage, checked across the whole application.

    ``audit_events`` is append-only at the database level, so a DELETE would raise anyway --
    but it would raise at 3am in a scheduled job, not here. Matched on the ORM classes rather
    than on the word "delete", because ``delete_allocations`` is a legitimate booking method
    and a text sweep would either flag it or be loosened until it flagged nothing.
    """
    offenders = []
    for name, source in sources().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if called != "delete":
                continue
            rendered = ast.unparse(node)
            if "AuditEvent" in rendered:
                offenders.append(f"{name}: {rendered}")

    assert offenders == [], offenders


def test_the_archive_repository_has_no_delete_or_update_method() -> None:
    methods = {name for name in vars(AuditArchiveRepository) if not name.startswith("__")}

    for forbidden in ("delete", "update", "purge", "prune", "remove", "set_", "apply_changes"):
        assert not any(name.startswith(forbidden) for name in methods), sorted(methods)


def test_the_retention_service_never_deletes() -> None:
    source = code_only(RETENTION_SERVICE.read_text(encoding="utf-8"))

    for forbidden in ["delete", "DELETE", "TRUNCATE", "truncate", "drop", "DROP"]:
        assert forbidden not in source, f"the retention service contains {forbidden!r}"


def test_the_migration_does_not_touch_the_audit_table() -> None:
    """0008 is additive. It must not alter ``audit_events`` and, above all, must not touch the
    trigger that makes it append-only."""
    upgrade = MIGRATION.read_text(encoding="utf-8")
    upgrade = upgrade[upgrade.index("def upgrade") : upgrade.index("def downgrade")]

    assert "ALTER TABLE audit_events" not in upgrade
    assert "trg_audit_events_append_only" not in upgrade
    assert "DROP" not in upgrade
    assert "DISABLE TRIGGER" not in upgrade


def test_no_migration_ever_disables_a_trigger() -> None:
    for path in sorted(MIGRATIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "DISABLE TRIGGER" not in source, path.name
        assert "session_replication_role" not in source, path.name


# ======================================================================================
# The archive is immutable too
# ======================================================================================


def test_the_migration_installs_an_append_only_trigger_on_the_archive() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TRIGGER trg_audit_events_archive_append_only" in migration
    assert "BEFORE UPDATE OR DELETE ON audit_events_archive" in migration


def test_the_archive_is_reachable_by_no_endpoint() -> None:
    """No route mentions the archive, retention, or purging -- and the absence is the API."""
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    for banned in ("archive", "retention", "purge", "prune", "cleanup"):
        assert not any(banned in path for path in paths), banned


def test_no_router_imports_the_retention_service_or_the_archive() -> None:
    """A job entry point, not an endpoint. If a router could reach it, the next step would be
    a route that calls it -- and that route would be a destructive operation with a URL."""
    for name, source in sources().items():
        if not name.startswith("api/"):
            continue
        tree = ast.parse(source)
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "app.services.retention" not in imported, name
        assert "app.repositories.audit_archive" not in imported, name


def test_the_archive_model_declares_no_foreign_key() -> None:
    """Deliberate: an archive that cannot outlive the operational rows it describes is not
    evidence. The identity is preserved as plain values plus denormalised public ids."""
    assert cast(Table, AuditEventArchive.__table__).foreign_key_constraints == set()


def test_the_archive_preserves_the_original_identity() -> None:
    """The primary key IS the original event id -- which is what makes idempotency and
    concurrency safety the database's job rather than this code's."""
    primary_key = [column.name for column in AuditEventArchive.__table__.primary_key]

    assert primary_key == ["audit_event_id"]
    assert AuditEventArchive.__table__.columns["audit_event_id"].autoincrement is False


def test_the_archive_carries_every_column_of_the_source() -> None:
    """A faithful copy, not a summary. ``archived_at`` is the only addition, and the two
    denormalised public ids are the only derivations."""
    from app.models.audit import AuditEvent

    source = set(AuditEvent.__table__.columns.keys())
    archive = set(AuditEventArchive.__table__.columns.keys())

    assert source - archive == {"id"}, "the archive drops a column of the source"
    assert AuditEventArchive.__table__.columns["audit_event_id"].name == "audit_event_id"
    assert archive - source == {
        "audit_event_id",
        "hotel_public_id",
        "actor_public_id",
        "archived_at",
    }


def test_the_archive_stores_no_email() -> None:
    """The active table does not store one either -- the read APIs join for it. An archive
    nobody currently reads is the wrong place to start keeping personal data at rest."""
    columns = set(AuditEventArchive.__table__.columns.keys())

    for banned in ("actor_email", "email", "guest_name", "password", "token"):
        assert banned not in columns


def test_the_copied_columns_are_declared_once() -> None:
    """The SELECT and the INSERT read the same tuple, so they cannot drift into writing the
    right values into the wrong columns."""
    assert ARCHIVED_COLUMNS == (
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
    )
    assert set(ARCHIVED_COLUMNS) <= set(AuditEventArchive.__table__.columns.keys())


# ======================================================================================
# The policy has one definition
# ======================================================================================


def test_the_policy_is_built_from_settings_and_nowhere_else() -> None:
    """No module may decide retention locally. A second definition is a second answer to "is
    this event eligible?", and the first disagreement archives something that should not be."""
    offenders = [
        name
        for name, source in sources().items()
        if name != "core/config.py"
        and ("audit_retention_days" in source or "retention_days=" in source)
        and name != "services/retention.py"
    ]

    assert offenders == [], offenders


def test_the_default_retention_is_conservative() -> None:
    settings = Settings(environment="test")

    assert settings.audit_retention_days == 730
    assert settings.audit_archive_batch_size == 1000
    assert settings.audit_archive_max_batches == 100


def test_retention_cannot_be_configured_to_zero_days() -> None:
    """A misconfigured 0 would make every event written this second eligible immediately."""
    with pytest.raises(ValueError, match="audit_retention_days"):
        Settings(environment="test", audit_retention_days=0)


def test_the_batch_size_is_bounded_at_both_ends() -> None:
    with pytest.raises(ValueError, match="audit_archive_batch_size"):
        Settings(environment="test", audit_archive_batch_size=0)
    with pytest.raises(ValueError, match="audit_archive_batch_size"):
        Settings(environment="test", audit_archive_batch_size=10_001)


def test_the_policy_reads_every_number_from_settings() -> None:
    settings = Settings(
        environment="test",
        audit_retention_days=30,
        audit_archive_batch_size=7,
        audit_archive_max_batches=3,
    )

    policy = RetentionPolicy.from_settings(settings)

    assert policy == RetentionPolicy(retention_days=30, batch_size=7, max_batches=3)


def test_the_cutoff_is_deterministic() -> None:
    """Two workers must agree on the eligible set without coordinating, which they can only do
    if the same *now* always yields the same cutoff."""
    policy = RetentionPolicy(retention_days=90, batch_size=10, max_batches=5)
    now = dt.datetime(2027, 6, 1, 12, 0, 0, tzinfo=dt.UTC)

    assert policy.cutoff(now) == policy.cutoff(now)
    assert policy.cutoff(now) == dt.datetime(2027, 3, 3, 12, 0, 0, tzinfo=dt.UTC)


def test_a_naive_now_is_refused_rather_than_localised() -> None:
    """Every timestamp in this schema is TIMESTAMPTZ. Comparing a naive datetime against one
    is a silent wrong answer that depends on the server's zone."""
    policy = RetentionPolicy(retention_days=90, batch_size=10, max_batches=5)

    with pytest.raises(ValueError, match="timezone-aware"):
        policy.cutoff(dt.datetime(2027, 6, 1, 12, 0, 0))


def test_the_policy_is_frozen() -> None:
    """It is read by a running job; a mutable policy could change between the count and the
    copy."""
    policy = RetentionPolicy(retention_days=90, batch_size=10, max_batches=5)

    with pytest.raises((AttributeError, TypeError)):
        policy.retention_days = 1  # type: ignore[misc]


# ======================================================================================
# The result is safe to log
# ======================================================================================


def test_the_result_carries_only_counts_and_times() -> None:
    """Designed to be safe to log verbatim, and the way to keep it safe is to give it nowhere
    to put anything unsafe."""
    fields = set(ArchivalResult.__dataclass_fields__)

    assert fields == {
        "cutoff",
        "batch_size",
        "batches_run",
        "examined",
        "archived",
        "already_archived",
        "remaining",
        "duration_seconds",
    }


@pytest.mark.parametrize(
    "banned",
    [
        "details",
        "actor",
        "actor_user_id",
        "email",
        "request_id",
        "public_id",
        "hotel_id",
        "ids",
        "rows",
        "events",
        "sql",
        "statement",
    ],
)
def test_the_result_cannot_carry_anything_sensitive(banned: str) -> None:
    assert banned not in ArchivalResult.__dataclass_fields__


def test_the_service_logs_no_driver_exception() -> None:
    """``exc_info`` on a failed archival INSERT would write the audit rows it was copying into
    the log -- the payload this project has spent eleven stages keeping out."""
    assert "exc_info" not in code_only(RETENTION_SERVICE.read_text(encoding="utf-8"))


def test_the_verification_error_names_no_event() -> None:
    """An operator needs to know a batch failed, not which audit events were in it."""
    source = code_only(RETENTION_SERVICE.read_text(encoding="utf-8"))
    raising = source[source.index("def _verify") :]

    assert "archived_ids)" in raising  # the COUNT is reported
    assert "{archived_ids}" not in raising  # the ids are not


# ======================================================================================
# Layering
# ======================================================================================


def test_the_service_builds_no_query() -> None:
    source = code_only(RETENTION_SERVICE.read_text(encoding="utf-8"))

    for forbidden in ["select(", "insert(", "func.", "text(", "AuditEvent", "AuditEventArchive"]:
        assert forbidden not in source, f"the retention service contains {forbidden!r}"


def test_the_repository_never_commits() -> None:
    source = code_only(ARCHIVE_REPOSITORY.read_text(encoding="utf-8"))

    assert "commit" not in source
    assert "rollback" not in source


def test_the_service_owns_the_transaction() -> None:
    source = code_only(RETENTION_SERVICE.read_text(encoding="utf-8"))

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


def test_the_batch_is_bounded_by_the_policy() -> None:
    """The limit reaching the database is the configured one, not a constant written twice."""
    source = code_only(RETENTION_SERVICE.read_text(encoding="utf-8"))

    assert "limit=self._policy.batch_size" in source


def test_the_job_entry_point_is_a_function_not_a_route() -> None:
    signature = inspect.signature(archive_audit_events)

    assert set(signature.parameters) == {"session", "settings", "now"}
    assert not hasattr(archive_audit_events, "__fastapi_routes__")


def test_the_service_takes_no_user_and_no_hotel() -> None:
    """Archival is an internal system operation. It is not scoped to a tenant and cannot be
    pointed at one, so no hotel-scoped caller could archive another hotel's data through it."""
    parameters = set(inspect.signature(AuditRetentionService.__init__).parameters)

    assert parameters == {"self", "session", "repository", "policy"}
    for forbidden in ("user", "actor", "hotel", "hotel_id", "current_user"):
        assert forbidden not in parameters


def test_archival_emits_no_audit_event() -> None:
    """Documented in the module and asserted here.

    An ``audit.archived`` event would be written into ``audit_events``, would itself become
    eligible under this very policy, and would be archived by a later run -- a retention
    mechanism whose own bookkeeping is subject to retention. It would also need a system actor,
    which the request-context-derived actor model does not have.
    """
    source = code_only(RETENTION_SERVICE.read_text(encoding="utf-8"))

    for forbidden in ["AuditTrail", "AuditAction", "AuditResourceType", "record("]:
        assert forbidden not in source, f"the retention service contains {forbidden!r}"


def test_the_migration_chain_is_linear_and_ends_at_0008() -> None:
    import re

    chain: dict[str, str | None] = {}
    for path in sorted(MIGRATIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision: str = "([^"]+)"', source, re.MULTILINE)
        down = re.search(r'^down_revision: str \| None = (None|"[^"]+")', source, re.MULTILINE)
        assert revision and down, path.name
        chain[revision.group(1)] = None if down.group(1) == "None" else down.group(1).strip('"')

    assert len(chain) == 14
    heads = [rev for rev in chain if rev not in set(chain.values())]
    # Stage 4.5.15 added 0009 and Stage 6.8 added 0010. What this file is responsible for is
    # 0008's own position, which is unchanged; the head moves because the chain grew past it.
    # Stage 7.6 added 0012; Stage 7.9 added 0014.
    assert heads == ["0014_hotel_documents"]
    assert chain["0008_audit_retention_archive"] == "0007_audit_events"

    parents = [down for down in chain.values() if down is not None]
    assert len(parents) == len(set(parents)) == 13


def test_no_earlier_migration_mentions_the_archive() -> None:
    """0001-0007 are frozen. The archive appears in 0008 and nowhere else."""
    for path in sorted(MIGRATIONS.glob("*.py")):
        if path.name.startswith("20260904_0008"):
            continue
        assert "audit_events_archive" not in path.read_text(encoding="utf-8"), path.name


def test_the_migration_is_reversible() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    downgrade = migration[migration.index("def downgrade") :]

    assert "DROP TABLE IF EXISTS audit_events_archive" in downgrade
    assert "DROP FUNCTION IF EXISTS audit_events_archive_append_only()" in downgrade
