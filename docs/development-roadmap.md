# Development roadmap

The platform is built in seven stages. **A stage is not started until the previous one is
implemented, tested, verified and documented.** Each stage ends with a short report: what was
built, which files were touched, the commands to run it, the test results, and known issues.

Status legend: **done** · **in progress** · **not started**

---

## Stage 1 — Foundation · *done*

**Goal:** a clean, scalable skeleton that runs, with the five areas separated from day one.

Deliverables: repository structure · root tooling configuration (ruff, mypy, pytest,
coverage) · pinned dependencies · environment template · `.gitignore` · Docker Compose
topology · frontend scaffolding · architecture and roadmap documents · a settings module and
an application entrypoint exposing a single `/health` probe · tests covering the probe and the
settings.

**Exit criteria:** `pytest`, `ruff` and `mypy` pass; `uvicorn` starts; `/health` returns 200;
`/docs` lists no route other than `/health`.

Explicitly *not* in this stage: schema, ORM models, migrations, API endpoints, authentication,
ML code, dashboards.

---

## Stage 2 — Database and domain API · *done*

> Delivered as 2A–2C (schema design, implementation, live verification) and 3A–3B.12
> (FastAPI foundation, eleven domains, analytics, intelligence, hardening).

**Goal:** the operational core — the system of record the analytics later read.

Deliverables: PostgreSQL schema with real constraints (checks, unique constraints, explicit
foreign-key policies, indexes on the query paths later stages need) · SQLAlchemy models ·
Alembic migration environment and an initial revision · session management and dependency
wiring · repository and service layers · CRUD for hotels, rooms, bookings, reviews and
amenities · pagination envelope · uniform error contract · structured logging with request IDs.

**Exit criteria:** migrations apply to an empty database and roll back; the domain rules
(availability as half-open intervals, server-side pricing, booking state machine) are covered
by tests.

---

## Stage 3 — Authentication and authorization · *not started*

> **Re-ordered in practice.** The domain API, analytics and intelligence layers were built
> first, over a frozen schema. Authorization is now the single largest production blocker;
> see `docs/backend-architecture.md` §11 for how it slots into the existing scope resolver.

**Goal:** identity, and access decisions made against the object being touched.

Deliverables: password hashing · JWT access and refresh tokens with separated token types ·
roles (guest, hotel manager, administrator) · object-scoped authorization, so one manager
cannot reach another's data · login that does not leak whether an account exists.

**Exit criteria:** the authorization matrix is covered by tests, including the negative cases.

---

## Stage 4 — Review sentiment · *not started*

**Goal:** the first ML module, end to end.

Deliverables: a data pipeline into `ml/data/processed/` · a baseline model · a stronger
transformer model · aspect-level breakdown · an evaluation record per model version · a
serving path in the backend with persisted annotations carrying model provenance.

**Exit criteria:** metrics come from a real evaluation run written to `metrics.json`; a
missing artifact returns an explicit unavailable-model error.

---

## Stage 5 — Occupancy forecasting · *partially delivered*

> A deterministic statistical baseline (seasonal-naive day-of-week median, robust anomaly
> detection, split-window trend) shipped in Stage 3B.11 — see `docs/ml-design.md`. What
> remains for this stage is a trained model, backtesting and validation metrics.

**Goal:** turn booking history into a forward view.

Deliverables: a daily-occupancy dataset built from bookings · several models compared on equal
terms · a rolling-origin backtest as the figure of record · forecasts persisted and later
scored against actuals · feature-importance explanations.

**Exit criteria:** the backtest — never a single chronological split — is what any reported
number comes from.

---

## Stage 6 — Computer vision and recommendations · *not started*

**Goal:** the two remaining ML modules.

Deliverables: room-image classification over a labelled image set, with the class set fixed
before training · a recommender over user and hotel history, evaluated with ranking metrics
against a popularity baseline.

**Exit criteria:** each beats its baseline on a held-out split, or the result is reported as
negative rather than quietly dropped.

---

## Stage 7 — Dashboard and hardening · *not started*

**Goal:** make the platform usable and deployable.

Deliverables: the React dashboard — authentication flow, domain views, and a view per ML
module including its explanations · loading and error states throughout · rate limiting ·
security headers · a CI pipeline running lint, types and tests · deployment documentation.

**Exit criteria:** a clean checkout can be brought up from documented commands alone, and CI
is green.

---

## Notes on the current development machine

Node/npm and Docker are **not installed**, so frontend and container work cannot be executed
or verified here. Anything written for them ships unverified and is labelled as such until it
runs somewhere that has the toolchain. Backend verification runs against the local Python
3.14 environment.
