# Database

PostgreSQL is the production database. This directory holds everything that describes the
database *as a database*, kept separate from the application code that queries it.

## Layout

| Path | Purpose |
|---|---|
| `migrations/` | Alembic migration environment and revision scripts. |
| `init/` | SQL executed once, on first Postgres cluster creation (extensions, roles). Mounted read-only into the `db` service by Docker Compose. |

## Current state

**Seventeen migrations, head `0017_llm_invocation_retention`,** applied by `alembic upgrade
head`. They build **28 tables** and the `historical_room_overlaps` view; with Alembic's own
`alembic_version`, a migrated database reports 30 entries in `information_schema.tables`. Every
one of the 28 is ORM-mapped -- `tests/backend/test_model_metadata.py` pins the list, so a table
that exists in one place and not the other fails the suite. The models live in
`backend/app/models/`; this directory
holds the migration history and the raw SQL that is not expressible through the ORM -- the
`btree_gist` extension, the room-overlap exclusion constraint, the constraint triggers and the
overlap-detection view.

The chain is linear: one root (`0001_initial_schema`, `down_revision = None`) and one head. CI
enforces both and pins the expected head **by name** rather than reading it back, so a chain that
grew a second head fails the build instead of agreeing with itself. A separate test pins a
canonical SHA-256 over every revision file with line endings normalised to LF, so the history
cannot be edited unnoticed on any platform:

```
690a0cf9ced1f9e3c4b7a26980789449069b172b217c5a36781a59ccba11ea01
```

Editing any shipped revision changes that digest and fails
`tests/backend/test_migration_integrity.py`. Schema changes are made by adding a revision, never
by editing one that has already run somewhere. `0017_llm_invocation_retention` is an example:
it creates no table and replaces the body of the trigger function `0013` created, so that an
`llm_invocations` record past its retention period can be purged -- see
[../docs/copilot-accounting-retention.md](../docs/copilot-accounting-retention.md).

`init/` is currently empty. It is mounted read-only into the `db` service and would execute only
on first cluster creation; nothing is delegated to it today, because migration `0001` creates the
`btree_gist` extension itself.

**PostgreSQL 18.6** is the verified version: CI runs it, the development machine runs it, and
`docker-compose.yml` pins `postgres:18.6-alpine`. The schema uses PostgreSQL-specific features
deliberately -- identity columns, stored generated columns, `EXCLUDE USING gist`, JSONB -- so it
is not portable to SQLite and is not intended to be.

## Demo data

`scripts/seed_demo.py` fills an **empty, fully migrated** database with synthetic data for two
fictional hotels: rooms and room types, guests, a year of past stays, the stays in house on the
reference date, four months of future bookings, captured payments, reviews, and a non-room
revenue and expense ledger. It also creates one demo owner account with an `owner` membership
at both hotels. The data exists to be looked at. It is **not evidence** of anything: never
train, evaluate or measure on it.

### Create a demo database

Use a name **without** the `_test` suffix, for example `hotel_intelligence_demo`. That keeps it
outside the integration suite's reach, because the suite refuses any target whose name does not
end in `_test`. It also keeps it apart from the older demo database `hotel_intelligence_test`,
whose name collides with CI's.

```bash
psql -U postgres -c "CREATE DATABASE hotel_intelligence_demo"
```

```bash
.venv/Scripts/python.exe -m alembic -x url=postgresql+psycopg://postgres:<password>@localhost:5432/hotel_intelligence_demo upgrade head
```

Set the demo owner's password in the environment (12 to 256 characters; it is never printed),
then seed:

```bash
export DEMO_OWNER_PASSWORD='<choose one>'
```

```bash
.venv/Scripts/python.exe scripts/seed_demo.py --database-url postgresql+psycopg://postgres:<password>@localhost:5432/hotel_intelligence_demo --reference-date today
```

Point the API at it with `DATABASE_URL` and sign in as `demo.owner@example.com`.

| Option | Default | Meaning |
|---|---|---|
| `--reference-date` | *required* | The "today" the data describes, as `YYYY-MM-DD`, or `today` for the machine's date. The data is as of 12:00 hotel-local on that date. |
| `--seed` | `7151` | The random seed. Same seed + same reference date + same options = the same data. |
| `--history-days` | `365` | Days of stays before the reference date (28 to 1000). |
| `--future-days` | `120` | Days of stays after it (0 to 365). Only bookings already taken by the reference moment exist. |
| `--owner-email` | `demo.owner@example.com` | The demo owner's login. The password always comes from `DEMO_OWNER_PASSWORD`. |
| `--dry-run` | — | Build the dataset in memory and print its summary and fingerprint. Touches no database. |
| `--database-url` | — | The target. Required unless `--dry-run`. **Never** read from `DATABASE_URL` or `TEST_DATABASE_URL`. |

**Choosing the reference date.** The dashboard and the intelligence page anchor their windows on
the real current date in the hotel's time zone. Seed with `--reference-date today` for a demo you
will look at now. A fixed date gives an exactly reproducible database, but its "recent" weeks
drift into the past as the calendar moves on.

### Determinism

The script prints a **fingerprint**: a SHA-256 over every seeded row, keyed by natural key with
surrogate keys left out. The same seed, reference date and options give the same fingerprint on
any machine and any Python version. `--dry-run` prints it without a database.

Before committing, the rows are read back and compared with the plan. After committing, they are
read again on a fresh connection, and that reading is the fingerprint reported.

- **Randomness:** only `random.Random.random()`, seeded with an integer and split into
  independent streams by SHA-256.
- **Public identifiers:** UUIDv5 values derived from the seed.
- **Dates:** every date is an offset from the reference date, and the only calendar effect is the
  weekday. So moving the reference date by whole weeks moves the whole dataset exactly.
- **No trend, season or anomaly** is injected.
- **Outside the fingerprint:** the owner's password hash (salted) and the account's timestamps
  (the real `now()`).

### The declared observation period

Each hotel also gets one `demand_observation_periods` row (migration 0016): the span whose complete
booking record the seed wrote -- from the longest stay's length into the window (a stay begun
before the window is not written, and the longest one reaches that far in) to the day before the
reference date. Only inside it is a night with no stays a zero; the demand model, its dataset and
the intelligence forecasts read every other date as unknown. The dry run prints the span.

A demo database seeded **before** migration 0016 has no span, so after `alembic upgrade head` the
demand model answers `422 INSUFFICIENT_HISTORY` and the stay-dated forecasts report too little
history until one is declared. Reseed into a new database, or declare the span the seed would
have written for its reference date (`R` below), for each hotel:

```bash
python -m app.jobs.demand_observation declare --hotel <hotel public id> --from <R - 359 days> --to <R - 1 day>
```

with the default `--history-days 365` (in general `--from` is `R - history_days + 6 days`). The
command refuses a span that reaches the hotel's today, a reversed span, and one that shares a
date with a span already declared.

### Resetting safely

The script **never deletes, truncates, overwrites or migrates** anything. It writes in a single
transaction into a database that is at the migration head and holds no row in any application
table. Anything else is refused before the first write, with the tables that hold rows named. A
failure part-way rolls everything back.

To refresh the demo, for example to move it to a new reference date:

1. Create a **new** database.
2. Migrate it and seed it as above.
3. Point `DATABASE_URL` at it.
4. Drop the old one yourself, and only once you have confirmed nothing in it needs keeping. No
   script here will do that for you.

The seed does not populate these tables, which it leaves empty:

- `daily_hotel_metrics`: nothing in the application writes it.
- the audit trail
- demand predictions
- hotel documents
- copilot conversations
- LLM invocation records
