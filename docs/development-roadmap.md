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

# V2 — IN PROGRESS

## Stage 6.1 — Demand dataset and feature pipeline · *done*

A reproducible, leakage-safe dataset over the existing domain data, and the feature pipeline a
later model stage can consume. **No model was trained, and none exists.**

Target: daily hotel room-night demand, one observation per hotel per calendar date, derived from
`booking_room_nights` under the existing `OCCUPANCY_STATUSES` definition. Features are tied to an
explicit prediction cutoff and classified by when they become knowable; chronological splitting
only, with no shuffle parameter to misuse. Implemented in the standard library, with no new
dependency and no schema change.

Detail, including the two schema facts that bound what is reconstructible and the measured
reasons the demo data cannot train anything: **[ml-dataset-design.md](ml-dataset-design.md)**.

## Stage 6.2 — Offline training dataset · *done*

Stage 6.1 measured that the demo database cannot train anything. Stage 6.2 is the answer that
is not fabrication: a real, published, CC BY 4.0 hotel-booking dataset — Antonio, de Almeida &
Nunes (2019), two hotels, 2015–2017 — acquired from a commit-pinned URL, checksum-enforced, and
transformed into **1,462 rows over 737 dates** that satisfy the Stage 6.1 contract rather than a
contract of their own. **No model was trained, and none exists.**

Bookings are expanded into room nights rather than counted as arrivals; dates whose demand the
source cannot fully account for are dropped as targets rather than believed; missing values stay
missing. One feature, `rooms_existing_at_cutoff`, is **NOT SUPPORTED** by the source and reports
as unavailable instead of being fabricated as zero. Standard library only, no new dependency, no
schema change, no API change, and the raw and processed payloads stay out of Git under the
ignore rule that already existed — what is committed is the manifest.

Detail, including provenance, licence, feature compatibility, the truncation rule and the
measured limits: **[ml-training-data.md](ml-training-data.md)**.

## Stage 6.3 — Baseline demand model and rolling-origin backtest · *done*

The first stage that fits anything, and it fits it **offline only**. A seasonal-naive baseline
(demand seven days earlier) and one learned regressor — scikit-learn's
`HistGradientBoostingRegressor`, the single dependency added — backtested over **54
chronological origins** at a 7-day horizon, 744 predictions, none skipped.

Measured, pooled over all 744: baseline MAE 18.371 / RMSE 28.166 / sMAPE 12.652 %; learned
17.502 / 27.000 / 12.473 %. The learned model is ahead on 32 of 54 folds, and the pooled gap of
0.87 room nights sits inside a per-fold spread of roughly ±50 — the two methods fail on
different weeks. **No winner is declared and no accuracy is claimed.**

The forecast horizon costs features and the cost is computed, not assumed: at seven days only 9
of the 15 contract columns are knowable, and `rooms_existing_at_cutoff` is excluded outright
rather than imputed. **No model artifact was persisted in that stage** (Stage 6.5 later fitted one), no API endpoint exists, the schema and
the 82-operation public API are untouched, and `backend/` gained no ML dependency —
`.dockerignore` keeps `ml/` out of the API image and a test asserts the application imports none
of it.

Detail, including the protocol, the fold table, the metric definitions and a section on why the
numbers establish neither production accuracy nor cross-hotel generalisation:
**[ml-model-evaluation.md](ml-model-evaluation.md)**.

## Stage 6.4 — Model validation and offline registry · *done*

Stage 6.3 measured. Stage 6.4 asks whether that measurement can be trusted, under an acceptance
policy **declared in code before the result was computed** — and structurally incapable of
seeing which method won, because `AcceptanceEvidence` carries no metric value at all. **Result:
PASS, 13 of 13 criteria**, all of them about reproducibility, identity and leakage rather than
accuracy. There is deliberately no "the learned model must beat the baseline" criterion.

Nothing was tuned, no origin was re-chosen and the Stage 6.3 record was not rewritten: the
protocol and the estimator configuration are pinned by checksum, and the re-run reproduced all
54 fold boundaries field by field.

What the robustness analysis found, and it is the point of the stage: the per-fold standard
deviation of MAE is **15.15 / 14.12** against a pooled difference of **0.87** — roughly
seventeen times larger. The two methods fail in different months (the learned model is ahead
December–August and behind September–November) and in different directions (all ten of its worst
days are under-forecasts; the baseline's run both ways). `demand_baseline_v1` is therefore the
**current offline candidate** — reproducible, pinned and re-verified — and nothing more.

A registry entry and a model card were added; **no artifact was persisted and no endpoint
serves anything**. Detail: **[ml-model-validation.md](ml-model-validation.md)** and
**[ml-model-card.md](ml-model-card.md)**.

## Stage 6.5 — Model artifact and offline inference contract · *done*

The first fitted artifact. The exact Stage 6.3 estimator, fitted **once** on the dataset's
declared training partition: 872 partition rows are selected, 56 are held out for having no
`demand_lag_28` yet, and **816 reach `estimator.fit()`** — 2015-09-23 to 2016-11-09, two hotels,
414 dates — persisted as a 695 KB standard-library pickle. **No dependency was added**: joblib is present
only as a scikit-learn requirement and is deliberately not used directly.

**The payload is not committed.** `.gitignore` has excluded model weights since Stage 1, and a
pickle is arbitrary code on load. What is committed is `artifact.json`, whose checksums make a
regenerated payload verifiable. The loader validates that metadata — schema, versions, dataset
checksum, feature columns, horizon, payload digest — **before** it deserialises anything, which
is the whole of the trust boundary.

Two checksums, because they answer different questions: the payload's SHA-256 (byte-identical
across refits on this build — an observation, not a cross-toolchain claim) and a **canonical
model digest** over the feature columns, the configuration, the training extent and the model's
predictions on a fixed probe grid. Fold 11 of the Stage 6.3 backtest is handed the same partition, the
same 816 rows reach the estimator in the same order, and a test compares both `fit` calls at the
call boundary; the artifact reproduces the fold's fourteen learned predictions **exactly**.

`ml/inference.py` is the offline contract: typed in, typed out, keyed by public UUID, and
deliberately unhelpful — a missing, extra, permuted, NaN, infinite, boolean or wrongly-typed
feature is an error rather than something to fix silently, and `fit` is never called. **Stage 6.5
establishes an offline artifact and an inference contract. It establishes no production serving,
no production accuracy, no online inference, no API, no monitoring and no drift detection.**

Detail: **[ml-model-card.md](ml-model-card.md)** §§10–12.

---

# V2 — FUTURE / NOT IMPLEMENTED

**None of the following exists in this repository.** No code, no dependency, no configuration.

## Machine learning

| Item | Note |
|---|---|
| A **served** trained forecast | Stage 6.5 produced the artifact; serving it still needs a loading path in the application, an unavailable-model error, an endpoint and a schema — none of which exists |
| Richer feature pipeline | the 7-day horizon admits 9 of 15 contract columns; a horizon-matched on-the-books and rolling-window feature set does not exist |
| Review sentiment | polarity and aspect breakdown over review text |
| Room-image classification | class set fixed before training |
| AI recommendations | evaluated with ranking metrics against a popularity baseline |
| A registry for several coexisting model versions | Stage 6.4 added a single-entry registry and a model card; nothing selects between versions, and no artifact is stored |

Rules these must follow, unchanged from the original plan: predictions persisted with the model
version that produced them; a missing artifact surfacing as an explicit unavailable-model error
rather than a fabricated number; time-series evaluation by rolling-origin backtest, never a
single chronological split. Stage 6.3 discharged the last of those three.

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
