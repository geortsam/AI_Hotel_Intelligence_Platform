# AI Hotel Intelligence Platform

An AI-powered hotel management and analytics platform: a FastAPI backend, a PostgreSQL domain
model, a React dashboard, and four planned machine-learning modules (review sentiment,
occupancy forecasting, room-image classification, hotel recommendations).

> ### Current state: **backend complete through Stage 3B.12 — no frontend, no authentication**
>
> The backend implements eleven domains against a 16-table PostgreSQL schema: hotels, room
> types, rooms, amenities, guests, bookings, payments, reviews, the financial ledger,
> analytics, and a deterministic statistical intelligence layer. 38 paths, 68 operations, all
> verified against live PostgreSQL.
>
> There is **no authentication or authorization** — every endpoint is open, and that is the
> largest remaining production blocker. There is **no dashboard**, **no Docker**, and **no
> deployment tooling**. The ML layer is explicitly a transparent statistical baseline, not a
> trained model: no LLMs, embeddings or agents. Nothing here is a placeholder pretending to be
> a feature — see [docs/backend-architecture.md](docs/backend-architecture.md) for what exists
> and what does not.

---

## Table of contents

1. [Project description](#project-description)
2. [Objectives](#objectives)
3. [Architecture](#architecture)
4. [Technologies](#technologies)
5. [Project structure](#project-structure)
6. [Development stages](#development-stages)
7. [How to run](#how-to-run)
8. [First run](#first-run)
9. [Testing and quality checks](#testing-and-quality-checks)
10. [Environment variables](#environment-variables)
11. [Future AI/ML components](#future-aiml-components)
12. [Known limitations](#known-limitations)

---

## Project description

Hotels sit on data they rarely use: reviews nobody reads in aggregate, booking history nobody
projects forward, and photo libraries nobody organises. This platform is intended to be the
operational system *and* the analytics layer on top of it — a property-management core
(hotels, rooms, bookings, reviews) with AI modules that read that same data and feed a
dashboard.

The engineering emphasis is on the parts usually skipped in ML portfolio projects: layer
separation, a real relational schema with constraints, object-scoped authorization,
migrations, a uniform error contract, and a strict boundary between offline training and
online inference.

## Objectives

1. **Operational core** — manage hotels, rooms, bookings and reviews through a typed, versioned
   REST API backed by a properly constrained relational schema.
2. **Analytics on the same data** — sentiment, forecasting, image classification and
   recommendations reading the operational tables rather than a parallel dataset.
3. **Honest ML** — every reported metric traceable to a real evaluation run; a missing model
   fails loudly instead of inventing a prediction.
4. **Production posture** — environment-driven configuration, containerised deployment,
   migrations, tests and type checking from the first stage rather than retrofitted.
5. **Incremental delivery** — seven stages, each verified before the next begins.

## Architecture

Five areas, kept separate at the top level, with one-way dependencies between them:

```
frontend/  ──HTTP──▶  backend/  ──SQL──▶  database (PostgreSQL)
                          │
                          └──reads──▶  ml/models/  ◀──writes──  ml/pipelines/ (offline)
```

Inside the backend, calls travel in one direction only:

```
api/  ──▶  services/  ──▶  repositories/  ──▶  db/ + models/
```

An endpoint knows HTTP but no business rules; a service knows the domain but no SQL; a
repository knows SQL but decides nothing. Training code and web code never import each other —
the only thing crossing that line is a file on disk.

Full detail, including the rules later stages must follow:
**[docs/architecture.md](docs/architecture.md)**.

## Technologies

| Area | Choice | Role |
|---|---|---|
| Backend | Python 3.12+, FastAPI, Uvicorn | Async HTTP API, OpenAPI generated from types |
| Validation | Pydantic v2, pydantic-settings | Request/response schemas, environment config |
| ORM | SQLAlchemy 2.0 | Data mapping *(declared; unused until Stage 2)* |
| Database | PostgreSQL 16 | System of record. SQLite may back local tests later |
| Migrations | Alembic | Schema history *(Stage 2)* |
| Frontend | React 18, TypeScript, Vite | Dashboard SPA |
| AI/ML | pandas, NumPy, scikit-learn | Data pipelines and models *(declared, not installed)* |
| Infrastructure | Docker, Docker Compose | Reproducible local and deployed stack |
| Quality | pytest, ruff, mypy | Tests, linting, static types |

## Project structure

```
AI_Hotel_Intelligence_Platform/
├── backend/           FastAPI application
│   ├── app/
│   │   ├── api/v1/        HTTP layer      (empty — Stage 2)
│   │   ├── core/          config.py       (the only implemented module)
│   │   ├── db/            engine/session  (empty — Stage 2)
│   │   ├── models/        ORM models      (empty — Stage 2)
│   │   ├── schemas/       Pydantic I/O    (empty — Stage 2)
│   │   ├── repositories/  data access     (empty — Stage 2)
│   │   ├── services/      business logic  (empty — Stage 2)
│   │   └── main.py        app factory + /health
│   ├── Dockerfile
│   ├── requirements.txt
│   └── requirements-dev.txt
├── frontend/          React + TypeScript + Vite scaffolding
├── database/          migrations/ and init SQL  (empty — Stage 2)
├── ml/                data/, pipelines/, models/, notebooks/  (empty — Stage 4+)
├── docs/              architecture.md, development-roadmap.md
├── tests/             root-level suite mirroring the source layout
├── pyproject.toml     ruff · mypy · pytest · coverage
├── docker-compose.yml db + api + frontend
├── .env.example
└── .gitignore
```

## Development stages

| Stage | Scope | Status |
|---|---|---|
| 1 | Foundation: structure, config, tooling, docs, `/health` | **done** |
| 2 | Database schema, models, migrations (2A–2C) | **done** |
| 3B.1–3B.9 | Domain API: hotels → ledger, over a frozen schema | **done** |
| 3B.10 | Analytics: KPIs, daily series, per-currency money | **done** |
| 3B.11 | Intelligence: forecasting, trend, anomalies, insights | **done** |
| 3B.12 | Cross-domain integration and backend hardening | **done** |
| — | Authentication and object-scoped authorization | not started |
| — | Review sentiment (trained model) | not started |
| — | Room-image classification and recommendations | not started |
| — | Dashboard, Docker, deployment, CI | not started |

Detail and exit criteria: **[docs/development-roadmap.md](docs/development-roadmap.md)**.

## How to run

### Backend (verified)

From the repository root:

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
```

On macOS or Linux use `.venv/bin/python` throughout.

Copying the environment template is optional in Stage 1 — every setting has a working default:

```bash
cp .env.example .env
```

Start the API:

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --reload
```

| URL | What you get |
|---|---|
| http://localhost:8000/health | `{"status":"ok", ...}` |
| http://localhost:8000/health/db | `{"status":"ok","database":"reachable", ...}`, or 503 when it is not |
| http://localhost:8000/docs | Swagger UI — 50 paths, 82 operations. Disabled when `ENVIRONMENT=production` |

### Frontend

```bash
cd frontend && npm ci && npm run dev
```

`npm ci` rather than `npm install`: `package-lock.json` is committed, and CI installs from it.

### Docker

`POSTGRES_PASSWORD` must be set in `.env`; Compose refuses to start without it. Set `SECRET_KEY`
too if you intend `ENVIRONMENT=production` — the API refuses to start in production without one.

```bash
docker compose up --build
```

The stack starts in a fixed order, each step gated on the previous one succeeding:

```
db healthy  →  migrate exits 0  →  api healthy  →  frontend starts
```

`migrate` is a one-shot service running `alembic upgrade head`; the API does not start unless it
exits 0, so the schema can never be behind the code that serves it. Browse the application at
http://localhost:5173 — nginx serves the built SPA and proxies `/api/` to the API on the same
origin, so no second origin and no CORS are involved.

This flow is exercised on every push: see [Continuous integration](#continuous-integration).

## First run

A freshly migrated database is empty, and the two things a new installation needs come from
two different places.

**The first hotel needs no manual step.** Register, sign in, and create it through the API —
the creator receives the `owner` membership in the same transaction as the hotel row:

```bash
POST /api/v1/auth/register  →  POST /api/v1/auth/login  →  POST /api/v1/hotels
```

**The first platform administrator has no API, by design.** `platform_admins` governs the
catalogues every hotel shares, so the grant is a deliberate out-of-band database write rather
than something an endpoint hands out — the same grant the integration suite performs. It
requires that the intended person has already registered, and it is idempotent:

```sql
INSERT INTO platform_admins (user_id, role)
SELECT u.id, 'platform_admin' FROM users u WHERE u.email = lower('ops@example.com')
ON CONFLICT (user_id) DO NOTHING;
```

An empty database is not authorization: nothing here creates an administrator because it
noticed no rows.

**Read [docs/deployment/first-run-bootstrap.md](docs/deployment/first-run-bootstrap.md) before
running the grant** — it covers the checks to make first, how to verify the result, secret
handling, what is *not* audited, and what to do if the wrong user was granted.

## Testing and quality checks

All run from the repository root:

```bash
.venv/Scripts/python.exe -m pytest -q
```

```bash
.venv/Scripts/python.exe -m ruff check .
```

```bash
.venv/Scripts/python.exe -m mypy backend/app
```

Coverage:

```bash
.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing
```

### PostgreSQL integration tests

The integration suite is **destructive**: it runs `alembic downgrade base` and
`TRUNCATE ... RESTART IDENTITY CASCADE`. It runs only when `TEST_DATABASE_URL` is set, and
only against a database that has proved it is disposable. Without the variable those tests
skip as PENDING; the rest of the suite runs normally and needs no database.

**Never point `TEST_DATABASE_URL` at the demo database, or at any database you would miss.**
A `_test` suffix is not enough on its own — this project's CI container and its demo database
share the name `hotel_intelligence_test` — so the guard also reads the target and refuses one
that already holds application data.

Get a safe database with the helper, which creates it, marks it disposable, and will not drop
anything that has not cleared the guard:

```bash
.venv/Scripts/python.exe scripts/testdb.py create my_scratch_test
.venv/Scripts/python.exe scripts/testdb.py check  my_scratch_test
.venv/Scripts/python.exe scripts/testdb.py drop   my_scratch_test
```

Then, with `TEST_DATABASE_URL` set to that database:

```bash
.venv/Scripts/python.exe -m pytest tests/integration
```

If the target is not disposable the suite stops before any SQL runs and prints what it found,
what it refused to do, and how to proceed. See `tests/db_safety.py` and
`docs/database-implementation.md`.

Tests live at the repository root and import `app` via `pythonpath = ["backend"]` in
`pyproject.toml`.

### Continuous integration

Every push and pull request to `main` runs three jobs in parallel on `ubuntu-latest`
(`.github/workflows/ci.yml`):

| Job | What it runs |
|---|---|
| `quality-gates` | Ruff lint, Ruff format check, mypy, the Alembic chain against a disposable PostgreSQL 18.6, then the full pytest suite |
| `Frontend quality gates` | `npm ci`, the Vitest suite, both TypeScript projects, the production build |
| `Docker runtime verification` | builds both images and **runs the real Compose stack** |

The third job is the one worth knowing about. It is not a lint of the YAML: it starts the
stack in a disposable, run-scoped Compose project and asserts, among other things, that
PostgreSQL reports 18.6, that `migrate` exits 0 and leaves the schema at `0009`, that the API
and frontend both become healthy, that nginx serves the SPA at `/`, `/bookings`, `/reviews`
and `/intelligence`, that `/api/v1/` is proxied through to FastAPI while an unknown `/api/`
path still returns a real 404 rather than the SPA, that `SECRET_KEY` and `POSTGRES_PASSWORD`
reach no frontend container or served asset, that a second `alembic upgrade head` is a no-op,
that the schema survives a stop/start, and that **a deliberately broken migration prevents the
API from starting at all**. Everything is torn down afterwards, volumes included.

That is why the deployment files in this repository are no longer described as unverified.

## Environment variables

Every variable is documented in [`.env.example`](.env.example). Configuration is read from the
environment only — never hard-coded — so one artifact runs in every environment.

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `development` | `development`, `test`, `staging` or `production` |
| `DEBUG` | `false` | |
| `APP_NAME` | AI Hotel Intelligence Platform | Shown in the OpenAPI title |
| `API_V1_PREFIX` | `/api/v1` | Reserved for Stage 2 |
| `LOG_LEVEL` | `INFO` | |
| `CORS_ORIGINS` | localhost:5173, localhost:3000 | Comma-separated |
| `DATABASE_URL` | *(empty)* | Declared; unused until Stage 2 |
| `SECRET_KEY` | *(empty)* | Declared; unused until authentication exists |

`.env` is git-ignored. `.env.example` contains no real secrets and never should.

In production, `/docs`, `/redoc` and `/openapi.json` are disabled automatically.

## Future AI/ML components

None of these exist yet. Each will be built as its own stage, with its dependencies in
`ml/requirements-ml.txt`, its pipeline in `ml/pipelines/`, and its artifacts plus evaluation
record in `ml/models/<model>-<version>/`.

| Module | Input | Output | Stage |
|---|---|---|---|
| **Review sentiment** | Review text | Polarity plus an aspect breakdown (cleanliness, staff, location, value) and token-level explanations | 4 |
| **Occupancy forecasting** | Booking history | Occupancy per future date with prediction intervals, validated by rolling-origin backtest | 5 |
| **Room-image classification** | Room photographs | Room type and feature tags for automatic media organisation | 6 |
| **Recommendations** | User and hotel history | Ranked hotel suggestions, evaluated against a popularity baseline | 6 |

Rules these must follow, fixed now so they are not negotiated later:

- Training is offline and never imports the web layer; serving loads an artifact from disk.
- Predictions are persisted with the model version that produced them.
- A metric may only be quoted from a real evaluation run recorded in `metrics.json`.
- A missing artifact returns an explicit unavailable-model error — never a fabricated number.

## Known limitations

The development machine still has **no Docker**, so nothing here is built or run locally. It is
built and run on every push instead — see [Continuous integration](#continuous-integration) —
which is where the frontend and the deployment are actually verified. Node 24 and npm are
available locally; `package-lock.json` is committed.

What remains genuinely missing is operational rather than functional:

- **No backup or restore.** There is no `pg_dump` procedure, no restore tooling and no recovery
  runbook. `docker compose down -v` destroys the database permanently and nothing can bring it
  back. This is the largest gap between "runs correctly" and "safe to run in production".
- **No TLS.** nginx listens on port 80 only. The stack terminates no TLS and the repository does
  not yet say whether termination belongs here or upstream, so the HSTS settings in
  `.env.example` cannot take effect.
- **`TRUSTED_PROXIES` is empty by default**, which is the safe value and the wrong one behind
  nginx: the backend then attributes every request to the proxy, so the per-address login rate
  limit becomes one bucket shared by all users and audit events name the proxy rather than the
  client. Set it to the Compose network's subnet before exposing the stack to real users.
- **The API port is published** (`API_PORT`, default 8000), bypassing nginx and its headers. It
  is kept published deliberately while the deployment is young, because it is how a first
  bring-up is debugged.
- **nginx resolves `api` once at startup.** If the API container is recreated with a new address
  the proxy keeps the old one and answers 502 until nginx itself restarts — and the frontend
  healthcheck reads a static file, so the container still reports healthy. Recovery today is
  `docker compose restart frontend`.
- **Base images use mutable tags** and are not digest-pinned, so a rebuild is not guaranteed to
  reproduce the same image. The API image also runs Python 3.12 while CI runs 3.14.
- **Two stacks cannot share one host** — the services declare fixed `container_name` values,
  which Docker scopes to the daemon rather than to the Compose project.
- **No platform-administrator API.** Granting platform administration is a deliberate
  out-of-band database write; see [First run](#first-run). That is by design, not an omission.

The ML layer remains a transparent statistical baseline rather than a trained model, as
described under [Future AI/ML components](#future-aiml-components).
