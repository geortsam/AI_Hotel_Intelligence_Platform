# Backup and restore

## 1. Purpose

The database is the only thing in this deployment that cannot be rebuilt. The API and the
frontend are stateless images; the schema is nine Alembic migrations; the rows are not
recoverable from anything else in the repository. This document is the procedure for making a
copy of them and for proving the copy works.

A backup nobody has restored is a belief, not a backup. The procedure below therefore ends with
a restore, and CI runs both halves on every push — see [§5](#5-what-ci-proves-on-every-push).

## 2. Scope and limitations

**In scope:** a logical backup of one PostgreSQL database with `pg_dump`, and a verified restore
into a separate PostgreSQL 18.6 instance with `pg_restore`.

**Not in scope, and not automated by this project:**

- scheduling — there is no cron, timer or backup service;
- off-host storage — no S3, no object storage, no cloud integration;
- encryption of the archive at rest;
- retention enforcement or rotation;
- point-in-time recovery (no WAL archiving, no `pg_basebackup`, no replication);
- restoring over a live production database.

A logical dump is a snapshot of one moment. It does not protect against anything that happened
after it was taken, and it is not a substitute for PITR if your tolerance for lost writes is
smaller than your backup interval.

**`docker compose down -v` destroys the database volume permanently and is not a backup
strategy.** Nothing in this repository can undo it. `down` without `-v` keeps the volume.

## 3. Prerequisites

- The stack is running and `migrate` has completed, so the schema is at `0011_demand_prediction_public_id`.
- You can reach the database container, e.g. `docker compose ps db` shows it healthy.
- Somewhere to put the archive that is **not** the same disk as the database volume. A backup
  that dies with the host it protects is not a backup.

`pg_dump` and `pg_restore` ship inside the `postgres:18.6-alpine` image, so nothing needs to be
installed on the host. Their versions are printed by the CI gate on every run; running the tools
from inside the container also guarantees they match the server version exactly.

## 4. Backup

### 4.1 Identify the database

```bash
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT current_database(), version_num FROM alembic_version"
```

Confirm the database name is the one you intend and the revision is the one you expect. If the
revision is not `0011_demand_prediction_public_id`, stop and find out why before backing up.

### 4.2 Take the dump

```bash
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  --format=custom --file=/tmp/hotel-intelligence.backup
docker compose cp db:/tmp/hotel-intelligence.backup ./hotel-intelligence-$(date -u +%Y%m%dT%H%M%SZ).backup
docker compose exec -T db rm -f /tmp/hotel-intelligence.backup
```

Three things about that command are deliberate:

**`--format=custom`, not a plain SQL file.** The custom archive is compressed, it can be
inspected without being applied (`pg_restore --list`), it can be restored selectively, and
`pg_restore` can order objects correctly. A `.sql` text dump is only replayable start to finish.

**No password anywhere.** `docker compose exec db` runs inside the container and authenticates
over its local socket, so no credential reaches your command line, your shell history, the
process list or the terminal log. Prefer this over `pg_dump "postgresql://user:pass@host/db"`,
which puts the password in all four. If you must run `pg_dump` from outside the container, use
`PGPASSWORD` from your secret store or a `~/.pggpass` file — never an inline URL.

**Written inside the container, then copied out.** The archive is binary; copying the file
avoids any chance of a shell mangling it on the way through stdout.

### 4.3 Verify the archive before you trust it

```bash
ls -l hotel-intelligence-*.backup
pg_restore --list hotel-intelligence-*.backup | head -40
```

Or, without host tooling:

```bash
docker compose cp ./hotel-intelligence-<stamp>.backup db:/tmp/check.backup
docker compose exec -T db pg_restore --list /tmp/check.backup | grep -E "TABLE (users|hotels|alembic_version)"
```

A non-empty file whose table of contents lists the tables you expect is the minimum. It is still
not proof that it restores — only [§6](#6-restore-into-a-disposable-database) is that.

## 5. What CI proves on every push

The `docker-runtime` job in `.github/workflows/ci.yml` runs this whole procedure against a
disposable stack, and fails the build if any part of it does not hold:

1. Seeds deterministic data **through the real API** — register, log in, create a hotel — so
   what is backed up is application-written data, not rows injected into tables.
2. Captures a fact sheet from the source: the Alembic revision, base-table and view counts, row
   counts, the stored values including server-generated `public_id` UUIDs and timestamps, and
   md5 digests over the populated tables.
3. Takes a `--format=custom` dump, asserts it is non-trivially sized, and asserts
   `pg_restore --list` succeeds and names the expected tables.
4. Starts a **separate** `postgres:18.6-alpine` container and restores into it with
   `pg_restore --exit-on-error --no-owner --no-privileges`.
5. Re-runs the identical fact query against the restored database and requires the two outputs
   to be **identical, byte for byte**.
6. Runs the real API image against the restored database and requires `/health/db` to answer
   `200` with `database: reachable`.
7. Re-runs the fact query against the source to prove the source was only ever read.
8. Truncates a **copy** of the archive, restores it into a third database, and requires
   `pg_restore` to fail — so "the restore succeeded" is a claim with teeth.

`--exit-on-error` in step 4 is not decoration: without it `pg_restore` reports errors and still
exits 0, which would make a green gate meaningless.

## 6. Restore into a disposable database

This is the rehearsal, and the only way to know a backup is real. It touches nothing you depend
on.

```bash
docker run -d --name pg-restore-rehearsal \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  -e POSTGRES_DB=hotel_restore \
  postgres:18.6-alpine
```

Wait for it, then restore:

```bash
until docker exec pg-restore-rehearsal pg_isready -U postgres -d hotel_restore; do sleep 2; done

docker cp ./hotel-intelligence-<stamp>.backup pg-restore-rehearsal:/tmp/hotel.backup

docker exec pg-restore-rehearsal pg_restore \
  --exit-on-error --no-owner --no-privileges \
  -U postgres -d hotel_restore /tmp/hotel.backup
```

`--no-owner` and `--no-privileges` because the objects in the archive belong to the application
role, which does not exist in a fresh instance; without them `pg_restore` fails on every
`ALTER ... OWNER TO`. This is the normal cross-instance case and it does not change the data.

## 7. Post-restore verification

Do not stop at the exit code.

```bash
docker exec pg-restore-rehearsal psql -U postgres -d hotel_restore -tAc \
  "SELECT version_num FROM alembic_version"
```

Expect `0011_demand_prediction_public_id` — the same revision as the source, with no upgrade or
downgrade having happened.

```bash
docker exec pg-restore-rehearsal psql -U postgres -d hotel_restore -tAc \
  "SELECT count(*) FROM information_schema.tables
    WHERE table_schema='public' AND table_type='BASE TABLE'"
```

Expect **23** — the 22 application tables plus `alembic_version`. The
`historical_room_overlaps` view should also be present.

Then compare facts rather than eyeballing. Run the same query against the source and the
restored database and diff the two outputs — row counts for the tables that matter to you, and
stable stored values. CI does exactly this; the shape is:

```sql
SELECT 'count.bookings=' || count(*)::text FROM bookings
UNION ALL SELECT 'digest.hotels=' || md5(string_agg(slug || name, '|' ORDER BY slug)) FROM hotels
ORDER BY 1;
```

Finally, prove the application can use it. Point a disposable API container at the restored
database and ask its readiness probe, which runs a real query:

```bash
docker run --rm --network <the restore container's network> \
  -e DATABASE_URL="postgresql+psycopg://postgres:<password>@pg-restore-rehearsal:5432/hotel_restore" \
  -e ENVIRONMENT=production -e SECRET_KEY="<a throwaway value>" \
  hotel-intelligence-api:local \
  python -c "from fastapi.testclient import TestClient; from app.main import create_app; \
c = TestClient(create_app()).get('/health/db'); print(c.status_code, c.json()); \
assert c.json()['database'] == 'reachable'"
```

Clean up when finished:

```bash
docker rm -f pg-restore-rehearsal
```

## 8. Production disaster recovery

Everything above restores into an **empty** database. Replacing a production database is a
different operation and this project does not automate it, because it cannot know your
tolerance for downtime or for lost writes.

The distinction that matters:

| | Restore into empty | Replace production |
|---|---|---|
| Target | a fresh database nobody is using | the database the API is serving from |
| Risk | none | total, if done wrong |
| Requires an outage | no | **yes** |
| Automated here | yes, in CI | **no, deliberately** |

If you must replace production, the shape of it is: stop the API so nothing is writing
(`docker compose stop api frontend`); take a fresh dump of the current database **even though
you think it is broken**, because it is the only copy of the state you are about to discard;
restore into a *new* database name and verify it with §7 before switching; then repoint and
restart. Restoring over the live database in place, with `--clean`, leaves you with nothing if
the restore fails halfway.

Rehearse it before you need it. A recovery procedure first attempted during an incident is a
guess.

**Alembic `downgrade` is not disaster recovery.** Every revision defines one and the test suite
exercises them, but against disposable databases — they drop columns and tables, and against
real data that is destruction, not recovery.

## 9. Retention and storage

Unautomated, and the decisions are yours. What the procedure assumes:

- Keep archives **off the host** that runs the database. A backup on the same disk protects you
  from `down -v` and from nothing else.
- Keep more than one. A single archive that turns out to be damaged is no archive.
- Date-stamp the filename, as §4.2 does, so ordering is obvious.
- Decide a retention window and enforce it deliberately; nothing here deletes old archives.
- Rehearse a restore on a schedule, not only after a failure.

Archive size grows with the data, chiefly `audit_events`. Custom format is compressed by
default.

## 10. Security

**The archive contains everything the database contains** — every user row, every Argon2id
password hash, every audit event. Treat it with the same care as the database itself:

- store it where the database's own credentials would be acceptable;
- never commit one to Git. Nothing in `.gitignore` will save you from `git add -f`;
- never attach one to an issue, a ticket or a CI artifact;
- prefer encryption at rest if it leaves the host.

In CI the archive lives in `$RUNNER_TEMP`, outside the workspace, is never uploaded, and is
deleted during teardown. The passwords involved are generated per run and masked.

Password hygiene for the commands themselves is covered in §4.2: run the tools inside the
container so no credential reaches a command line.

## 11. What this project does not automate

No scheduler. No off-host copying. No encryption. No retention or rotation. No PITR or WAL
archiving. No production replacement. No backup API, and no backup endpoint — backups are an
operator action performed with database credentials, not something the application exposes.

These are absences by decision, not oversights. Adding any of them is a deliberate change with
its own operational cost.

## 12. Recovery checklist

When you actually need this:

1. **Stop writing.** `docker compose stop api frontend`. Leave `db` running.
2. **Dump the current state anyway**, broken or not (§4.2). It is the only copy of what you are
   about to replace.
3. **Choose the archive** and confirm it with `pg_restore --list` (§4.3).
4. **Restore into a new, empty database** — never over the live one (§6).
5. **Verify** revision, table count, row counts and stored values against what you expect (§7).
6. **Verify the application can serve from it** with the `/health/db` check (§7).
7. **Switch over** deliberately: repoint `DATABASE_URL`, `docker compose up -d`, confirm the API
   is healthy and the frontend serves.
8. **Write down what was lost** — everything between the archive's timestamp and the incident.
9. **Keep the failed database** until you are certain the recovery is good.
