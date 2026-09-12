"""The safe way to get an isolated PostgreSQL database for the integration suite.

Every destructive step in this project's test workflow runs through
:mod:`tests.db_safety`. This script is the developer-facing front end to it, so that
creating, marking, checking and dropping a scratch database is one obvious command rather
than a remembered sequence of ``psql`` invocations -- the sequence being where mistakes
happen.

It exists because a database *name* cannot prove a database is disposable in this
repository: CI's ephemeral container and the seeded demo database are both called
``hotel_intelligence_test``. See ``tests/db_safety.py`` for the full reasoning.

Nothing here touches the demo database, and nothing here can be pointed at it by accident:
``drop`` refuses any target that fails both safety signals, exactly as the test fixtures do.

Usage (from the repository root, with the project's interpreter)::

    python scripts/testdb.py create  my_scratch_test     # create it and mark it disposable
    python scripts/testdb.py check   my_scratch_test     # report what the guard thinks
    python scripts/testdb.py drop    my_scratch_test     # refuse unless it is disposable
    python scripts/testdb.py url     my_scratch_test     # print the URL, password redacted

The server to connect to comes from ``TESTDB_ADMIN_URL`` if set, otherwise from
``TEST_DATABASE_URL``, otherwise from ``DATABASE_URL``. Only the host, port and credentials
are taken from it -- the database name always comes from the command line, so the value of
those variables can never decide what gets created or dropped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from tests.db_safety import (  # noqa: E402
    DISPOSABLE_MARKER,
    REQUIRED_DB_SUFFIX,
    UnsafeTestDatabaseError,
    assert_safe_destructive_target,
    assert_safe_test_database_url,
    read_contents,
    redact_url,
)


def _admin_url() -> str:
    """A URL whose host and credentials we borrow. Its database name is never used."""
    for variable in ("TESTDB_ADMIN_URL", "TEST_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(variable)
        if value:
            return value
    raise SystemExit(
        "No server to connect to. Set one of TESTDB_ADMIN_URL, TEST_DATABASE_URL or\n"
        "DATABASE_URL to a URL on the PostgreSQL server you want the scratch database on.\n"
        "Only its host, port and credentials are used -- the database name comes from the\n"
        "command line."
    )


def _urls(name: str) -> tuple[str, str]:
    """``(url for *name*, url for the maintenance database)`` on the configured server."""
    import sqlalchemy as sa

    base = sa.engine.url.make_url(_admin_url())
    target = base.set(database=name).render_as_string(hide_password=False)
    admin = base.set(database="postgres").render_as_string(hide_password=False)
    return target, admin


def _require_test_name(name: str) -> None:
    if not name.lower().endswith(REQUIRED_DB_SUFFIX):
        raise SystemExit(
            f"Refusing: {name!r} does not end with {REQUIRED_DB_SUFFIX!r}.\n"
            "This script only ever operates on databases whose name marks them as tests."
        )


def create(name: str) -> int:
    """Create *name* and mark it disposable, so the suite will accept it when populated."""
    import sqlalchemy as sa

    _require_test_name(name)
    target, admin = _urls(name)
    # The name rule, applied to what we are about to create, before we create it.
    assert_safe_test_database_url(target)

    engine = sa.create_engine(
        admin, future=True, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool
    )
    with engine.connect() as connection:
        exists = connection.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).scalar()
        if exists:
            print(f"{name!r} already exists -- leaving it alone. Use `drop` first to recreate.")
        else:
            # Identifiers cannot be bound as parameters. `name` has passed the suffix rule
            # and is quoted, so a crafted name cannot break out of the identifier.
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
            print(f"created {name!r}")

        connection.execute(sa.text(f"COMMENT ON DATABASE \"{name}\" IS '{DISPOSABLE_MARKER}'"))
        print(f"marked disposable: {DISPOSABLE_MARKER}")
    engine.dispose()

    print("\nRun the suite against it with:")
    print(f'  $env:TEST_DATABASE_URL = "{redact_url(target)}"   # with the real password')
    print("  python -m pytest tests/integration")
    return 0


def check(name: str) -> int:
    """Report what the guard makes of *name*, without changing anything."""
    target, _ = _urls(name)
    print(f"target : {redact_url(target)}")
    try:
        contents = read_contents(target)
    except UnsafeTestDatabaseError as exc:
        print(f"\n{exc}")
        return 1

    marked = contents.marker is not None and DISPOSABLE_MARKER in contents.marker
    print(f"marker : {'present' if marked else 'absent'}")
    print(f"rows   : {contents.row_counts or 'no application tables'}")
    try:
        assert_safe_destructive_target(target)
    except UnsafeTestDatabaseError as exc:
        print(f"\nVERDICT: UNSAFE\n\n{exc}")
        return 1
    print("\nVERDICT: safe for destructive integration tests")
    return 0


def drop(name: str) -> int:
    """Drop *name*, but only once both safety signals have cleared it."""
    import sqlalchemy as sa

    _require_test_name(name)
    target, admin = _urls(name)

    # BOTH signals, before the DROP is composed. A database holding data it was never
    # marked disposable for is not dropped by this script.
    try:
        assert_safe_destructive_target(target)
    except UnsafeTestDatabaseError as exc:
        print(f"Refusing to drop {name!r}.\n\n{exc}", file=sys.stderr)
        return 1

    engine = sa.create_engine(
        admin, future=True, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool
    )
    with engine.connect() as connection:
        connection.execute(
            sa.text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
        connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        remaining = connection.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).scalar()
    engine.dispose()

    if remaining:
        print(f"{name!r} still exists after DROP", file=sys.stderr)
        return 1
    print(f"dropped {name!r}; verified gone")
    return 0


def url(name: str) -> int:
    """Print the URL for *name* with the password redacted, for copying into docs."""
    target, _ = _urls(name)
    print(redact_url(target))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="testdb",
        description="Create, inspect and drop isolated PostgreSQL databases for the "
        "integration suite. Never operates on a database that has not cleared the "
        "safety guard in tests/db_safety.py.",
    )
    parser.add_argument("action", choices=("create", "check", "drop", "url"))
    parser.add_argument("name", help=f"database name; must end with {REQUIRED_DB_SUFFIX!r}")
    args = parser.parse_args(argv)

    return {"create": create, "check": check, "drop": drop, "url": url}[args.action](args.name)


if __name__ == "__main__":
    raise SystemExit(main())
