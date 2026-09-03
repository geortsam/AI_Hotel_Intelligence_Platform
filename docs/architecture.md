# Architecture

This document describes the intended architecture and the rules that later stages must
follow. It describes **intent**; it does not claim that code exists. Anything not yet built is
marked as such.

---

## 1. Separation of areas

Five areas, deliberately kept apart at the top level of the repository:

```
backend/    HTTP API and business logic (Python, FastAPI)
frontend/   Browser client (React, TypeScript, Vite)
database/   Migrations and raw SQL -- the database as a database
ml/         Datasets, training pipelines and trained artifacts
docs/       Documentation
tests/      Test suite, mirroring the source layout
```

The dependency rules between them:

| Area | May depend on | May never depend on |
|---|---|---|
| `backend/` | trained artifacts in `ml/models/` (read-only, at runtime) | `frontend/`, `ml/pipelines/` |
| `frontend/` | the HTTP API contract only | backend source, the database |
| `ml/` | `ml/data/` | `backend/` |
| `database/` | nothing | everything |

The consequence worth stating plainly: **training code and web code never import each other.**
The only thing that crosses between them is a file on disk -- a trained artifact plus its
evaluation record.

---

## 2. Backend layering

```
HTTP request
    |
    v
api/         routing, validation, status codes.       Knows HTTP. Knows no business rules.
    |
    v
services/    business rules, orchestration.           Knows the domain. Knows no HTTP, no SQL.
    |
    v
repositories/ query construction, data access.        Knows SQL. Knows no business rules.
    |
    v
db/ + models/ engine, session, ORM mapping
```

Calls travel downward only. A service never sees a `Request`; a repository never decides
whether an action is permitted; an endpoint never builds a query.

`schemas/` (Pydantic) defines what crosses the HTTP boundary. `models/` (SQLAlchemy) defines
what is stored. They are kept as separate types on purpose -- so that a change to the storage
layout is not automatically a public API change.

`core/` holds cross-cutting concerns: configuration, and later logging, error handling and
security.

**Current state:** only `core/config.py` and `main.py` exist. Every other layer directory is
an empty marker.

---

## 3. Configuration

Configuration comes from the environment, never from literals in code, so one artifact runs in
every environment. `backend/app/core/config.py` reads it into a validated Pydantic `Settings`
object, and `.env.example` is the checked-in documentation of every variable.

Development and production are separated by the `ENVIRONMENT` variable rather than by separate
code paths. One visible consequence today: interactive API docs are served everywhere except
production.

In Stage 1 every setting has a working default, so the backend starts on a fresh checkout with
no `.env` at all. As real secrets appear (starting with `SECRET_KEY` for authentication), the
settings object should refuse to start without them rather than fall back to a default -- a
misconfigured deployment must fail loudly instead of running insecurely.

---

## 4. Request lifecycle (target)

1. CORS middleware admits or rejects the browser origin.
2. *(later)* A request ID is assigned and bound to the logging context.
3. FastAPI validates the request against a Pydantic schema.
4. *(later)* Dependencies resolve the database session and the authenticated user.
5. The endpoint calls a service.
6. The service applies business rules and calls repositories.
7. The response is serialised through a response schema.
8. *(later)* Domain errors map to one uniform error envelope.

Steps 2, 4 and 8 do not exist yet. Only 1, 3, 5 and 7 have any code behind them, and the only
route is `/health`.

---

## 5. Data and ML architecture (planned)

The platform is an operational system *and* an analytics layer reading the same data.

```
operational tables ---> pipelines (offline) ---> artifact + metrics.json
                                                         |
                                                         v
                                          backend loads artifact, serves predictions
```

Four planned modules, none of them built:

| Module | Input | Output |
|---|---|---|
| Review sentiment | review text | polarity, aspect breakdown |
| Occupancy forecasting | booking history | occupancy per future date, with intervals |
| Room image classification | room photographs | room type / feature tags |
| Recommendations | user and hotel history | ranked hotel suggestions |

Rules these must follow when they are built:

- Predictions are persisted with the model version that produced them, so a number can always
  be traced to an artifact.
- A missing artifact surfaces as an explicit unavailable-model error. It never degrades into a
  fabricated prediction.
- For time-series work, evaluation uses a rolling-origin backtest, not a single split.

---

## 6. Deployment (planned, unverified)

Docker Compose defines three services -- `db` (PostgreSQL 16), `api`, `frontend` (built assets
behind nginx). The API image excludes ML dependencies and trained weights; artifacts are
mounted rather than baked in, so retraining does not require an image rebuild.

**These files have not been executed.** Docker is not installed on the current development
machine, so `docker-compose.yml` and both Dockerfiles are written from documented behaviour
and remain unverified.
