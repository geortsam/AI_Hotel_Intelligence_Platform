# Tests

Tests live at the repository root and mirror the source layout, so a test's location tells you
what it covers.

| Path | Covers |
|---|---|
| `conftest.py` | Shared fixtures: test settings and a `TestClient`. |
| `db_safety.py` | The guard between a mistyped `TEST_DATABASE_URL` and lost data. |
| `backend/` | Everything that needs no database: configuration, schemas, error handling, and the architectural rules (layering, no SQL in routers, no commits in repositories). |
| `integration/` | The PostgreSQL suite: domain behaviour, authorization, and concurrency under real transactions. |
| `ml/` | Offline ML pipelines. Empty, because `ml/` holds no pipeline to test — the shipped statistical layer is covered from `backend/` and `integration/`. |

Root-level tests import `app` because `pythonpath = ["backend"]` is set in the root
`pyproject.toml`; run pytest from the repository root.

```bash
pytest -q
```

## The PostgreSQL suite and its guard

`integration/` is **destructive by design**: it runs `alembic downgrade base` once per session
and truncates after every test. It skips entirely unless `TEST_DATABASE_URL` is set, and even
then `db_safety.py` refuses any target that does not pass **both** a name rule and a contents
check — a database that already holds application data is rejected unless it has been
deliberately marked disposable. A name alone is not enough, because this project's CI container
and a developer's demo database can share one.

Never point it at a demo or shared database. `scripts/testdb.py` creates, marks, checks and
drops a scratch database safely:

```bash
python scripts/testdb.py create my_scratch_test
```

## Frontend tests

Frontend tests live beside the code they cover, as `frontend/src/**/*.test.tsx`, and run under
Vitest rather than pytest:

```bash
cd frontend && npm test
```
