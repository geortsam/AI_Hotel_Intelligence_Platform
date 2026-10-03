"""Static guards on the `llm_invocations` retention contract (Issue 4).

`tests/integration/test_llm_invocation_purge.py` proves the purge deletes the right records over
real PostgreSQL. This file proves what a behavioural test cannot: the setting's bounds, that one
module declares the retention period to the database and one module applies it, that no HTTP
surface reaches the purge, that the cutoff is counted in hours, and that migration 0017 replaced a
function body and nothing else.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

import app
from app.core.config import Settings
from app.repositories.llm_invocation import RETENTION_DECLARATION, _expired
from tests.backend.test_retention_layering import sources

MIGRATIONS = Path(app.__file__).resolve().parents[2] / "database" / "migrations" / "versions"
MIGRATION = MIGRATIONS / "20261003_0017_llm_invocation_retention.py"
ORIGINAL = MIGRATIONS / "20260925_0013_llm_invocations.py"


# ======================================================================================
# The setting
# ======================================================================================


def test_the_default_retention_is_the_approved_365_days() -> None:
    assert Settings(environment="test").llm_invocation_retention_days == 365


def test_the_retention_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_INVOCATION_RETENTION_DAYS", "400")
    assert Settings(environment="test").llm_invocation_retention_days == 400


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_the_retention_is_bounded_at_both_ends(days: int) -> None:
    with pytest.raises(ValidationError, match="llm_invocation_retention_days"):
        Settings(environment="test", llm_invocation_retention_days=days)


def test_the_retention_bounds_themselves_are_accepted() -> None:
    assert Settings(environment="test", llm_invocation_retention_days=3650)
    one_day = Settings(
        environment="test", llm_invocation_retention_days=1, copilot_conversation_retention_days=1
    )
    assert one_day.llm_invocation_retention_days == 1


def test_the_retention_may_not_be_shorter_than_the_conversation_retention() -> None:
    """A live conversation turn names its accounting record by `request_id`."""
    with pytest.raises(ValidationError, match="LLM_INVOCATION_RETENTION_DAYS"):
        Settings(
            environment="test",
            llm_invocation_retention_days=29,
            copilot_conversation_retention_days=30,
        )
    equal = Settings(
        environment="test",
        llm_invocation_retention_days=90,
        copilot_conversation_retention_days=90,
    )
    assert equal.llm_invocation_retention_days == 90


def test_every_allowed_conversation_retention_fits_under_the_default() -> None:
    """The conversation retention's maximum is 365: the default can never be refused."""
    assert (
        Settings(
            environment="test", copilot_conversation_retention_days=365
        ).llm_invocation_retention_days
        == 365
    )


# ======================================================================================
# One declaration, one purge, no HTTP surface
# ======================================================================================


def test_only_the_repository_declares_a_retention_period_to_the_database() -> None:
    declaring = sorted(
        name
        for name, source in sources().items()
        if RETENTION_DECLARATION in source or "set_config" in source
    )
    assert declaring == ["repositories/llm_invocation.py"]
    assert f"'{RETENTION_DECLARATION}'" in MIGRATION.read_text(encoding="utf-8")


def test_only_the_retention_service_runs_the_purge() -> None:
    callers = sorted(
        name
        for name, source in sources().items()
        if ("purge_expired(" in source or "hotels_with_expired(" in source)
        and "LlmInvocationRepository" in source
        and name != "repositories/llm_invocation.py"
    )
    assert callers == ["services/llm_invocation_retention.py"]


def test_no_route_or_request_dependency_reaches_the_purge() -> None:
    importers = sorted(
        name
        for name, source in sources().items()
        if "app.services.llm_invocation_retention" in source
        or "purge_expired_invocations" in source
    )
    assert importers == ["jobs/purge_llm_invocations.py", "services/llm_invocation_retention.py"]


def test_a_retention_day_is_counted_as_24_hours_by_the_database() -> None:
    """An interval of days is calendar arithmetic in the session's time zone; hours are not."""
    compiled = str(
        _expired(365).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert re.search(r"created_at <= now\(\) - make_interval\(0, 0, 0, 0, 8760\)", compiled), (
        compiled
    )


# ======================================================================================
# Migration 0017
# ======================================================================================


def _section(source: str, start: str, end: str | None = None) -> str:
    return source[source.index(start) : source.index(end) if end else None]


def test_the_migration_replaces_one_function_body_and_touches_nothing_else() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    upgrade = _section(migration, "def upgrade", "def downgrade")
    downgrade = _section(migration, "def downgrade")
    for half in (upgrade, downgrade):
        assert half.count("CREATE OR REPLACE FUNCTION llm_invocations_append_only()") == 1
        for forbidden in ("CREATE TABLE", "ALTER TABLE", "DROP", "TRIGGER trg_", "INDEX"):
            assert forbidden not in half, forbidden
        assert "audit_events" not in half


def test_the_downgrade_restores_migration_0013s_function_exactly() -> None:
    def body(source: str) -> str:
        start = source.index("RETURNS trigger AS $$")
        return " ".join(source[start : source.index("$$ LANGUAGE plpgsql", start)].split())

    original = _section(ORIGINAL.read_text(encoding="utf-8"), "CREATE FUNCTION")
    downgrade = _section(MIGRATION.read_text(encoding="utf-8"), "def downgrade")
    assert body(downgrade) == body(original)


def test_the_upgrade_still_refuses_every_update_and_floors_the_period_at_a_day() -> None:
    upgrade = _section(MIGRATION.read_text(encoding="utf-8"), "def upgrade", "def downgrade")
    # The one path that returns a row is inside the DELETE branch; everything else raises.
    assert upgrade.count("RETURN OLD") == 1
    assert upgrade.index("IF TG_OP = 'DELETE' THEN") < upgrade.index("RETURN OLD")
    assert "retention_hours < 24" in upgrade
    assert "OLD.created_at <= now() - make_interval(hours => retention_hours)" in upgrade
    assert "current_setting('app.llm_invocation_retention_hours', true)" in upgrade
