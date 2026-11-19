# v2.0 roadmap — execution matrix and decision briefs

> **Status (2026-11-19).** Written at `2da02f0` (branch `r2.10c3c-adjudication`, CI run
> 44524291097 green) under your standing instruction to work through
> [roadmap-v2.0.md](roadmap-v2.0.md) R0–R6 without routine approval. This file says, for every
> task there, what is done, what is done now, and what waits for a decision only you can make.
> The rows are updated as each stage lands; §3 holds the decision briefs.

## 1. Constraints every row is held to

- **The retrieval fingerprint.** Decision F (§12 of
  [retrieval-evaluation-decision.md](retrieval-evaluation-decision.md)) fingerprints 47 files and
  every migration version: among them `.github/workflows/ci.yml`, `pyproject.toml`, both lock
  files, `backend/app/main.py`, `api/v1/router.py`, `api/deps.py`, `models/enums.py` and
  `repositories/hotel.py`. Changing any of them makes the pinned C.4 report stale, and CI fails
  until it is re-measured and re-pinned (§12.6). Such changes are therefore **batched into one
  re-pin stage** (§2, "Re-pin batch") instead of being pushed one by one.
- **Migrations** are added only as new, additive revisions, applied by CI to its ephemeral
  database and locally only to a disposable scratch database (`scripts/testdb.py`). The demo
  databases are never migrated by this work.
- **No external service, credential, payment, release or real data** is created, used or
  invented. Synthetic data stays labelled synthetic.
- **No gate is weakened.** Every stage adds its own tests and recorded mutations.

## 2. Matrix

Legend — **Local:** can be finished in this repository without credentials or a business
decision. **Touches:** M = migration, F = fingerprinted file (re-pin), CI = workflow, Sec = security
policy, Ext = external service, Rel = release.

| Task | State before this pass | Local? | Touches | Depends on | Verification | Outcome |
|---|---|---|---|---|---|---|
| **R0.1** cut `v1.0.1` | proposed (R0.1 audit `fae39f3`), never cut | no: publishing a release | Rel | your decision | tag points at a green commit | **decision** — §3.1 |
| **R0.2** CI independent of unauthenticated Docker Hub | partly: the restore image no longer pulls from Docker Hub (`e2abcf6`) | partly | CI, F | — | all 7 jobs green through the mirror; re-pin | re-pin batch (registry mirror for the Docker jobs); authenticated pulls need a secret — §3.2 |
| **R0.3** shard the backend CI job | **done** (`c1cdf3e`, 3 shards) | — | — | — | — | done |
| **R0.4a** Dependabot | none | config yes; activation no | Ext (on `main`) | merging to `main` | config parses; it is inert off the default branch | **E5**, then activation — §3.3 |
| **R0.4b** transitive Python pinning | **done** (`53fdd0e`, hashed universal locks) | — | — | — | — | done |
| **R1.1** `HttpOnly; Secure; SameSite` cookie session, refresh, CSRF | none; token in `sessionStorage` (architecture §7.1) | yes | Sec, F (`deps.py`) | — | backend and frontend suites, CSRF and revocation tests, mutations; re-pin | re-pin batch |
| **R1.2** email channel, password reset, deactivation | none | no | Sec, Ext, M | who may deactivate a global `users` row; an email provider | — | **decision** — §3.4 |
| **R1.3a** platform-administrator bootstrap | out-of-band SQL | yes | Sec | — | CLI tests against the scratch database, audit row, mutations | **E4** |
| **R1.3b** platform-administrator API | none | no | Sec | who may grant and revoke | — | **decision** — §3.5 |
| **R1.4a** shared rate limiter (Redis or PostgreSQL) | per-process | no | Ext or M, F | the backing store; a deployment change | — | **decision** — §3.6 |
| **R1.4b** request-size limits | **done** (`bcdabeb`) | — | — | — | — | done |
| **R1.4c** 429 declared in OpenAPI | declared on the rate-limited routes (login, copilot, conversations, knowledge) | yes | — | — | a test that every limited route declares 429 | **E2** (checked, pinned by test) |
| **R1.5** `hotels.timezone` against IANA names | any text accepted, unknown zones fall back to UTC | yes | — (schema only) | — | API tests, mutations | **E2** |
| **R2.1** a real or realistic data source | none verified (R2.4 feasibility `5b754f3`) | no | — | a dataset and its licence, or a pilot hotel | — | **decision** — §3.7 |
| **R2.2** accuracy and drift protocols on that data | blocked by R2.1 | no | — | R2.1 | — | blocked |
| **R2.3** live copilot evaluation, then Stage 7.15 | local Ollama pilots only; paid run excluded | no | Ext (paid) | spending; real documents | — | **decision** — §3.8 |
| **R2.4** serve 7/14/28-day models; settle `daily_hotel_metrics` | offline only; table reserved | no | M, F | R2.2's evidence | — | blocked |
| **R3.1** room-status history (`room_blocks`) | open decision 6, deferred | no | M, F | the block model and its constraint | — | **decision** — §3.9 |
| **R3.2** pending holds with expiry | decision 12: when a checkout flow exists | no | M, F | a checkout flow (R5) | — | blocked |
| **R3.3** `updated_at` and `If-Match` on the global catalogues | not present (H6 covers hotel-scoped resources) | yes | M, F | — | migration on CI and scratch, API tests, mutations; re-pin | re-pin batch |
| **R3.4** FX rates | decision 14: transaction currency only | no | M | multi-currency properties | — | **decision** — §3.10 |
| **R4.1** ACME certificates; off-site encrypted backups, PITR | manual certificates; local backups | no | Ext | a domain, a CA account, off-site storage | — | **decision** — §3.11 |
| **R4.2** a scheduler for the purges and the audit archival | operator commands | yes | — | — | job tests; Compose stays valid in CI | **E6** (opt-in) |
| **R4.3a** metrics exporter | none | partly | F (`main.py`) | where alerts go | — | §3.12 |
| **R4.3b** SBOM, image signing | none | SBOM partly; signing no | CI, Ext | a signing identity and a registry | — | §3.12 |
| **R5** portfolio view, exports, payments, OTA import, sentiment, housekeeping | none | no | — | the audience (roadmap §5) | — | **decision** — §3.13 |
| **R6.1** consolidate the stage-snapshot documents | 36 files under `docs/` | yes | — | — | links resolve; doc tests pass | this file and §4 |
| **R6.2** README counts that cannot go stale | hand-maintained, and stale (`7984 backend tests`) | yes | — | — | a test computes each count from the code | **E3** |
| §2.5 frontend suite on a loaded machine | `--maxWorkers=2` by hand | yes | — | — | Vitest config, frontend CI job | with E3 |

## 3. Decision briefs

Each brief names the choice, what follows from each option, a recommended default and the least
you would have to do.

### 3.1 Release `v1.0.1` (R0.1)

- **Choice:** tag `v1.0.1` on `main`, or skip it and tag the next line when PR #1 merges.
- **Consequences:** `v1.0.0`'s README carries stale counts; a tag on `main` today would release
  `2a14dc6`, without Decision F. A tag is public and permanent.
- **Recommended:** merge PR #1 first, then tag the merge commit as `v1.1.0` (it adds features
  beyond a patch), not `v1.0.1`.
- **Your action:** say which tag and on which commit; the tag itself is one command.

### 3.2 Docker Hub (R0.2)

- **Choice:** a public pull-through mirror (no credentials), or authenticated Docker Hub pulls.
- **Consequences:** the mirror is done here for the jobs that build and run images; GitHub pulls
  the jobs' `services:` containers before any step runs, so those still come from Docker Hub.
  Authenticated pulls cover everything but need a Docker Hub account and two repository secrets.
- **Recommended:** keep the mirror; add authenticated pulls only if a Hub outage recurs.
- **Your action:** none, or create `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` secrets.

### 3.3 Dependabot activation (R0.4a)

- **Choice:** activate the prepared `.github/dependabot.yml`, or not.
- **Consequences:** GitHub reads it only from the default branch. Once on `main`, it opens
  pull requests on a schedule; any that touches `pyproject.toml`, a lock file or `ci.yml` changes
  the retrieval fingerprint and needs a C.4 re-measure and re-pin before it can merge.
- **Recommended:** activate, weekly, grouped, as prepared.
- **Your action:** merge it to `main` (it arrives with PR #1).

### 3.4 Password reset, email and deactivation (R1.2)

- **Choice:** who may deactivate an account (`users` is global across hotels): only a platform
  administrator, or also an owner of every hotel the user belongs to; and which email provider.
- **Consequences:** a hotel owner deactivating a user would lock them out of other hotels;
  email needs a provider account, a sender domain and credentials.
- **Recommended:** platform administrators only; hotel owners remove membership instead (which
  exists). SMTP through any provider, configured by environment.
- **Your action:** confirm the rule; provide a provider when deploying.

### 3.5 Platform-administrator API (R1.3b)

- **Choice:** may a platform administrator grant or revoke the capability for others over the
  API, or does it stay an operator command?
- **Consequences:** an API makes a compromised administrator able to create more; the command
  (E4) keeps it on the server.
- **Recommended:** operator command only, as E4 builds.
- **Your action:** none, unless you want the API.

### 3.6 Shared rate limiter (R1.4a)

- **Choice:** Redis (a new service in the stack) or PostgreSQL (a table and a migration).
- **Consequences:** Redis adds a container, a backup question and a failure mode; PostgreSQL adds
  write load on every limited request. Either needs the deployment to run more than one worker
  before it matters: today's stack runs one.
- **Recommended:** PostgreSQL, when more than one worker is deployed.
- **Your action:** say when the deployment will scale out.

### 3.7 Data for the intelligence claims (R2.1)

- **Choice:** a public dataset (licence permitting), or a pilot hotel's data under an agreement.
- **Consequences:** R2.4 found no public dataset with the fields the contract needs; a pilot
  hotel needs a data agreement. Without either, no accuracy claim can be made, and none is.
- **Recommended:** a pilot property, if one is available; otherwise the claims stay as stated.
- **Your action:** provide a source and its permission.

### 3.8 Live copilot evaluation (R2.3)

- **Choice:** a paid run of `scripts/copilot_live_eval.py`, and real hotel documents for 7.15.
- **Consequences:** both are excluded by your evaluation constraints (local Ollama only).
- **Recommended:** keep excluded until real documents exist; 7.15 cannot be decided without them.
- **Your action:** approve spending and supply documents, or leave as is.

### 3.9 Room-status history (R3.1)

- **Choice:** decision 6: a `room_blocks` table sharing the bookings' overlap constraint, its
  reasons (maintenance, out of order, owner use), and whether historical occupancy is measured
  against it.
- **Consequences:** changes the occupancy basis of every past figure, so published analytics
  would shift.
- **Recommended:** build it as its own stage once you rule on decision 6.
- **Your action:** rule on decision 6.

### 3.10 FX (R3.4)

- **Choice:** build a rate table and a which-day rule, or keep transaction currency only.
- **Recommended:** keep (decision 14) unless a multi-currency property is a target.
- **Your action:** none unless the audience needs it.

### 3.11 Certificates and backups (R4.1)

- **Needs:** a public domain, a CA account (ACME), and off-site storage with credentials.
- **Recommended:** at the first real deployment.
- **Your action:** provide them then.

### 3.12 Metrics, alerts, SBOM, signing (R4.3)

- **Choice:** where alerts are delivered; which registry images are published to and signed in.
- **Consequences:** images are built and verified in CI but published nowhere, so signing has
  nothing to sign for anyone.
- **Recommended:** decide with R4.1, at the first real deployment.

### 3.13 Product reach (R5)

- **Choice:** roadmap §5's audience: a portfolio or academic project, or a pilot hotel.
- **Recommended:** as a portfolio project, none of R5 is needed.
- **Your action:** name the audience.

## 4. Stage log

| Stage | Tasks | Commit | CI |
|---|---|---|---|
| E1 | this matrix and the decision briefs | (this commit) | — |
