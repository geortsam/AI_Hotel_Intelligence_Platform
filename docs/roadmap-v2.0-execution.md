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
| **R0.2** CI independent of unauthenticated Docker Hub | partly: the restore image no longer pulls from Docker Hub (`e2abcf6`) | partly | CI, F | — | all 7 jobs green through the mirror; re-pin | **done in E8** for the two jobs that pull in their steps (`docker-runtime`, `image-reproducibility`): the daemon tries `mirror.gcr.io` first and falls back to Docker Hub; digests still pinned. The `services:` containers are pulled before any step runs, so they still come from Docker Hub; authenticated pulls would need a secret — §3.2 |
| **R0.3** shard the backend CI job | **done** (`c1cdf3e`, 3 shards) | — | — | — | — | done |
| **R0.4a** Dependabot | none | config yes; activation no | Ext (on `main`) | merging to `main` | config parses; it is inert off the default branch | **config done in E5** (`.github/dependabot.yml`: pip, npm, Docker, Compose and Actions, weekly, grouped, 3 open at most; a test checks every manifest is covered); activation is the §3.3 decision |
| **R0.4b** transitive Python pinning | **done** (`53fdd0e`, hashed universal locks) | — | — | — | — | done |
| **R1.1** `HttpOnly; Secure; SameSite` cookie session, refresh, CSRF | none; token in `sessionStorage` (architecture §7.1) | yes | Sec, F (`deps.py`) | — | backend and frontend suites, CSRF and revocation tests, mutations; re-pin | **done in E8**: `POST /auth/session`, `/session/refresh`, `/session/end`; `HttpOnly` access and refresh cookies, a CSRF token derived from the session; `auth_sessions` (migration 0023) with rotation and replay detection; a password change ends every session; the browser no longer holds a credential |
| **R1.2** email channel, password reset, deactivation | none | no | Sec, Ext, M | who may deactivate a global `users` row; an email provider | — | **decision** — §3.4 |
| **R1.3a** platform-administrator bootstrap | out-of-band SQL | yes | Sec | — | CLI tests against the scratch database, audit row, mutations | **done in E4**: `python -m app.jobs.platform_admin grant/revoke EMAIL`, audited (`platform_admin.granted`/`.revoked`, migration `0021`); keeps the last administrator unless `--allow-none` |
| **R1.3b** platform-administrator API | none | no | Sec | who may grant and revoke | — | **decision** — §3.5 |
| **R1.4a** shared rate limiter (Redis or PostgreSQL) | per-process | no | Ext or M, F | the backing store; a deployment change | — | **decision** — §3.6 |
| **R1.4b** request-size limits | **done** (`bcdabeb`) | — | — | — | — | done |
| **R1.4c** 429 declared in OpenAPI | declared on the rate-limited routes (login, copilot, conversations, knowledge) | yes | — | — | a test that every limited route declares 429 | **done in E2**: all 11 limited routes declare it; the limited surface is pinned |
| **R1.5** `hotels.timezone` against IANA names | any text accepted, unknown zones fall back to UTC | yes | — (schema only) | — | API tests, mutations | **done in E2** |
| **R2.1** a real or realistic data source | none verified (R2.4 feasibility `5b754f3`) | no | — | a dataset and its licence, or a pilot hotel | — | **decision** — §3.7 |
| **R2.2** accuracy and drift protocols on that data | blocked by R2.1 | no | — | R2.1 | — | blocked |
| **R2.3** live copilot evaluation, then Stage 7.15 | local Ollama pilots only; paid run excluded | no | Ext (paid) | spending; real documents | — | **decision** — §3.8 |
| **R2.4** serve 7/14/28-day models; settle `daily_hotel_metrics` | offline only; table reserved | no | M, F | R2.2's evidence | — | blocked |
| **R3.1** room-status history (`room_blocks`) | open decision 6, deferred | no | M, F | the block model and its constraint | — | **decision** — §3.9 |
| **R3.2** pending holds with expiry | decision 12: when a checkout flow exists | no | M, F | a checkout flow (R5) | — | blocked |
| **R3.3** `updated_at` and `If-Match` on the global catalogues | not present (H6 covers hotel-scoped resources) | yes | M, F | — | migration on CI and scratch, API tests, mutations; re-pin | **done in E7**: migration `0022_catalogue_versions` (an `updated_at` and the shared trigger on `amenities`, `revenue_categories`, `expense_categories`; no `created_at`); the three PATCHes take `If-Match` (412 `STALE_UPDATE`, lock then compare); the platform page sends the loaded version and offers a reload |
| **R3.4** FX rates | decision 14: transaction currency only | no | M | multi-currency properties | — | **decision** — §3.10 |
| **R4.1** ACME certificates; off-site encrypted backups, PITR | manual certificates; local backups | no | Ext | a domain, a CA account, off-site storage | — | **decision** — §3.11 |
| **R4.2** a scheduler for the purges and the audit archival | operator commands | yes | — | — | job tests; Compose stays valid in CI | **done in E6**, opt-in: `python -m app.jobs.scheduler` runs the audit archival (new command `app.jobs.archive_audit_events`) and the three purges every 24 hours, a failed job stopping neither the others nor the schedule; `docker-compose.scheduler.yml` adds it as a service, the base stack unchanged; runbook `docs/deployment/retention-scheduler.md` |
| **R4.3a** metrics exporter | none | partly | F (`main.py`) | where alerts go | — | §3.12 |
| **R4.3b** SBOM, image signing | none | SBOM partly; signing no | CI, Ext | a signing identity and a registry | — | §3.12 |
| **R5** portfolio view, exports, payments, OTA import, sentiment, housekeeping | none | no | — | the audience (roadmap §5) | — | **decision** — §3.13 |
| **R6.1** consolidate the stage-snapshot documents | 36 files under `docs/` | yes | — | — | links resolve; doc tests pass | **not done**: this file indexes the work but consolidates nothing; tracked as A8 in [project-completion-audit.md](project-completion-audit.md) |
| **R6.2** README counts that cannot go stale | hand-maintained, and stale (`7984 backend tests`) | yes | — | — | a test computes each count from the code | **done in E3**: paths, operations, authenticated operations, tables, model modules (was stale: 15, really 17), migrations and head, approved values; test-suite sizes removed and forbidden |
| §2.5 frontend suite on a loaded machine | `--maxWorkers=2` by hand | yes | — | — | Vitest config, frontend CI job | **done in E3**: `maxWorkers: '50%'` in `vitest.config.ts` |

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
| E1 | this matrix and the decision briefs | `6cc1d62` | see E2's row |
| E2 | R1.5 IANA time zones on create and update; R1.4c every limited route declares 429 (pinned); 8 mutations (R1.4-M1–M2, R1.5-M1–M6): 803 in all | `1210746` | [44590050067](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44590050067): red on shard 1 (`localtime` accepted on Linux), fixed in E3 |
| E3 | R6.2 README counts computed from the code, test-suite sizes removed; §2.5 Vitest worker cap; 9 mutations (R6.2-M1–M9); R1.5 fix: Debian's `localtime`, `posixrules` and `Factory` are not places and are refused (R1.5-M7), found by E2's CI on Linux: 813 in all | `73507c9` | [44613868962](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44613868962): all 7 green, 813/813 |
| E5 | R0.4a Dependabot configuration, inert until it reaches `main`; 8 mutations (R0.4a-M1–M8): 821 in all | `df3a3cf` | see E6's row |
| E6 | R4.2 opt-in retention scheduler and the audit archival command; 14 mutations (R4.2-M1–M14, 4 on PostgreSQL); the README tests badge no longer states suite sizes (R6.2-M10); 836 in all | `b9b121a` | [44652079979](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44652079979): all 7 green, 836/836 (E5 and E6 together) |
| E4 | R1.3a audited platform-administrator grant and revocation (operator command, no API), migration `0021_audit_platform_grants`, head and count pins moved, the first-run runbook rewritten; ci.yml no longer restates mutation counts; 15 mutations (R1.3a-M1–M15, 13 on PostgreSQL): 851 in all. Fingerprinted: `enums.py`, `ci.yml`, the migrations directory — the C.4 report is re-pinned after the batch | `475c39f` | see E7's row |
| E7 | R3.3 `If-Match` on the three global catalogues: migration `0022_catalogue_versions`, the PATCHes conditional, the platform page sends the version and offers a reload; head and count pins moved; 16 mutations (R3.3-M1–M16, 9 on PostgreSQL, 5 Vitest): 867 in all. Fingerprinted: `ci.yml`, the migrations directory — re-pinned together with E4's changes, from this commit's CI run | `ce64720` | [44678376755](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44678376755): red only on the retrieval pin gate, as expected; 867/867 killed (E4's run [44665059279](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44665059279) likewise, 851/851) |
| pin | the C.4 report re-pinned by §12.6 from run 44678376755's artifact, verified offline at `ce64720`; the measured sections are identical to the first pin, only the fingerprinted inputs moved; `test_expectations_v3_v2.py` admits the new digests | `82a57cd`, `94c3201` | [44685207749](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44685207749): all 7 green, 867/867 |
| E8 | R1.1 cookie sessions (`POST /auth/session`, `/session/refresh`, `/session/end`; `HttpOnly` access and refresh cookies; CSRF on every cookie-authenticated write; `auth_sessions`, migration `0023`, with rotation and replay detection; the browser holds no credential) and R0.2 the Docker Hub mirror in the two Docker jobs; 22 mutations (R1.1-M1–M20, R0.2-M1–M2): 889 in all. Four new modules recorded in the retrieval fingerprint's exclusions under existing §12.3 reasons, for your review. Fingerprinted: `ci.yml`, `main.py`, `deps.py`, `models/__init__.py`, the migrations directory, the C.4 harness's fingerprint module -- re-pinned from this commit's CI run | `27fb18c` | [44703063590](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44703063590): red on the pin gate, as expected, and one mutation survived (G1-M4: the CSRF 403 now filled a missing platform 403); the G1 test is strengthened in the next row |
| pin 2 | the C.4 report re-pinned by §12.6 from run 44703063590's artifact, verified offline at `27fb18c`, measured sections identical; the digests admitted; `test_every_route_level_gate_declares_403` now requires a gated 403 to name its gate, not merely exist | `c5de382`, `132012c` | [44712167689](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44712167689): all 7 green, 889/889 |
| E9 | the four cookie-session modules moved from the retrieval fingerprint's exclusions to its sources (`AUTH_SOURCES`; 51 sources, 171 exclusions), tests that all four are fingerprinted and none excluded; 2 mutations (R1.1-M21, M22): 891 in all. Fingerprinted: `retrieval_fingerprint.py` and the four -- re-pinned from this commit's CI run | `649d1bb` | [44745383909](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44745383909): red only on the pin gate (`inputs changed: sources`), as expected; 891/891 |
| pin 3 | the C.4 report re-pinned by §12.6 from run 44745383909's artifact, verified offline at `649d1bb`; only `sources` changed, by the four modules; the digests admitted (`a69b44d`), and a comment line ruff refused rewrapped (`847adfe`) | `05b9ba9`, `a69b44d`, `847adfe` | [44752923061](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44752923061) on `a69b44d`: red on lint only (E501); [44761729436](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44761729436) on `847adfe`: all 7 green, 891/891 |
| UI-1–UI-10 | the UI redesign ("Midnight concierge"): tokens (UI-1), shell and one `h1` per page (UI-2), shared form anatomy (UI-3), shared states and `Button loading` (UI-4), data display -- cards below 1024px, the shared DataTable module, right-aligned numbers, the 2 x 2 period selector, the shell column, one select chevron (UI-5), dashboards (UI-6), forecasting and copilot (UI-7), detail pages (UI-8), sign-in, not-found and reviews (UI-9), and a 51-check audit at 375/768/1440px (UI-10); then your two decisions, self-hosted Inter and Source Serif 4 (`bf8bcef`) and the page titled Forecasting (`a08bb95`); frontend only, no re-pin | `f7f8c41` … `b1d1b0d`, `a08bb95`, `bf8bcef` | [44771121496](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44771121496) (`f7f8c41`), [44792294569](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44792294569) (`80270a5`), [44800234002](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44800234002) (`3b31953`), [44809155529](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44809155529) (`b1d1b0d`): all green, 891/891; [44818913876](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44818913876) (`bf8bcef`): red on image reproducibility -- see the next row |
| UI fix | the frontend bundle reproducible again: since `6ac7d44` a list module reached `VisuallyHidden` by two `composes` paths, so Vite placed its rule differently from build to build; the DataTable caption now states the rule itself (one path per shared module). Local builds: 4 of 4 distinct at `6ac7d44`, 1 of 6 after the fix | `52fde7b` | [44829954281](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44829954281): all 7 green, 891/891 |
| audit | [project-completion-audit.md](project-completion-audit.md): read-only audit of the whole repository; this matrix reconciled with the code (13 rows done, not 14: R6.1 had not been performed), 9 autonomous tasks (A1–A9), 13 open decision briefs | `f2b8d50` | [45030511072](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45030511072): all 7 green, 891/891 |
| A1 | frontend dev-toolchain advisories, 6 → 0: `npm audit fix` (vite 6.4.6, undici 8.13.1, source-map-js 1.2.4) and vitest / `@vitest/coverage-v8` `^5.0.0` → `^5.0.5`; the shipped runtime had none | `044df52` | [45035486687](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45035486687): all 7 green, 891/891; 68 served files byte-identical |
