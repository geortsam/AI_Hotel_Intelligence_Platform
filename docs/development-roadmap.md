# Development roadmap

**V1 is complete and verified.** This document records what V1 contains and what is deliberately
left for V2.

The working rule throughout was: *a stage is not started until the previous one is implemented,
tested, verified and documented*, and each stage ended with a report naming what was built, which
files were touched, the test results and the known issues. That is how the history below was
produced.

**There are two V2 sections and they mean different things.** *V2 — IN PROGRESS* holds numbered
stages, each carrying its state in its heading:

| Marker | Meaning |
|---|---|
| `· done` | implemented, tested, verified, documented, and locked |
| `· defined, not started` | specified in enough detail to be reviewed and built, and **no code exists** |

*V2 — FUTURE / NOT IMPLEMENTED* is a backlog of capabilities, not stages: one-line notes with no
objective, no acceptance criteria and no commitment. Nothing in it exists in this repository, and
an item only becomes a stage when it is written up as one. Where a V2 item is mentioned elsewhere
in the documentation it is labelled the same way.

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

PostgreSQL **18.6**. At the V1 boundary, nine linear Alembic migrations at head
`0009_audit_booking_deleted`, no branch points. *(Stages 6.8 and 6.11 have since added two more,
Stage 7.6 a third, Stage 7.7 a fourth, Stage 7.9 a fifth and Stage 7.11 a sixth: head is now
`0015_copilot_conversations` across 15 linear revisions, still with no branch points.)* The schema carries its rules rather than delegating them to application code:
63 CHECK constraints and 28 foreign keys — 17 `ON DELETE RESTRICT`, 8 `ON DELETE CASCADE`,
3 `ON DELETE SET NULL` — a GiST exclusion constraint over half-open date ranges for room
allocation, a deferred trigger asserting night-completeness, a database-level append-only trigger
on the audit table, generated columns for derived metrics, currency-format checks, and composite
foreign keys carrying `hotel_id` so the database itself refuses a cross-tenant row.

*Those two counts are measured at migration head `0009_audit_booking_deleted`, the V1 boundary:
CHECK constraints as `pg_constraint.contype = 'c'`, and the delete policies as
`information_schema.referential_constraints.delete_rule`. **`delete_rule`, not `ON UPDATE`** — two
of the eight delete-CASCADE keys also carry `ON UPDATE CASCADE`, and they are the same keys, not
additional ones. An earlier version of this paragraph read `55 CHECK constraints, 18 RESTRICT /
10 CASCADE / 3 SET NULL`. The 55 and the 10 are the Stage 2 figures for migration `0001`, where
55 CHECKs and 10 composite foreign keys are exactly right and
[database-implementation.md](database-implementation.md) still records them as such; they were
carried into a paragraph about `0009`. The 18 matched no measured revision at the V1 boundary, and
the 3 was correct throughout.*

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

**At V1: no trained model, no LLM, no embeddings, no vector database, no RAG, no agent.** The
last five are still true of the repository. The first stopped being true at Stage 6.6, which serves
a trained demand model *beside* this layer without altering any response above. See
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
rather than imputed. **No model artifact was persisted in that stage** (Stage 6.5 later fitted one), no API endpoint
existed, the schema and the 82-operation public API were untouched, and `backend/` gained no ML
dependency — at Stage 6.3 `.dockerignore` kept `ml/` out of the API image entirely. *(Stage 6.6
added the endpoint, and Stage 6.7 the dependency and a narrowed `ml/` allowlist in the image.
What still holds is that no module under `backend/app` imports an ML library directly, and a test
asserts it.)*

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

## Stage 6.6 — ML serving boundary and API contract · *done*

The artifact becomes reachable over HTTP, and **nothing about the model changes**. One
read-only route —

    GET /api/v1/hotels/{hotel_public_id}/ml/demand-forecast?target_date=…

— authenticated, hotel-scoped, membership-checked through the existing policy, taking the public
API from 82 operations to **83**. No migration, no schema change, no frontend change, and no new
production dependency: `backend/requirements.txt` is untouched and the API image still excludes
`ml/`, so the endpoint answers 503 there and is exercised where the runtime and a verified
artifact are present.

**The request carries no model internals.** A hotel and a date. There is no parameter for an
artifact, a path, a version, a column or an estimator, and `horizon_days` exists only so a caller
expecting something other than seven days is told so. The server owns artifact selection, every
version, the feature set and integrity verification.

**Features come from the Stage 6.1 primitives, not a second implementation**, and leakage is
prevented by the bounds of the one grouped query: the window is `[T-28, T-7]` and its upper bound
*is* the cutoff date, so the extraction is never handed a day the forecaster may not see. An
integration test writes bookings on the target day and the six before it and requires the
prediction to be byte-identical.

**Loading is validate-then-deserialise**, nine checks before a byte is unpickled and seven after
— including a behavioural one: the deserialised estimator is asked what it computes on a fixed
probe grid and must answer what the artifact recorded. The artifact still declares
`serving_enabled: false`; the approval lives in reviewed application code, so a file on disk
cannot authorise itself and Stage 6.5's records were not edited. Loaded once per process behind a
lock, never mutated, and every refusal collapses to one fixed 503 sentence.

**Not established by this stage:** production accuracy, cross-hotel generalisation, calibration
for a hotel of a different scale from the two it was fitted on, persistence of predictions,
monitoring, drift detection or any frontend surface.

Detail: **[ml-serving.md](ml-serving.md)**.

## Stage 6.7 — Production ML runtime and artifact delivery · *done*

Stage 6.6 built the endpoint; inside the production image it answered 503, because the image had
neither an ML runtime nor a model. This closes that, and **changes nothing about the model**.

**The artifact is regenerated, not shipped.** The payload is not committed and must not be, so
`backend/Dockerfile` gained a disposable `artifact-builder` stage that refits the approved model
from the committed dataset and **fails the build unless twenty-two approved values match** —
versions, dataset checksum, feature columns and order, horizon, estimator configuration and its
checksum, claims, training extent, the probe predictions and the canonical model digest.
Seventeen tampering cases are tested. They are not one per approved value — see
[ml-production-runtime.md](ml-production-runtime.md) §4 for why they cannot be.

**The identity is the canonical model digest, and that is a measured decision rather than a
convenience.** Refitting this model produces a different payload SHA-256 for every OpenMP thread
count — 1, 2, 4, 8 and 12 threads give five digests, all 711,530 bytes, all with the same
canonical digest `436bf6b3…`. The Stage 6.5 value was produced at twelve threads, this machine's
physical core count, which `artifact.json` never recorded. So the payload hash identifies a CPU
topology as much as a model; it is recorded as a build fact and the digest is enforced. Nothing
was relaxed: the payload is still verified against its own metadata before deserialisation, and
every Stage 6.6 runtime check still runs.

**One dependency** — `scikit-learn==1.9.1`, the pin the offline file already used — and roughly
150 MB. The image carries thirteen `ml/` modules, computed as the import closure of
`ml.artifact` and `ml.inference` and asserted against the Dockerfile, plus the artifact. It
carries **no dataset, no notebook and none of the five pipeline entry points**.

CI proves it in the real container against real PostgreSQL: a 200 with a real prediction through
nginx and TLS, byte-identical repeats, 8 concurrent requests agreeing, 401 / 404 / 404 / 422,
the served number equal to what the artifact computes directly, the model loaded exactly once
per process, the model loading with `--network none` and sockets disabled, no `.csv` anywhere in
the image, and a throwaway image with a truncated payload answering a clean 503.

**Still not established:** production accuracy, cross-hotel generalisation, or any improvement
in the model's poor scale behaviour for small hotels. Deployment changed where it runs.

Detail: **[ml-production-runtime.md](ml-production-runtime.md)**.

## Stage 6.8 — Production prediction persistence and observability foundation · *done*

> One new table, one migration — head moves to `0010_demand_predictions` — and **no API
> change at all**: the surface stayed at 51 paths / 83 operations through this stage, and the
> response body for a given request is what Stage 6.6 published. *(Stage 6.11 later moved the head
> to `0011_demand_prediction_public_id` and the API to 52 / 84, without touching this endpoint's
> contract.)*

**Objective: make every production demand prediction a durable, attributable record, and emit the
minimum signal needed to observe the serving path — without changing the model, the API contract
or a single number the endpoint returns.** A prediction served today is computed, returned and
forgotten; this closes that, and makes drift and accuracy work *possible later* without
attempting either.

It discharges the roadmap's standing V2 rule that predictions are persisted with the model
version that produced them.

**In scope.** One table, `demand_predictions`, written inside the transaction that serves the
request: hotel, target date, horizon, prediction cutoff, the predicted value, the full model
identity, the nine feature values, and the request id. One new Alembic migration, `0010_*`. One
structured log event per serving attempt, carrying outcome, model version, horizon and duration
and nothing else.

**The identity question, and the answer.** A prediction is keyed by
`(hotel_id, target_date, forecast_horizon_days, model_version, feature_digest)` — *not* by the
first four alone. A booking recorded late changes `demand_lag_7`, so the same hotel, date, horizon
and model can legitimately produce a different number tomorrow; keying without the inputs would
force a choice between overwriting history and rejecting a legitimate new prediction. With the
digest, a repeated request writes no second row and a changed input writes a new one. Same
instinct as Stage 6.5's canonical digest: hash the inputs, not the bytes.

**Model identity is unchanged.** `436bf6b3…` remains the approved model's identity and is stored
on every row. The artifact payload SHA-256 is deliberately **not** stored — Stage 6.7 measured
that it changes with the build machine's thread count, so it would record which CPU served the
request rather than anything about the prediction.

**Nothing is fabricated and nothing is half-recorded.** 401, 404, 422 and 503 all write no row,
and a persistence failure fails the request rather than serving a prediction nobody recorded — the
table's whole value is that it is complete.

**Explicitly not in this stage:** retraining, drift detection, accuracy measurement or any
production accuracy claim, a metrics exporter or dashboard, a read API for stored predictions,
retention policy, a new or replacement model, and any frontend. The richer feature pipeline,
multi-model registry, review sentiment, room-image classification and AI recommendations remain
separate backlog capabilities and are **not** folded in here.

**One architectural consequence, flagged in advance:** `DemandPredictionService` stops being
read-only and gains a transaction boundary. The precedent is Stage 4.5.12's audit trail, which
writes a record of what happened inside the transaction of the thing that happened. The endpoint
stays idempotent — a repeated request creates no second row.

**What it cost architecturally**, as flagged when the stage was defined: the serving service is
no longer read-only. It holds the session, commits, and rolls back — and the repository still
does neither, because the transaction boundary belongs to the service. A persistence failure
fails the request rather than returning a prediction nobody recorded.

**Proven against real PostgreSQL:** one row per served prediction with every field matching the
response, a repeat leaving the row and its `generated_at` untouched, a changed input writing a
second row beside the first, a direct duplicate `INSERT` refused by the constraint with no
Python involved, eight concurrent identical requests producing exactly one row, every refusal
path writing nothing, and a forced persistence failure returning no prediction and no partial
row.

Twenty-one concrete acceptance criteria, the full record shape, the observability and drift
contracts, and the distinction between operational monitoring, data drift, prediction drift and
actual accuracy: **[ml-prediction-persistence-design.md](ml-prediction-persistence-design.md)**.

## Stage 6.9 — Retrospective forecast accuracy measurement · *done*

> **No table, no migration, no endpoint, no CLI and no dependency.** At this stage's boundary the
> head stayed at `0010_demand_predictions` and the API at 51 paths / 83 operations. The result is
> computed and returned, never persisted. *(Stage 6.11 later moved both, to
> `0011_demand_prediction_public_id` and 52 / 84. Nothing in this stage changed.)*

**Objective: measure served predictions against realised demand under a protocol declared before
any number was computed — and report the result as a measurement, not as a production accuracy
claim.** Stage 6.8 named three gaps and closed the first; this is the second, *how far off were
we*. Drift remains the third and is untouched.

**The protocol is frozen and content-checksummed** in `backend/app/ml/accuracy_protocol.py`, and
carries no threshold, no pass mark, no baseline and no ranking field — so the evaluation cannot
reach a verdict, because there is nothing in its rules to reach one against.

**The 28-day settlement lag is an operational assumption and says so.** The transition graph
settles more than it looks: `checked_in` leads only to `checked_out` and both are occupancy, so
a night is already final for every checked-in or terminal allocation however long the guest
stays. Only `pending` and `confirmed` are volatile, and the protocol *derives* exactly those
two rather than restating occupancy. What no lag can bound is a row a property never resolves,
or a booking entered after the stay — measured here at 133 of 166 demo bookings. So the
evaluation measures whether its own assumption held: `unsettled_allocations` counts the
volatile allocations over the scored window, and `settled` is false when any remain.

**Selection is earliest, not latest**, for one decisive reason: once a target date has been
scored, no later prediction can change that score. Scoring the newest row would let a hotelier
re-requesting a forecast for a past date silently rewrite accuracy already measured.

**Small hotels are reported, never hidden.** Two segments with separate denominators and no
combined figure anywhere, split at the 40 room nights Stage 6.6 measured to sit below the
model's lowest learned bin edge — read from stored inputs only, never from the outcome.

Metrics are `ml/metrics.py`, imported and not restated, through a function-local bridge in
`app/ml/accuracy.py` and nowhere else. It costs nothing: that module is pure standard library
and already one of the thirteen the production image ships.

**Proven against real PostgreSQL:** the selection rule as `DISTINCT ON` executes it including
the `id` tie-break, a later prediction leaving an already-scored date untouched, the settlement
boundary at exactly 28 days and one day short, unsettled detection against real booking
statuses, two model versions never sharing a denominator, cross-tenant isolation with two
hotels holding predictions for the same dates, a non-member refused before anything is read,
and an evaluation leaving the database byte-for-byte unchanged.

**It does not establish production accuracy**, evaluate a threshold, compare against a
baseline, rank a model or trigger any retraining. Fifty acceptance criteria, the full protocol
and its limitations: **[ml-accuracy-measurement.md](ml-accuracy-measurement.md)**.

## Stage 6.10 — Prediction and feature distribution observation · *done*

> **No table, no migration, no endpoint, no CLI, no dependency and no Docker change.** At this
> stage's boundary the head stayed at `0010_demand_predictions` and the API at 51 paths / 83
> operations. Computed and returned, never persisted. *(Stage 6.11 later moved both, to
> `0011_demand_prediction_public_id` and 52 / 84. Nothing in this stage changed.)*

**Objective: make a hotel's stored model inputs and model outputs observable over an explicit
window, and comparable against an explicit baseline window.** The third of the three gaps Stage
6.8 named, and the last of them.

**It detects nothing.** No threshold, no verdict, no alert, no anomaly flag, no PSI, no KS, no
Jensen-Shannon — in the protocol, in the result, or anywhere this stage reaches. That is a
deferral Stage 6.8 §9 argued for before either stage existed: choosing a statistic before there
is a row to look at would be guessing, and a fresh deployment still has none. What changed is
that the distributions can now be looked at.

**The reference is a second window of stored predictions, not the training set** — decided on
evidence rather than preference. The committed manifest carries `target_statistics` and no
per-feature statistics at all, and the image ships neither `ml/manifests/` nor `ml/data/`, so an
absolute reference would have needed a new artifact and a Dockerfile change. A window-versus-
window reference needs neither, and every number comes from rows the platform actually served.

`distribution_v1` is frozen and checksummed, and declares all of it: the model's nine feature
columns in its own order plus the output, five statistics, five quantiles, and **one** quantile
convention — linear interpolation between order statistics, written out in the code that applies
it. A test cross-checks it against `statistics.quantiles(method="inclusive")`, and the median is
the `p50` quantile by construction. Empty series report `None` everywhere, never `0.0`.

**Nothing is recomputed, and nothing new was read.** The values summarised are the ones Stage 6.8
stored — what the model *was* given, not what it *would be* given today — and the read is Stage
6.9's `scorable_predictions` unchanged, so one selection rule still governs this codebase and no
repository method was added.

**Proven against real PostgreSQL:** summaries over real stored rows, a baseline window and its
absence, identical windows giving zero differences, model versions never sharing a summary, a
foreign digest reported and pooled into nothing, cross-tenant isolation, a non-member refused
before anything is read, one query per window, and the database unchanged after a run.

Forty-two acceptance criteria, the protocol, the quantile convention and the limitations:
**[ml-drift-observation.md](ml-drift-observation.md)**.

## Stage 6.11 — Tenant-scoped read API for stored demand predictions · *done*

> **One migration and one endpoint, both deliberate.** Head moves to
> `0011_demand_prediction_public_id`; the API moves **51 paths / 83 operations → 52 / 84** — the
> first API change since Stage 6.6 and the first migration since Stage 6.8. No new table: the
> base-table count stays at 23.

**Objective: let an authenticated member of a hotel read that hotel's own stored demand
predictions over an explicit bounded window.** Stage 6.8 made predictions durable, 6.9 measured
them and 6.10 observed their distributions — three readers, all programmatic, all invoked only
by tests. The hotels whose data those rows are had no path to a single one of them.

**Stage 6.8 wrote the precondition and this stage met it.** That stage withheld `public_id` and
said why: *"A future read API adds the column in its own migration rather than this stage
guessing the shape of one."* Migration `0011` is that migration.

**Every stored row, which is the point.** This is the deliberate opposite of Stage 6.9's
`scorable_predictions`, whose `DISTINCT ON` collapses a target date so an accuracy measurement
cannot count it twice. A hotel asking what it was told is owed every answer it was given, and
Stage 6.8 made repeats genuinely distinct rows rather than duplicates — a booking recorded late
changes `demand_lag_7`. Two repository methods, never one with a flag.

The order `target_date, generated_at, public_id` is **total**, because `public_id` is unique and
no two rows can tie on all three. Pagination rests on that: a non-total order lets equal rows
swap between pages, so a client walking them could see one twice and another never.

**Ten response fields and no internal identifier.** `feature_values`, `feature_digest`,
`canonical_model_digest` and `request_id` are withheld with reasons recorded in the schema. The
`public_id` is a surrogate rather than a fingerprint, so it differs between environments by
design — the deliberate opposite of the two content-derived digests on the same table.

**Proven against real PostgreSQL through the real HTTP surface:** the backfill of rows seeded
before `0011` and the downgrade, two predictions for one target date both returned while the
Stage 6.9 reader collapses them in the same test, inclusive boundaries, pages that concatenate to
the full ordered set exactly once, cross-tenant isolation, unauthenticated and non-member
refusals that are byte-identical to an unknown hotel's, two statements per read, and the database
unchanged afterwards.

**It establishes no production accuracy**, endorses no individual prediction and detects nothing.
It also exposes the known-weak small-hotel regime to exactly the properties it is invalid for,
which the documentation states where a reader will meet it. Retention remains out of scope and a
separate future capability: **[ml-prediction-read-api.md](ml-prediction-read-api.md)**.

---

# V2 — FUTURE / NOT IMPLEMENTED

**None of the following exists in this repository** — no code, no dependency, no configuration —
with one exception: the first row of the machine-learning table, which records what Stages 6.6-6.11
delivered out of this backlog and what of it is still outstanding. It is kept rather than deleted
so the served model can be read against the item it came from.

## Machine learning

| Item | Note |
|---|---|
| A **served** trained forecast | *Done in Stages 6.6 and 6.7* — loading path, unavailable-model error, endpoint, schema, and a production image that carries and executes the approved model. Persisted predictions and the observability foundation are **Stage 6.8**, *done*; retrospective accuracy measurement under a declared protocol is **Stage 6.9**, *done*, and establishes no production accuracy; distribution observation is **Stage 6.10**, *done*, and detects nothing; a tenant-scoped read API over the stored rows is **Stage 6.11**, *done*. Drift *detection* — a statistic, a threshold, an alert — retraining, and retention of stored predictions remain backlog |
| Richer feature pipeline | *Done offline in Stage 7.14* — horizon-matched datasets at 7, 14 and 28 days admit the rolling means and the on-the-books count (13 / 12 / 11 features), measured under the frozen `multi_horizon_v1` protocol; see [ml-multi-horizon.md](ml-multi-horizon.md). Still backlog: **serving** any of these models (with its own capacity-capping rule), and a production-equivalent on-the-books feature — the offline one is day-resolution and status-agnostic. The served 7-day model still uses 9 of 15 columns |
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
