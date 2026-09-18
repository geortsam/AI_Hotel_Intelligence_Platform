# Development roadmap

**V1 is complete and verified.** This document records what V1 contains and what is deliberately
left for V2.

The working rule throughout was: *a stage is not started until the previous one is implemented,
tested, verified and documented*, and each stage ended with a report naming what was built, which
files were touched, the test results and the known issues. That is how the history below was
produced, and it is why the V2 section names nothing as done.

Nothing in the V2 section exists in this repository. Where a V2 item is mentioned elsewhere in
the documentation it is labelled the same way.

---

# V1 — COMPLETE / VERIFIED

Verified at commit `ba770f1` by the final project audit: 4016 backend tests and 998 frontend
tests passing, Ruff / format / mypy clean, 82 API routes, 9 linear migrations at head
`0009_audit_booking_deleted`, and the deployment exercised on real containers in CI.

## Foundation and configuration

Repository structure with the five areas separated · root tooling (ruff, mypy, pytest,
coverage) · pinned dependencies · environment template · a validated Pydantic settings module
that refuses to construct a production configuration without a `SECRET_KEY`.

## Database

PostgreSQL **18.6**. Nine linear Alembic migrations, head `0009_audit_booking_deleted`, no
branch points. The schema carries its rules rather than delegating them to application code:
55 CHECK constraints, 18 `ON DELETE RESTRICT` / 10 `CASCADE` / 3 `SET NULL` foreign keys, a GiST
exclusion constraint over half-open date ranges for room allocation, a deferred trigger asserting
night-completeness, a database-level append-only trigger on the audit table, generated columns
for derived metrics, currency-format checks, and composite foreign keys carrying `hotel_id` so
the database itself refuses a cross-tenant row.

## Domain API

Eleven domains over that schema — hotels, room types, rooms, amenities, guests, bookings,
payments, reviews, the financial ledger, analytics and intelligence — as 82 routes behind a
layered backend: routers → schemas → services → repositories → SQLAlchemy → PostgreSQL.
Repositories never commit; services own the transaction boundary.

Includes: availability search (single, multi-room and mixed room-type) · the booking state
machine · stay modification and in-house extension · server-side pricing with per-night rates ·
financial repricing · payments, refunds and reconciliation · a pagination envelope · a uniform
error contract that leaks no SQL, SQLSTATE, constraint name or traceback.

## Authentication and authorization

Argon2id password hashing · **access tokens only** (HS256 JWT; there is no refresh token) ·
token revocation on password change · enumeration-resistant login · per-address rate limiting on
authentication · a four-level hotel role hierarchy — `viewer` < `staff` < `manager` < `owner` —
with last-owner protection · platform administration as a separate capability gating the global
catalogues and the platform audit read.

Authorization is structural: every hotel-scoped service is reached through
`HotelServiceDep → HotelAccessPolicyDep → CurrentUserDep`, so there is no path to a domain
service that skips it. 77 of the 82 routes require authentication.

## Audit trail

An append-only `audit_events` table enforced by a database trigger, automatic coverage of
mutating actions, booking-deletion auditing, hotel-scoped and platform-scoped read endpoints,
and a retention/archive path that copies, verifies field by field, and only then commits —
rolling back rather than letting an unverified copy become the record.

## Intelligence

A **deterministic statistical baseline**, computed on request, implemented in the Python
standard library, carrying `MODEL_VERSION = "1.0.0"`:

- seasonal-naive day-of-week median forecasting for occupancy and revenue
- MAD-based prediction intervals
- MAD modified z-score anomaly detection (Iglewicz & Hoaglin, threshold 3.5)
- split-window median trend detection against an explicit threshold
- deterministic insight templates

**No trained model, no LLM, no embeddings, no vector database, no RAG, no agent.** See
[`architecture.md` §5](architecture.md#5-data-and-intelligence-architecture) and the stage
snapshot in [`ml-design.md`](ml-design.md).

## Observability and web security

Request IDs propagated through logs and responses, including the 500 path · structured logging
configuration · security headers · a trusted-proxy model that believes forwarding headers from
exactly one declared peer · HSTS gated on production plus a trusted HTTPS forwarding chain ·
CORS · CSP, Referrer-Policy, Permissions-Policy, X-Content-Type-Options, X-Frame-Options.

## Frontend

React 18 + TypeScript 5.7 strict + Vite 6. Authentication flow, protected routing, and feature
areas for dashboard, bookings and booking mutations, payments, financials, reviews, guests,
rooms, availability, hotel and room-type management, membership administration, platform
administration and intelligence. Route-level code splitting across 13 lazy routes; one HTTP
seam; no client-side money arithmetic.

## Deployment

Docker Compose: `db` → `migrate` → `api` → `frontend`, chained on health and on the migration
having exited 0. nginx terminates TLS and is the only published service; the API and the
database have no host ports. Backup and restore are documented and proven by restoring into a
separate instance and comparing the data byte for byte. Base images are pinned by digest and CI
builds each image twice to prove every shipped file is identical.

## CI

Four GitHub Actions jobs on every push: `quality-gates`, `frontend-quality-gates`,
`docker-runtime` and `image-reproducibility`. The Docker job does not lint YAML — it runs the
real stack in a disposable project and asserts the topology, TLS, the trust boundary, backup and
restore, and that a failed migration blocks the API from starting.

---

# V2 — FUTURE / NOT IMPLEMENTED

**None of the following exists in this repository.** No code, no dependency, no configuration.

## Machine learning

| Item | Note |
|---|---|
| Trained occupancy forecasting | replacing the statistical baseline; requires a dataset, a rolling-origin backtest as the figure of record, and per-version evaluation records |
| Richer feature pipeline | `ml/pipelines/` is an empty placeholder today |
| Review sentiment | polarity and aspect breakdown over review text |
| Room-image classification | class set fixed before training |
| AI recommendations | evaluated with ranking metrics against a popularity baseline |
| Model evaluation and versioning discipline | a metric may only be quoted from a real run written to `metrics.json` |

Rules these must follow, unchanged from the original plan: predictions persisted with the model
version that produced them; a missing artifact surfacing as an explicit unavailable-model error
rather than a fabricated number; time-series evaluation by rolling-origin backtest, never a
single chronological split.

## Generative AI

| Item | Note |
|---|---|
| LLM hotel analyst | none of the repository's "intelligence" is generative today |
| Retrieval-augmented generation | no vector store, no embeddings |
| Agent / LangGraph workflows | no agent framework of any kind |
| AI evaluation harness | would be required before any of the above could be claimed |

## Security

| Item | Note |
|---|---|
| `HttpOnly; Secure; SameSite` cookie sessions | replaces `sessionStorage` for the access token; a backend change — see [`architecture.md` §7.1](architecture.md#71-session-handling-and-its-limitation) |
| Refresh tokens | V1 issues access tokens only |
| Platform-administrator API and bootstrap | granting platform administration is an out-of-band database write today |

## Operations

| Item | Note |
|---|---|
| ACME / certificate automation | certificates are replaced by hand today |
| Off-site encrypted backups and PITR | backup is a deliberate operator action |
| Zero-downtime deployment | a single-nginx, single-API stack has nothing to route to during a replacement |
| Dependency update automation | no Renovate, no Dependabot |
| SBOM, signing, provenance | digest pinning is a prerequisite for these, not a substitute |
| Transitive Python dependency pinning | direct dependencies are pinned; their dependencies are resolved at build time |
