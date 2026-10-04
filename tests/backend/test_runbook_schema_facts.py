"""The operator runbooks quote the schema the repository actually ships (M1).

``docs/deployment/backup-restore.md`` tells an operator to stop a backup unless the database is at
a named Alembic revision, and to judge a restore by that revision and a base-table count;
``docs/deployment/first-run-bootstrap.md`` names the revision a fresh deployment must report.
Those are literal values on purpose -- an operator compares them by eye, as CI's restore gate
does -- so every migration can leave them behind. They did: for six migrations the runbooks
named ``0011`` and 23 tables, and following them halted every valid backup.

This file ties the literals to the repository's own sources. The expected values are never
written here: the head is what Alembic resolves from the migration files (and must equal the
head the migration-integrity test pins), and the table count is the ORM's, which
``test_model_metadata`` proves equals the migrated schema. A migration that moves either fails
this suite until the runbooks move with it.

Positive controls run each extraction over text whose answer is known, so a pattern that matched
nothing -- and therefore asserted nothing -- cannot pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

import app.models  # noqa: F401 -- registers every table on the metadata
from app.db.base import Base
from tests.backend.test_migration_integrity import EXPECTED_HEAD

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKUP_RESTORE = REPOSITORY_ROOT / "docs" / "deployment" / "backup-restore.md"
FIRST_RUN = REPOSITORY_ROOT / "docs" / "deployment" / "first-run-bootstrap.md"

#: A revision identifier quoted on its own in backticks, e.g. `0017_llm_invocation_retention`.
#: A migration's file path (`database/.../20260901_0005_platform_administration.py:25`) does not
#: match: the backtick must be immediately followed by the four-digit revision number.
QUOTED_REVISION = re.compile(r"`(\d{4}_[a-z][a-z0-9_]*)`")

#: §7's base-table expectation: "Expect **N** — the M application tables plus `alembic_version`".
TABLE_EXPECTATION = re.compile(
    r"Expect \*\*(\d+)\*\* — the (\d+) application tables plus `alembic_version`"
)

#: A stated migration count, in digits or words -- deliberately absent since M1.
MIGRATION_COUNT = re.compile(
    r"\b(?:\d+|[a-z]+teen|[a-z]+ty|nine|ten|eleven|twelve) Alembic "
    r"migrations\b",
    re.IGNORECASE,
)


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def alembic_head() -> str:
    """The single head Alembic resolves from ``database/migrations`` -- no database involved."""
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "database" / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, heads
    return str(heads[0])


def application_tables() -> int:
    return len(Base.metadata.tables)


def quoted_revisions(text: str) -> list[str]:
    return QUOTED_REVISION.findall(text)


def table_expectations(text: str) -> list[tuple[int, int]]:
    return [(int(base), int(application)) for base, application in TABLE_EXPECTATION.findall(text)]


# ======================================================================================
# The sources the runbooks are checked against
# ======================================================================================


def test_the_head_alembic_resolves_is_the_head_the_integrity_test_pins() -> None:
    """Two authorities, one answer -- so the runbooks are checked against a single head."""
    assert alembic_head() == EXPECTED_HEAD


# ======================================================================================
# The runbooks
# ======================================================================================


@pytest.mark.parametrize(
    ("runbook", "occurrences"),
    [(BACKUP_RESTORE, 3), (FIRST_RUN, 1)],
    ids=["backup-restore", "first-run-bootstrap"],
)
def test_every_revision_a_runbook_quotes_is_the_current_head(
    runbook: Path, occurrences: int
) -> None:
    """§3, §4.1 and §7 of the backup runbook and §1 of the bootstrap one each name the revision a
    current deployment is at. Any other revision quoted there -- ``0011``, say -- is a stale
    instruction, and a stop condition that halts a valid backup."""
    revisions = quoted_revisions(text_of(runbook))

    assert len(revisions) == occurrences, revisions  # each instruction is still found
    assert set(revisions) == {alembic_head()}


def test_the_restore_check_expects_every_application_table_plus_alembic_version() -> None:
    [(base_tables, application)] = table_expectations(text_of(BACKUP_RESTORE))

    assert application == application_tables()
    assert base_tables == application_tables() + 1


def test_the_backup_runbook_states_no_migration_count() -> None:
    """A count of migrations is one more literal for the next migration to falsify; the revision
    check in §4.1 and §7 is what verifies the schema."""
    assert MIGRATION_COUNT.search(text_of(BACKUP_RESTORE)) is None


# ======================================================================================
# Positive controls: each extraction finds what it should, and a stale value cannot pass
# ======================================================================================


def test_the_revision_extraction_finds_a_stale_revision() -> None:
    stale = (
        "If the revision is not `0011_demand_prediction_public_id`, stop. "
        "See [`database/migrations/versions/20260901_0005_platform_administration.py:25`]."
    )
    assert quoted_revisions(stale) == ["0011_demand_prediction_public_id"]
    assert set(quoted_revisions(stale)) != {alembic_head()}


def test_the_table_extraction_finds_a_stale_count() -> None:
    stale = "Expect **23** — the 22 application tables plus `alembic_version`. The view"
    assert table_expectations(stale) == [(23, 22)]
    assert table_expectations(stale) != [(application_tables() + 1, application_tables())]


@pytest.mark.parametrize(
    "stated",
    [
        "the schema is nine Alembic migrations",
        "17 Alembic migrations",
        "seventeen Alembic migrations",
    ],
)
def test_the_migration_count_pattern_recognises_a_stated_count(stated: str) -> None:
    assert MIGRATION_COUNT.search(stated) is not None
