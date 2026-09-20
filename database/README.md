# Database

PostgreSQL is the production database. This directory holds everything that describes the
database *as a database*, kept separate from the application code that queries it.

## Layout

| Path | Purpose |
|---|---|
| `migrations/` | Alembic migration environment and revision scripts. |
| `init/` | SQL executed once, on first Postgres cluster creation (extensions, roles). Mounted read-only into the `db` service by Docker Compose. |

## Current state

**Ten migrations, head `0010_demand_predictions`,** applied by `alembic upgrade head`. They
build **22 tables** and the `historical_room_overlaps` view; with Alembic's own
`alembic_version`, a migrated database reports 24 entries in `information_schema.tables`. Every
one of the 22 is ORM-mapped -- `tests/backend/test_model_metadata.py` pins the list, so a table
that exists in one place and not the other fails the suite. The models live in
`backend/app/models/`; this directory
holds the migration history and the raw SQL that is not expressible through the ORM -- the
`btree_gist` extension, the room-overlap exclusion constraint, the constraint triggers and the
overlap-detection view.

The chain is linear: one root (`0001_initial_schema`, `down_revision = None`) and one head. CI
enforces both and pins the expected head **by name** rather than reading it back, so a chain that
grew a second head fails the build instead of agreeing with itself. A separate test pins a
canonical SHA-256 over the nine revision files with line endings normalised to LF, so the history
cannot be edited unnoticed on any platform:

```
0dc2f8b156e87d65827bd8a2d5802e53a3e91625ff3bd449b9d97e1ddc335895
```

Editing any shipped revision changes that digest and fails
`tests/backend/test_migration_integrity.py`. Schema changes are made by adding a revision, never
by editing one that has already run somewhere.

`init/` is currently empty. It is mounted read-only into the `db` service and would execute only
on first cluster creation; nothing is delegated to it today, because migration `0001` creates the
`btree_gist` extension itself.

**PostgreSQL 18.6** is the verified version: CI runs it, the development machine runs it, and
`docker-compose.yml` pins `postgres:18.6-alpine`. The schema uses PostgreSQL-specific features
deliberately -- identity columns, stored generated columns, `EXCLUDE USING gist`, JSONB -- so it
is not portable to SQLite and is not intended to be.
