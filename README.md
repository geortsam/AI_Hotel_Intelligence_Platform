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
8. [Testing and quality checks](#testing-and-quality-checks)
9. [Environment variables](#environment-variables)
10. [Future AI/ML components](#future-aiml-components)
11. [Known limitations](#known-limitations)

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
| http://localhost:8000/docs | Swagger UI — listing `/health` and nothing else |

### Frontend (not verified — see [limitations](#known-limitations))

```bash
cd frontend && npm install && npm run dev
```

### Docker (not verified — see [limitations](#known-limitations))

`POSTGRES_PASSWORD` must be set in `.env`; Compose refuses to start without it.

```bash
docker compose up --build
```

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

The development machine has **no Node/npm and no Docker**. Consequently:

- `frontend/` — `package.json`, `tsconfig.json`, `vite.config.ts` and the React entry files are
  written from documented defaults but have **never been installed, built or run**. No lockfile
  exists.
- `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile` — **never built or
  validated**, not even with `docker compose config`.

Every such file carries a comment saying so. The backend, its tests, the linter and the type
checker *are* verified and run on Python 3.14.6.
