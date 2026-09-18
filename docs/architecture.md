# Architecture

This document describes the architecture of the system as it stands at V1, and the rules it is
held to. Where something is planned rather than built, it says so in those words.

It was originally written as a statement of intent before the code existed. It is now a
description of what is there: every rule below is enforced by code and asserted by the test
suite. The stage-dated companions -- [`backend-architecture.md`](backend-architecture.md),
[`database-design.md`](database-design.md), [`database-implementation.md`](database-implementation.md),
[`analytics-design.md`](analytics-design.md) and [`ml-design.md`](ml-design.md) -- are snapshots
of the stage each one names, kept as the record of how the system was built. This document and
the runbooks under [`deployment/`](deployment/) are the ones maintained as current.

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

`core/` holds cross-cutting concerns: configuration, logging, the error taxonomy, password
hashing and tokens, rate limiting, request-id propagation, security headers and the
trusted-proxy client-address rules. `middleware/` carries the two ASGI middlewares that apply
the last two per request.

**Transaction ownership, which is the layering rule with teeth.** A repository never commits.
`grep` for `.commit()` under `backend/app/repositories/` returns nothing, and it is meant to
stay that way: repositories build and run queries, services decide when a unit of work is
finished. The request-scoped session in `db/session.py` commits on success and rolls back on
an exception, so a service that raises cannot leave a half-written change behind.

**Multi-tenant isolation is structural rather than conditional.** Every hotel-scoped service is
reached through `HotelServiceDep -> HotelAccessPolicyDep -> CurrentUserDep`, so there is no path
to a domain service that skips authentication — see `backend/app/api/deps.py`. Where a
cross-tenant lookup would otherwise be expressible, the repository method simply does not exist,
and the database backs this up with composite foreign keys that carry `hotel_id`. Public
identifiers at the API boundary are UUIDs; internal `BIGINT` keys are never serialised.

**Current state at V1:** every layer directory is populated —
`api/` 28 files, `services/` 23, `schemas/` 20, `repositories/` 18, `models/` 14, `core/` 9,
`db/` 3, `middleware/` 3, `ml/` 2. The HTTP surface is 82 routes, of which 77 require
authentication; the 5 that do not are the version-metadata endpoint, registration, login and
the two health probes.

---

## 3. Configuration

Configuration comes from the environment, never from literals in code, so one artifact runs in
every environment. `backend/app/core/config.py` reads it into a validated Pydantic `Settings`
object, and `.env.example` is the checked-in documentation of every variable.

Development and production are separated by the `ENVIRONMENT` variable rather than by separate
code paths. One visible consequence today: interactive API docs are served everywhere except
production.

Most settings have a working default so a fresh checkout runs, but that stops where it should:
`Settings` refuses to construct a **production** configuration without a `SECRET_KEY` and will
not fall back to a built-in one. A misconfigured deployment fails loudly instead of running
insecurely, and because `database/migrations/env.py` constructs the same `Settings`, it fails at
the migration rather than later in the API.

---

## 4. Request lifecycle

1. A request ID is assigned and bound to the logging context (`middleware/request_id.py`).
2. CORS middleware admits or rejects the browser origin.
3. FastAPI validates the request against a Pydantic schema.
4. Dependencies resolve the database session and the authenticated user, and — for every
   hotel-scoped route — the caller's membership of that hotel.
5. The endpoint calls a service.
6. The service applies business rules and calls repositories.
7. The response is serialised through a response schema.
8. Domain errors map to one uniform error envelope (`core/errors.py`).
9. Security headers are applied on the way out (`middleware/security_headers.py`).

All nine steps exist. Step 8 is worth reading in full before changing anything near it: the
envelope is deliberately uninformative about internals, and 500-class faults carry one fixed
sentence so that a client cannot learn a constraint name, a relation or a SQLSTATE from an
error response. The detail goes to the logs, correlated by the request id from step 1.

---

## 5. Data and intelligence architecture

The platform is an operational system *and* an analytics layer reading the same data.

### What exists at V1

A **deterministic statistical intelligence layer**, computed on request from the operational
tables. It is not trained, not learned and not generative:

| Capability | Method |
|---|---|
| Occupancy and revenue forecasting | seasonal-naive day-of-week median, falling back to the window median when a weekday bucket is too thin |
| Prediction intervals | median absolute deviation, scaled by 1.4826 |
| Anomaly detection | modified z-score on the MAD, Iglewicz & Hoaglin threshold 3.5 |
| Demand trend | split-window median comparison against an explicit relative threshold |
| Insights | deterministic templates over the figures above |

It lives in `backend/app/ml/timeseries.py` and `backend/app/services/intelligence.py`, is
implemented in the **Python standard library** — no NumPy, pandas or scikit-learn — and carries
`MODEL_VERSION = "1.0.0"`. Every response is a pure function of the hotel, the requested dates
and the model version, so the same question always returns the same answer.

Two properties are deliberate. The forecast method is **returned per point** rather than hidden
behind one header, so a caller can see which points fell back to the window median. And the
trend response returns both window medians *and* the threshold, so its classification can be
recomputed by hand.

**There is no LLM, no embedding, no vector database, no retrieval-augmented generation, no
agent framework and no external AI service anywhere in this repository.** See §5.3.

### 5.1 The V2 demand dataset (Stage 6.1) — a dataset, not a model

A separate layer, added without touching anything above. It turns the operational tables into a
leakage-safe training dataset: one observation per hotel per calendar date, targeting realised
room-night demand under the same `OCCUPANCY_STATUSES` definition the analytics layer uses.

    MlDemandRepository     SQL, one hotel at a time     app/repositories/ml_demand.py
        |
        v
    MlDatasetService       orchestration, writes nothing  app/services/ml_dataset.py
        |
        v
    app.ml.dataset         pure features, rules, splits   app/ml/dataset.py

Every feature is tied to an explicit prediction cutoff and declared by when it becomes knowable;
splitting is chronological with no shuffle parameter to misuse. **No model is trained and none
exists**, the V1 statistical layer above is unchanged, and there is no public endpoint — the
pipeline is called programmatically. See
[ml-dataset-design.md](ml-dataset-design.md), particularly its limitations.

### 5.1a The offline training dataset (Stage 6.2) — still not a model

The production database holds too little history to train on, which Stage 6.1 measured rather
than assumed. Stage 6.2 acquires real history instead of manufacturing it: a published, CC BY
4.0 hotel-booking dataset (Antonio, de Almeida & Nunes, 2019 — two hotels, 2015–2017), pinned
to a commit, checksum-enforced on every run, and transformed into 1,462 rows over 737 dates.

```
ml/pipelines/offline_demand.py  ---imports--->  app.ml.dataset   (the §5.1 contract)
        |
        v
ml/data/processed/  (ignored)   +   ml/manifests/  (committed: checksums, ranges, partitions)
```

The arrow is the point: the offline pipeline does not define a contract of its own, so a model
trained on its output would consume the same columns, order and cutoff semantics as one served
against the production database. `ml/` is excluded from the backend build context, so nothing
here reaches the API image. **No model is trained, none exists, and no dependency capable of
training one is installed.** See [ml-training-data.md](ml-training-data.md).

### 5.1b The offline backtest (Stage 6.3) — a measurement, not a deployment

The first stage that fits an estimator, and it fits it entirely offline. A seasonal-naive
baseline and one `HistGradientBoostingRegressor` are backtested over 54 chronological origins at
a 7-day horizon; the record lands in `ml/models/demand_baseline_v1/metrics.json`.

```
ml/models.py       feature admissibility, baseline, estimator   (the only sklearn import)
ml/evaluation.py   rolling origins, folds, the chronology assertion
ml/metrics.py      MAE, RMSE, sMAPE, with their denominators
ml/manifests.py    the evaluation record and its content checksum
```

Three properties matter architecturally. **The horizon decides the feature set**: at seven days
only 9 of the 15 contract columns are knowable, and the other six are excluded with recorded
reasons rather than by hand. **No artifact is persisted** — nothing was serialised, so nothing
could be served. **`backend/` gained no dependency**: scikit-learn is pinned in
`ml/requirements-ml.txt`, installed by CI and not by the API image, and a test asserts that no
module under `backend/app` imports it or NumPy or SciPy.

The measured numbers, and a section on why they establish neither production accuracy nor
cross-hotel generalisation: [ml-model-evaluation.md](ml-model-evaluation.md).

### 5.2 The offline / online boundary (offline half only, no artifact)

```
operational tables ---> pipelines (offline) ---> artifact + metrics.json
                                                         |
                                                         v
                                          backend loads artifact, serves predictions
```

`ml/` holds this structure — `data/`, `pipelines/`, `manifests/`, `models/`, `notebooks/`. Only
the first half of the diagram exists: data preparation (Stage 6.2), an offline backtest and its
`metrics.json` (Stage 6.3). **There is no trained artifact**, so the arrow into the backend has
nothing to carry, and the backend has no path that would load one. `ml/requirements-ml.txt` pins
scikit-learn, CI installs it and the API image does not.

### 5.3 FUTURE — NOT IMPLEMENTED

None of the following exists. They are recorded as direction, not as capability:

| Module | Input | Output |
|---|---|---|
| Review sentiment | review text | polarity, aspect breakdown |
| Served occupancy forecasting | booking history | a learned model replacing the statistical baseline; Stage 6.3 backtested one offline and persisted nothing |
| Room image classification | room photographs | room type / feature tags |
| Recommendations | user and hotel history | ranked hotel suggestions |

Rules these must follow when they are built:

- Predictions are persisted with the model version that produced them, so a number can always
  be traced to an artifact.
- A missing artifact surfaces as an explicit unavailable-model error. It never degrades into a
  fabricated prediction.
- For time-series work, evaluation uses a rolling-origin backtest, not a single split. Stage 6.3
  is the first stage to do this; see [ml-model-evaluation.md](ml-model-evaluation.md).

---

## 6. Deployment (runtime-verified in CI)

Docker Compose defines four services:

| Service | Image | Role |
|---|---|---|
| `db` | `postgres:18.6-alpine` | The database. Not published to the host. |
| `migrate` | the API image | One-shot `alembic upgrade head`, then exits. |
| `api` | built from `backend/Dockerfile` | uvicorn, running as non-root `appuser`. |
| `frontend` | built from `frontend/Dockerfile` | nginx serving the built SPA and proxying `/api/`. |

Startup is a chain of conditions rather than a sequence of delays:

```
db healthy  →  migrate exits 0  →  api healthy  →  frontend starts
```

`migrate` is a separate service from `api` deliberately. `pg_isready` proves the server accepts
connections, not that a schema exists, and the API's own readiness probe is `SELECT 1`, which
succeeds perfectly well against an empty database -- so without this gate a stack on a fresh
volume would come up with every probe green and every data endpoint failing.
`service_completed_successfully` makes the API's start conditional on the migration having
actually succeeded.

The API image excludes ML dependencies and trained weights. It carries the application package
plus `alembic.ini` and `database/migrations/`, which is why its build context is the repository
root rather than `backend/`: one image both serves requests and applies migrations.

**These files are executed on every push.** A `docker-runtime` job on GitHub Actions builds both
images and runs the stack in a disposable, run-scoped Compose project, asserting the startup
chain, the PostgreSQL version, the schema revision, the SPA fallback, the `/api` proxy, secret
isolation, migration idempotency, persistence across a restart, that the running nginx keeps
serving a REPLACED api container without being restarted itself, and that a failed migration
blocks the API from starting. The repository README lists the gates.

nginx terminates TLS on 443 and is the only published service; port 80 redirects to it, and the
API and database have no host ports. The backend trusts exactly one forwarding peer -- the
frontend container's fixed address -- which is what lets rate limiting, audit attribution and
HSTS see the real client. See [deployment/tls.md](deployment/tls.md).

nginx reaches the API through a variable upstream, against a resolver generated at container
start from the container's own `/etc/resolv.conf`, so a recreated api container is picked up by
the running nginx rather than leaving it bound to the address it resolved once. Nothing in the
compose file is scoped to the Docker daemon rather than to the project, so two stacks coexist on
one host given their own address space. See [deployment/robustness.md](deployment/robustness.md).

Every external base image is pinned by digest rather than by a tag someone else can move, and
the API image runs the same Python the test suite does. CI builds each image twice from scratch
and requires every shipped file to be byte-identical; what that does and does not claim is in
[deployment/reproducibility.md](deployment/reproducibility.md).

Not present, and not claimed here: automated certificate issuance or renewal, and any multi-host
or orchestrated deployment. This is a single-host Compose deployment, and CI proves its TLS with
a self-signed certificate, which is not the same as public-internet readiness.

---

## 7. Frontend / backend separation

The browser client is a separate application that knows the HTTP contract and nothing else. It
holds no database connection, no ORM, no SQL and no shared code with `backend/`; the only thing
crossing between them is JSON over `/api/v1`.

Two consequences are enforced rather than encouraged:

- **One HTTP seam.** `frontend/src/services/api/client.ts` is the only module that calls
  `fetch`, and the only place an `Authorization` header is constructed. A feature module cannot
  reach the network past it.
- **No client-side authority.** Money is never computed in the browser. `frontend/src/lib/decimal.ts`
  works on strings and compares digit by digit; a monetary value becomes a JavaScript number only
  at the display edge, to be handed to `Intl.NumberFormat`. Prices, totals, refunds and
  permissions are all decided server-side, and the browser renders what it is told.

In production both are served from **one origin**: nginx serves the built SPA and proxies
`/api/` to the API, so the bundle is built with an empty API base URL and no CORS is involved.

### 7.1 Session handling, and its limitation

The backend authenticates with a bearer JWT and exposes no cookie-session endpoint. A token the
browser must attach to a header is a token JavaScript must be able to read, so the access token
is kept in **`sessionStorage`** (`frontend/src/session/tokenStorage.ts`).

**This is a documented limitation, not a solved problem.** Any cross-site-scripting flaw in the
application could read that token; no choice of Web Storage changes that, because `localStorage`
and `sessionStorage` are equally readable by script on the origin. `sessionStorage` is chosen
over `localStorage` only for lifetime — it is scoped to the tab and cleared when the tab closes,
while still surviving a reload — and the access token lives 30 minutes.

What reduces the exposure today: the content security policy is `script-src 'self'` with no
inline script and no `eval` in the bundle, and every storage access is confined to four
functions in one module. **The genuine fix is an `HttpOnly; Secure; SameSite` cookie issued by
the backend, which script cannot read at all. That is a backend change and is V2 work** — see
[development-roadmap.md](development-roadmap.md).
