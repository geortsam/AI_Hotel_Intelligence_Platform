# Database

PostgreSQL is the production database. This directory holds everything that describes the
database *as a database*, kept separate from the application code that queries it.

## Layout

| Path | Purpose |
|---|---|
| `migrations/` | Alembic migration environment and revision scripts. |
| `init/` | SQL executed once, on first Postgres cluster creation (extensions, roles). Mounted read-only into the `db` service by Docker Compose. |

## Current state

**Empty by design.** Stage 1 creates no schema, no tables and no migrations. The ORM models
live in `backend/app/models/` when they are written; this directory holds the migration
history and any raw SQL that is not expressible through the ORM.

SQLite may be used later as a local test backend, which is why the models -- when they
arrive -- should prefer portable column types over PostgreSQL-only ones wherever the choice
is free.
