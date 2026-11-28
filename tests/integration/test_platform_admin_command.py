"""R1.3a: ``python -m app.jobs.platform_admin``, the audited operator grant, on PostgreSQL.

The command replaces the runbook's out-of-band INSERT into ``platform_admins``. These tests run it
as an operator does, in a fresh process with only the database URL, and check that each change
and its audit event commit together, that what it refuses changes nothing, that repeating is
harmless, that the events reach the platform audit API, and that its output names nobody. The
last section runs migration 0021's own upgrade and downgrade inside a rolled-back transaction.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.conftest import (
    TEST_DATABASE_URL,
    create_test_app,
    register_and_login,
    requires_postgres,
)

pytestmark = requires_postgres

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPOSITORY_ROOT
    / "database"
    / "migrations"
    / "versions"
    / "20261119_0021_audit_platform_grants.py"
)
OPS = "ops-grant@example.test"
SECOND = "ops-second@example.test"
OUTPUT = re.compile(r"^platform administration: (.+); (\d+) administrator\(s\)$")


@pytest.fixture(autouse=True)
def _clean(session: Session) -> Iterator[None]:
    """The suite's own teardown after every test -- users, grants and audit events truncated --
    so every account, grant and count a test sees is its own."""
    yield


@pytest.fixture
def registered(engine: Engine) -> TestClient:
    """Both accounts registered through the API, as the runbook requires before a grant."""
    client = TestClient(create_test_app(engine))
    for email in (OPS, SECOND):
        register_and_login(client, email)
    return client


def run(
    tmp_path: Path, *arguments: str, url: str | None = None
) -> subprocess.CompletedProcess[str]:
    """The command in a fresh process, from an empty directory so no ``.env`` is read."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("POSTGRES_", "DATABASE_URL"))
    }
    environment["DATABASE_URL"] = url or str(TEST_DATABASE_URL)
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "backend")
    return subprocess.run(
        [sys.executable, "-m", "app.jobs.platform_admin", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def succeeded(completed: subprocess.CompletedProcess[str]) -> tuple[str, int]:
    assert completed.returncode == 0, completed.stderr
    match = OUTPUT.match(completed.stdout.strip())
    assert match is not None, completed.stdout
    return match.group(1), int(match.group(2))


def administrators(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.scalars(
                sa.text(
                    "SELECT u.email FROM platform_admins pa JOIN users u ON u.id = pa.user_id "
                    "ORDER BY u.email"
                )
            )
        )


def events(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT e.action, e.resource_type, e.resource_reference, e.hotel_id, "
                "e.actor_user_id, e.details, e.request_id, u.public_id::text AS subject "
                "FROM audit_events e LEFT JOIN users u ON u.public_id::text = e.resource_reference "
                "ORDER BY e.id"
            )
        )
        return [dict(row._mapping) for row in rows]


def public_id(engine: Engine, email: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.scalar(
                sa.text("SELECT public_id::text FROM users WHERE email = :e"), {"e": email}
            )
        )


# --- grant ------------------------------------------------------------------------------------


def test_a_grant_and_its_audit_event_commit_together(
    engine: Engine, registered: TestClient, tmp_path: Path
) -> None:
    assert succeeded(run(tmp_path, "grant", OPS)) == ("platform administration granted", 1)

    assert administrators(engine) == [OPS]
    (event,) = events(engine)
    assert event["action"] == "platform_admin.granted"
    assert event["resource_type"] == "user"
    assert event["resource_reference"] == public_id(engine, OPS) == event["subject"]
    assert event["hotel_id"] is None and event["actor_user_id"] is None
    assert event["details"] == {"role": "platform_admin"}
    assert event["request_id"] is None


def test_a_repeated_grant_changes_nothing_and_records_nothing(
    engine: Engine, registered: TestClient, tmp_path: Path
) -> None:
    succeeded(run(tmp_path, "grant", OPS))

    assert succeeded(run(tmp_path, "grant", OPS)) == (
        "already a platform administrator; nothing changed",
        1,
    )
    assert administrators(engine) == [OPS]
    assert len(events(engine)) == 1


def test_the_email_is_matched_as_registration_stores_it(
    engine: Engine, registered: TestClient, tmp_path: Path
) -> None:
    succeeded(run(tmp_path, "grant", f"  {OPS.upper()} "))
    assert administrators(engine) == [OPS]


def test_an_unregistered_email_is_refused_and_nothing_is_created(
    engine: Engine, registered: TestClient, tmp_path: Path
) -> None:
    completed = run(tmp_path, "grant", "nobody-registered@example.test")

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "refused: no registered user has that email" in completed.stderr
    assert administrators(engine) == [] and events(engine) == []
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM users")) == 2


def test_a_deactivated_account_is_refused(
    engine: Engine, registered: TestClient, tmp_path: Path
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE users SET is_active = false WHERE email = :e"), {"e": OPS}
        )

    completed = run(tmp_path, "grant", OPS)

    assert completed.returncode == 1
    assert "refused: the account is deactivated" in completed.stderr
    assert administrators(engine) == [] and events(engine) == []


# --- revoke -----------------------------------------------------------------------------------


def test_a_revocation_and_its_audit_event_commit_together(
    engine: Engine, registered: TestClient, tmp_path: Path
) -> None:
    succeeded(run(tmp_path, "grant", OPS))
    succeeded(run(tmp_path, "grant", SECOND))

    assert succeeded(run(tmp_path, "revoke", OPS)) == ("platform administration revoked", 1)

    assert administrators(engine) == [SECOND]
    revoked = events(engine)[-1]
    assert revoked["action"] == "platform_admin.revoked"
    assert revoked["resource_type"] == "user"
    assert revoked["resource_reference"] == public_id(engine, OPS)
    assert revoked["hotel_id"] is None and revoked["actor_user_id"] is None
    assert revoked["details"] == {"role": "platform_admin"}
    with engine.connect() as connection:
        # Only the grant goes: the account itself is untouched.
        assert connection.scalar(
            sa.text("SELECT is_active FROM users WHERE email = :e"), {"e": OPS}
        )


def test_the_last_administrator_is_kept_unless_none_is_intended(
    engine: Engine, registered: TestClient, tmp_path: Path
) -> None:
    succeeded(run(tmp_path, "grant", OPS))

    refused = run(tmp_path, "revoke", OPS)
    assert refused.returncode == 1
    assert "refused: that is the last platform administrator" in refused.stderr
    assert administrators(engine) == [OPS] and len(events(engine)) == 1

    assert succeeded(run(tmp_path, "revoke", OPS, "--allow-none")) == (
        "platform administration revoked",
        0,
    )
    assert administrators(engine) == []
    assert [e["action"] for e in events(engine)] == [
        "platform_admin.granted",
        "platform_admin.revoked",
    ]


def test_revoking_from_a_non_administrator_changes_nothing(
    engine: Engine, registered: TestClient, tmp_path: Path
) -> None:
    succeeded(run(tmp_path, "grant", OPS))

    assert succeeded(run(tmp_path, "revoke", SECOND)) == (
        "not a platform administrator; nothing changed",
        1,
    )
    assert administrators(engine) == [OPS] and len(events(engine)) == 1


# --- what it makes visible, and what it never prints -----------------------------------------


def test_the_grant_is_live_and_its_event_is_in_the_platform_audit_api(
    engine: Engine, registered: TestClient, tmp_path: Path
) -> None:
    token = register_and_login(registered, OPS)
    headers = {"Authorization": f"Bearer {token}"}
    assert registered.get("/api/v1/platform/audit-events", headers=headers).status_code == 403

    succeeded(run(tmp_path, "grant", OPS))

    response = registered.get("/api/v1/platform/audit-events", headers=headers)
    assert response.status_code == 200, response.text
    (item,) = response.json()["items"]
    assert item["action"] == "platform_admin.granted"
    assert item["resource_type"] == "user"
    assert item["resource_reference"] == public_id(engine, OPS)
    assert item["actor_public_id"] is None


def test_the_output_names_nobody(engine: Engine, registered: TestClient, tmp_path: Path) -> None:
    outputs = [
        run(tmp_path, "grant", OPS),
        run(tmp_path, "grant", OPS),
        run(tmp_path, "grant", SECOND),
        run(tmp_path, "revoke", OPS),
        run(tmp_path, "revoke", SECOND),
        run(tmp_path, "grant", "nobody-registered@example.test"),
    ]
    text = "".join(c.stdout + c.stderr for c in outputs)
    for secret in (
        OPS,
        SECOND,
        "nobody-registered",
        public_id(engine, OPS),
        str(TEST_DATABASE_URL),
    ):
        assert secret not in text


def test_a_failure_changes_nothing_and_leaks_nothing(tmp_path: Path) -> None:
    unreachable = (
        "postgresql+psycopg://grant-user:PASSWORD-SENTINEL@127.0.0.1:1/nowhere?connect_timeout=3"
    )

    completed = run(tmp_path, "grant", OPS, url=unreachable)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "platform administration FAILED (OperationalError); nothing changed" in completed.stderr
    for secret in ("PASSWORD-SENTINEL", "grant-user", "nowhere", "Traceback", OPS):
        assert secret not in completed.stderr


@pytest.mark.parametrize(
    "arguments",
    [(), ("grant",), ("promote", OPS), ("grant", OPS, "--allow-none"), ("revoke",)],
)
def test_a_usage_error_is_refused_before_any_connection(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    unused = "postgresql+psycopg://nobody:unused@127.0.0.1:1/unused?connect_timeout=1"
    completed = run(tmp_path, *arguments, url=unused)
    assert completed.returncode == 2
    assert completed.stdout == ""


# --- migration 0021 itself --------------------------------------------------------------------


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0021", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def migrating(engine: Engine) -> Iterator[sa.Connection]:
    """A connection inside a transaction that is always rolled back, with Alembic operations
    bound to it -- so 0021's own functions run against the real catalog and leave nothing."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(sa.text("SET LOCAL lock_timeout = '10s'"))
            with Operations.context(MigrationContext.configure(connection)):
                yield connection
        finally:
            transaction.rollback()


def action_check(connection: sa.Connection) -> str:
    return str(
        connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_audit_events_action_valid'"
            )
        )
    )


def test_the_migration_is_the_one_on_disk() -> None:
    migration = load_migration()
    assert migration.revision == "0021_audit_platform_grants"
    assert migration.down_revision == "0020_guest_email_rules"


def test_the_head_permits_both_actions_and_the_downgrade_removes_only_them(
    migrating: sa.Connection,
) -> None:
    migration = load_migration()
    upgraded = action_check(migrating)
    assert "'platform_admin.granted'" in upgraded and "'platform_admin.revoked'" in upgraded

    migration.downgrade()
    downgraded = action_check(migrating)
    migration.upgrade()

    assert "platform_admin." not in downgraded
    for action in migration._PREVIOUS_ACTIONS:
        assert f"'{action}'" in downgraded
    assert action_check(migrating) == upgraded


def test_the_downgrade_is_refused_while_a_grant_event_exists(
    engine: Engine, registered: TestClient, tmp_path: Path, migrating: sa.Connection
) -> None:
    succeeded(run(tmp_path, "grant", OPS))

    with pytest.raises(IntegrityError):
        load_migration().downgrade()
