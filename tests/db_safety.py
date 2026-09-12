"""The single authoritative guard between a mistyped ``TEST_DATABASE_URL`` and lost data.

The PostgreSQL integration suite is destructive by design: it runs ``alembic downgrade base``
(DROP TABLE on every table) once per session and ``TRUNCATE ... RESTART IDENTITY CASCADE``
after every test. ``TEST_DATABASE_URL`` is supplied by hand, so a single mistake decides
whether that lands on a throwaway database or on one somebody needs.

This module is the one place that decides. It is imported by
``tests/integration/conftest.py`` (which re-exports the names it has always exported, so
existing imports keep working) and is exercised directly by the guard tests, which open no
database connection for the string half of it.

## Two independent signals, both required

Stage 5.17 exists because **the name rule alone is not sufficient, and this repository
proves it**: CI provisions an ephemeral container database called ``hotel_intelligence_test``,
and the local demo database -- the one holding the seeded hotels, bookings and ledger -- is
*also* called ``hotel_intelligence_test``. One is disposable and one is not. No amount of
reading the name can tell them apart, and an earlier run of the full suite pointed
``alembic downgrade base`` at the populated one. It was refused only by an unrelated
migration defect, which is luck rather than safety.

So a target must pass **both** of these before anything destructive runs:

1. :func:`assert_safe_test_database_url` -- pure string analysis of the URL. Parseable,
   PostgreSQL, names exactly one database, that name ends with ``_test``, and the host does
   not look like real infrastructure. Unchanged from the guard that has always been here.
2. :func:`assert_disposable_database` -- **connects and looks**. A database that already
   holds application data is refused unless it has been deliberately marked disposable. This
   is the signal that separates CI's empty container from the populated demo database,
   because it asks the only question that actually matters: *would destroying this lose
   anything?*

Signal 2 needs a connection, but the connection it makes is read-only -- three ``SELECT``
statements. Nothing destructive has run at the point it is consulted.

## Marking a database disposable

A scratch database that is reused across runs will legitimately hold rows from the previous
run. Rather than guess, the owner says so once, in the database itself::

    COMMENT ON DATABASE my_scratch_test IS 'ahip-disposable-test-database';

A database comment survives ``downgrade base`` -- that drops tables, not the database -- so
the mark is made once and holds. It is deliberately awkward to set by accident, it is
visible to anyone inspecting the server, and it is trivially removable. It is never applied
automatically by this module: a guard that can clear its own alarm is not a guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit


class UnsafeTestDatabaseError(RuntimeError):
    """``TEST_DATABASE_URL`` does not clearly identify a throwaway test database."""


#: Mandatory rule: the database name must end with this.
REQUIRED_DB_SUFFIX = "_test"

#: Secondary heuristic on the host. Deliberately small -- a long blocklist gives false
#: confidence without adding much.
DANGEROUS_HOST_TOKENS = ("prod", "production", "live")

#: The exact database comment that marks a database as disposable. Checked literally, as a
#: substring of the comment, so a developer may add context around it.
DISPOSABLE_MARKER = "ahip-disposable-test-database"

#: Tables consulted to decide whether a database holds anything worth keeping. Chosen
#: because every one of them is application data a person would miss, and because between
#: them they cover a database seeded by any route -- the demo seeder, a manual session, or a
#: previous suite run that did not finish its teardown.
SENTINEL_TABLES = ("hotels", "users", "bookings", "guests", "revenue")

#: How the marker is set, quoted in every refusal so the fix is in front of the reader.
MARKER_SQL_HINT = "COMMENT ON DATABASE <database> IS '{marker}';"

EXAMPLE_URL = "postgresql+psycopg://user:PASSWORD@localhost:5432/hotel_test"


def redact_url(url: str) -> str:
    """Return *url* with the password replaced by ``***``.

    Every message this module emits passes through here. A connection string reaches error
    output, logs and CI transcripts, and none of those should ever carry the password.
    """
    # urlsplit() does not validate on construction: `.port` and `.password` parse lazily and
    # raise ValueError on ACCESS. Every one of them must therefore be read inside the try --
    # this function is called from error paths, so it must never raise there itself.
    try:
        parts = urlsplit(url)
        password = parts.password
        host = parts.hostname or ""
        port = parts.port
        username = parts.username or ""
    except ValueError:
        return "<unparseable URL>"

    if not password:
        return url

    netloc = f"{username}:***@{host}:{port}" if port else f"{username}:***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def database_name_from_url(url: str) -> str:
    """Extract the database name, raising :class:`UnsafeTestDatabaseError` if it cannot be.

    A URL we cannot parse is treated as unsafe rather than given the benefit of the doubt:
    if we do not know what we are about to drop, we do not drop it.
    """
    if not url or not url.strip():
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is empty.\n"
            f"  required : a PostgreSQL URL whose database name ends with {REQUIRED_DB_SUFFIX!r}\n"
            f"  example  : {EXAMPLE_URL}"
        )

    try:
        parts = urlsplit(url)
        _ = parts.port  # invalid ports only raise on access
    except ValueError as exc:
        raise UnsafeTestDatabaseError(
            f"TEST_DATABASE_URL could not be parsed ({exc}).\n"
            f"  target   : {redact_url(url)}\n"
            f"  example  : {EXAMPLE_URL}"
        ) from exc

    # A bare `localhost:5432/db` parses with "localhost" AS THE SCHEME, so checking merely
    # that a scheme exists would wave it through. Requiring a postgres driver prefix rejects
    # that, and also stops a sqlite:// URL being handed to this suite by accident.
    if not parts.scheme.lower().startswith("postgres"):
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is not a PostgreSQL URL.\n"
            f"  detected scheme : {parts.scheme!r}\n"
            f"  target          : {redact_url(url)}\n"
            "  required        : a postgresql:// or postgresql+psycopg:// URL\n"
            f"  example         : {EXAMPLE_URL}"
        )

    name = parts.path.lstrip("/")
    if not name or "/" in name:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL names no database (or names more than one).\n"
            f"  target   : {redact_url(url)}\n"
            f"  required : a database name ending with {REQUIRED_DB_SUFFIX!r}\n"
            f"  example  : {EXAMPLE_URL}"
        )
    return name


def assert_safe_test_database_url(url: str) -> str:
    """Signal 1. Return the database name, or refuse on the URL alone.

    Pure string analysis -- it opens nothing. Called before any Alembic downgrade/upgrade and
    before any TRUNCATE, and always before :func:`assert_disposable_database`, so an
    obviously wrong target is rejected without a connection being attempted at all.
    """
    name = database_name_from_url(url)

    if not name.lower().endswith(REQUIRED_DB_SUFFIX):
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is UNSAFE -- the integration suite refuses to run.\n"
            f"  detected database : {name!r}\n"
            f"  target            : {redact_url(url)}\n"
            f"  required          : the database name must end with {REQUIRED_DB_SUFFIX!r}\n"
            f"  example           : {EXAMPLE_URL}\n"
            "\n"
            "This suite runs `alembic downgrade base` (DROP TABLE on every table) and\n"
            "TRUNCATE ... RESTART IDENTITY CASCADE after every test. Pointing it at a\n"
            "database that is not a throwaway would destroy it."
        )

    host = (urlsplit(url).hostname or "").lower()
    dangerous = [token for token in DANGEROUS_HOST_TOKENS if token in host]
    if dangerous:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is UNSAFE -- the host looks like real infrastructure.\n"
            f"  detected database : {name!r}\n"
            f"  detected host     : {host!r} (contains {dangerous[0]!r})\n"
            f"  target            : {redact_url(url)}\n"
            "  required          : run this suite against a local or disposable server\n"
            f"  example           : {EXAMPLE_URL}\n"
            "\n"
            "The database name passed the naming rule, but the host did not. If this host\n"
            "really is disposable, rename it or adjust DANGEROUS_HOST_TOKENS deliberately."
        )

    return name


# ======================================================================================
# Signal 2: is this database actually disposable?
# ======================================================================================


@dataclass(frozen=True)
class DatabaseContents:
    """What a read-only look at the target found.

    ``row_counts`` holds only the sentinel tables that exist; a table that is absent is not
    listed, which is how a freshly created database is told apart from an empty schema.
    """

    marker: str | None
    row_counts: dict[str, int]

    @property
    def populated(self) -> dict[str, int]:
        return {table: count for table, count in self.row_counts.items() if count > 0}


class ContentsReader(Protocol):
    """Reads the facts signal 2 needs. Injectable so the decision can be tested without a
    database, and so nothing in the decision path can accidentally issue a write."""

    def __call__(self, url: str) -> DatabaseContents: ...


def evaluate_disposability(name: str, url: str, contents: DatabaseContents) -> None:
    """The decision, as a pure function. Raises :class:`UnsafeTestDatabaseError` to refuse.

    Two ways to be disposable, and no third:

    * **nothing to lose** -- none of the sentinel tables holds a row. A database that has
      just been created, and one the suite itself has finished truncating, both look like
      this. This is the ordinary CI and scratch-database case.
    * **explicitly marked** -- the database comment carries :data:`DISPOSABLE_MARKER`. This
      is how a reusable local scratch database with leftover rows is allowed through, by a
      person who has decided it is expendable.

    Anything else is refused. The refusal is deliberately loud about *what* was found,
    because "it has 166 bookings in it" is the fact that changes a developer's mind.
    """
    if contents.marker is not None and DISPOSABLE_MARKER in contents.marker:
        return

    populated = contents.populated
    if not populated:
        return

    found = ", ".join(f"{table}={count}" for table, count in sorted(populated.items()))
    raise UnsafeTestDatabaseError(
        "TEST_DATABASE_URL is UNSAFE -- the target already holds application data.\n"
        f"  detected database : {name!r}\n"
        f"  target            : {redact_url(url)}\n"
        f"  found             : {found}\n"
        "  refused           : `alembic downgrade base` and TRUNCATE were NOT executed\n"
        "\n"
        "The name ends with "
        f"{REQUIRED_DB_SUFFIX!r}, but a name cannot prove a database is disposable --\n"
        "this project's CI container and its seeded demo database share one. So the\n"
        "contents were checked, and this database has rows in it.\n"
        "\n"
        "If this is genuinely a throwaway database, say so once, in the database:\n"
        f"  {MARKER_SQL_HINT.format(marker=DISPOSABLE_MARKER)}\n"
        "\n"
        "Otherwise point TEST_DATABASE_URL at a database you are willing to lose:\n"
        f"  {EXAMPLE_URL}"
    )


def read_contents(url: str) -> DatabaseContents:
    """Open a read-only connection and gather the facts. Three SELECTs, no writes.

    A failure to connect is *not* treated as safe: it is re-raised as a refusal, because a
    target we cannot inspect is a target we cannot vouch for.
    """
    import sqlalchemy as sa

    try:
        engine = sa.create_engine(url, future=True, poolclass=sa.pool.NullPool)
        with engine.connect() as connection:
            marker = connection.execute(
                sa.text(
                    "SELECT shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname = current_database()"
                )
            ).scalar()

            present = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                    ),
                    {"names": list(SENTINEL_TABLES)},
                )
            }

            counts: dict[str, int] = {}
            for table in SENTINEL_TABLES:
                if table in present:
                    # The identifier comes from SENTINEL_TABLES, a module constant, and is
                    # additionally confined to what information_schema just confirmed
                    # exists. No caller-supplied string reaches this statement.
                    counts[table] = int(
                        connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() or 0
                    )
        engine.dispose()
    except UnsafeTestDatabaseError:
        raise
    except Exception as exc:  # any failure to inspect must fail closed
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL could not be inspected, so it cannot be judged safe.\n"
            f"  target   : {redact_url(url)}\n"
            f"  error    : {type(exc).__name__}\n"
            "  refused  : nothing destructive was executed\n"
            "\n"
            "A database this guard cannot read is one it will not let the suite drop.\n"
            "Check that the server is running and the URL is correct."
        ) from exc

    return DatabaseContents(marker=marker, row_counts=counts)


def assert_disposable_database(url: str, *, reader: ContentsReader | None = None) -> None:
    """Signal 2. Refuse a database that holds data and has not been marked disposable.

    Must be called **after** :func:`assert_safe_test_database_url` and **before** the first
    destructive statement.
    """
    name = database_name_from_url(url)
    evaluate_disposability(name, url, (reader or read_contents)(url))


def assert_safe_destructive_target(url: str, *, reader: ContentsReader | None = None) -> str:
    """Both signals, in order. The one call a destructive fixture should make.

    Returns the database name so a caller can log or assert on it.
    """
    name = assert_safe_test_database_url(url)
    assert_disposable_database(url, reader=reader)
    return name


__all__ = [
    "DANGEROUS_HOST_TOKENS",
    "DISPOSABLE_MARKER",
    "EXAMPLE_URL",
    "MARKER_SQL_HINT",
    "REQUIRED_DB_SUFFIX",
    "SENTINEL_TABLES",
    "ContentsReader",
    "DatabaseContents",
    "UnsafeTestDatabaseError",
    "assert_disposable_database",
    "assert_safe_destructive_target",
    "assert_safe_test_database_url",
    "database_name_from_url",
    "evaluate_disposability",
    "read_contents",
    "redact_url",
]
