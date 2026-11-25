# v2.0 roadmap — the proposal, its execution matrix and decision briefs

> **Status (2026-11-19).** Written at `2da02f0` (branch `r2.10c3c-adjudication`, CI run
> 44524291097 green) under your standing instruction to work through the v2.0 roadmap's stages
> R0–R6 without routine approval. This file says, for every task there, what is done, what is
> done now, and what waits for a decision only you can make. The rows are updated as each stage
> lands; §3 holds the decision briefs.
>
> **The proposal itself** (written 2026-10-10 as `docs/roadmap-v2.0.md`) is kept in
> [§5](#5-the-proposal-as-written-on-2026-10-10), as it was proposed, with a note on each
> limitation saying where it now stands. It was moved here on 2026-11-25 (R6.1); the old path is
> a redirect.

## 1. Constraints every row is held to

- **The retrieval fingerprint.** Decision F (§12 of
  [retrieval-evaluation-decision.md](retrieval-evaluation-decision.md)) fingerprinted 47 files and
  every migration version when this was written (at `15a29d1`: 55 files and all 23 migrations,
  78 sources): among them `.github/workflows/ci.yml`, `pyproject.toml`, both lock
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
| **R4.3a** metrics exporter | none | partly | F (`main.py`) | where alerts go | — | §3.12; **the local half done in A5**: an internal `GET /metrics` (Prometheus text, bounded labels, no nginx route, 404 to proxied requests); alerting still needs the decision |
| **R4.3b** SBOM, image signing | none | SBOM partly; signing no | CI, Ext | a signing identity and a registry | — | §3.12; **the SBOM half done in A3**: a CycloneDX 1.6 SBOM per built image in CI, validated and kept as an artifact; signing and publication still need the decision |
| **R5** portfolio view, exports, payments, OTA import, sentiment, housekeeping | none | no | — | the audience (roadmap §5) | — | **decision** — §3.13 |
| **R6.1** consolidate the stage-snapshot documents | 36 files under `docs/` | yes | — | — | links resolve; doc tests pass | **consolidated; awaiting CI before it is marked done** (§4, the R6.1 row). In two parts. **A8** (`305f50c`, `b6c6c65`): [docs/README.md](README.md), one entry and one status per document; `scripts/doc_links.py`, every relative link and anchor and every document's reachability, in CI; 8 contradictions resolved and guarded. That alone met the row as an index, not as a merge — the deviation recorded in [project-completion-audit.md](project-completion-audit.md) §4.7 and [project-closure-audit.md](project-closure-audit.md) §4. **The literal merge** (2026-11-25, from `a3d996f`): the six superseded stage snapshots consolidated into three maintained documents — [architecture.md](architecture.md) (the backend at 3B.12, analytics at 3B.10, the statistical layer at 3B.11), the [database README](../database/README.md) (the Stage 2A design and the Stage 2B/2C build) and §5 of this file (the proposal) — and each old path a redirect with a section map, so the code comments and frozen records that cite them still resolve. The 15 frozen records are untouched, and no fingerprinted source changed |
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
| A6 | stale and contradictory documentation corrected (README stage table and AI-capability text, 12 screenshots, frontend README, this log, `ui-redesign.md`); 12 regression tests; mutation stage A6, 8 recorded: 899 in all | `485dd15` | [45046381883](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45046381883): all 7 green, 899/899 |
| A4 | automated accessibility checks: axe-core 4.14.0 in Vitest on every route and state (54 checks, 2 `index.html` checks); genuine heading-order, definition-list and ARIA defects fixed; mutation stage A4, 10 recorded: 909 in all | `4c9a2a0` | [45063664775](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45063664775): all 7 green, 909/909 |
| A7 | the dead placeholder-route scaffolding removed (`PlaceholderPage`, `featureRoutes`, `BUILT_AREAS`, `AREA_DESCRIPTIONS`, `plannedStage`); Not Found styles moved unchanged (identical computed-style digests at 375 and 1440px); the navigation-to-route guarantee is now `navigationRoutes.test.tsx` over the exported `APP_ROUTES`; mutation stage A7, 4 recorded: 913 in all | `5125b5b` | [45150368805](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45150368805): all 7 green, 913/913 |
| A2 | dependency vulnerability scanning in CI: a new job runs `npm audit --omit=dev --audit-level=high` and pip-audit 2.10.1 (hash-locked, its own venv) on every entry of both Python locks and its own, after two positive controls; outages fail after 3 attempts. Found first and fixed: PyJWT 2.13.0 → 2.15.1 (15 advisories, 1 critical, 6 high) and certifi 2026.7.22 → 2026.10.23 (1 critical, dev only); mutation stage A2, 9 recorded: 922 in all. Re-pinned by §12.6 (measured sections unchanged) | `6a91858`, re-pin `fda8718` + `9fec999` | [45390743636](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45390743636): red only on the pin gate, as expected; [45398092718](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45398092718): all 8 green, 922/922 |
| A2 fix, A3, A5, A9 | **A2 correction:** the dependency scan moved to its own workflow, `dependency-audit.yml`, on every push to any branch and on pull requests to `main` (it had run only on `main` and pull requests, so A2's "every push" was not met until now); trigger tests and 4 mutations. **A3:** Syft 1.57.1 CycloneDX 1.6 SBOMs of both builds of both images, schema-validated, checked against the lock and nginx pin, identical across builds, no host path or credential, artifact `sbom-<run>-<attempt>`. **A5:** internal `GET /metrics` (request counts and latencies, route templates only), unreachable through nginx. **A9:** CI job `responsive-audit` runs UI-10's 51 checks in a digest-pinned Playwright Chromium on the seeded stack; the Analytics expenses-table clipping is reported separately, still open. Mutation stages A2 +4, A3 12, A5 12, A9 13: 963 in all. Re-pinned once by §12.6 (measured sections unchanged; A5's metrics modules fingerprinted, 55 sources) | `7858c27`, re-pin `133c5be` + `f4503e4` | [45463427755](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45463427755): red only on the pin gate, as expected; [45476887432](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476887432): all 8 green, 963/963, 51/51; scan [45476878966](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476878966) (`push`) and [45476887470](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476887470) green |
| A8 | R6.1: the documentation index `docs/README.md` (one entry and one status per document); `scripts/doc_links.py`, an offline checker of every relative link, anchor and the index's reach; 8 contradictions resolved (README project structure and V2 heading, `architecture.md`, `database-design.md`, `database-implementation.md`, `ml-design.md`, `tests/README.md`) and guarded by tests; mutation stage A8, 24 recorded: 987 in all. No fingerprinted source changed, so no re-pin | `305f50c`, `b6c6c65` | [45509617278](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45509617278): all 8 green, 987/987, 51/51; scan [45509613321](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45509613321) (`push`) and [45509617354](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45509617354) green |
| R6.1 | R6.1 read literally: the six superseded stage snapshots consolidated into `architecture.md` (§2.1–§2.6, §4.1, §5, §8), the database README (schema design §1–§13 with a "Now" column and a numbering key for the decision register; the implementation and its Stage 2C verification) and §5 of this file; each old path a redirect; the index's statuses are now Current, Frozen record and Redirect; `scripts/doc_links.py` also checks the database README; A8-M17 and M21–M23 re-pinned to where their statements moved; mutation stage R6.1, 12 recorded: 999 in all. No fingerprinted source changed, so no re-pin | `a3d996f` and the commit after it | pending |

## 5. The proposal as written on 2026-10-10

The v2.0 line as it was proposed, before any of it was approved or built. It was written against
commit `be96736` (the `v1.0.0` tag is `4dae239`; `be96736` adds only the README count fix) as a
proposal: nothing in it was implemented, scheduled or approved, and each stage was to need its
own authorization. Work started on 2026-11-19 under your standing instruction; §2 above is what
became of each task.

**Naming.** [v2-roadmap.md](v2-roadmap.md) already used "V2" for Stages 7.2–7.14 (the copilot,
knowledge documents and multi-horizon models), which shipped **inside** `v1.0.0`. To avoid two
meanings of "V2", the proposal called the next major line **v2.0** and numbered its stages
**R0–R6**. That naming was kept.

### 5.1 What the platform did at v1.0.0

Figures verified at `be96736` (CI run 38046070392, all five jobs green) and true **then**, not
now: 65 API paths / 101 operations (96 require authentication), 28 application tables,
migration head `0020_guest_email_rules`, 7,984 backend tests (6 skipped), 1,346 frontend tests,
312 recorded mutations all killed. The current figures are in the root
[README](../README.md), where a test computes each from the code.

| Area | Capability at v1.0.0 |
|---|---|
| **Property management** | Hotels, room types, rooms, amenities; guests (format-checked, case-insensitive unique email); bookings with per-night pricing and a database-enforced non-overlap calendar; the full lifecycle — modify, extend, early departure, check-in/out, cancel, no-show; payments and refunds with a refund cap; a revenue and expense ledger; reviews; availability search; optional `If-Match` on PATCH |
| **Security** | Argon2id passwords; HS256 access tokens, revoked by a password change; login rate limiting; four hotel roles plus a separate platform-administrator capability; hotel-scoped tenant isolation that answers 404 to non-members; an append-only audit trail with archival |
| **Analytics** | Occupancy, ADR, RevPAR, revenue by category, expenses and reviews, computed on demand, per currency, on the hotel's own calendar |
| **Intelligence** | A deterministic statistical layer — day-of-week forecasts with prediction intervals, anomaly detection, demand trend and an attention list — and, separately, one served demand model whose predictions are stored (730 days by target date) with read, accuracy, drift and forecast-performance endpoints |
| **Copilot** | Read-only, seven tools, checked figures, citations from the hotel's documents (PostgreSQL full-text search), per-owner conversations with retention, budgets and a circuit breaker |
| **Frontend** | React + TypeScript single-page app, thirteen navigation areas |
| **Deployment** | TLS-terminated Docker Compose stack whose runtime, backup/restore and image reproducibility are exercised on real containers in CI |

### 5.2 The limitations it named, and where each stands

The left column is the proposal's text, as written on 2026-10-10. The right column is added, as
of 2026-11-25; the matrix in §2 and the briefs in §3 hold the detail.

**The intelligence claims are unproven — the proposal called this the largest gap.**

| Limitation at v1.0.0 | Where it stands |
|---|---|
| The served model's production accuracy, reliability and generalisation are **not established** ([ml-model-card.md](ml-model-card.md)). The demo data is small and synthetic, and the model returns roughly the same figure for any hotel at or below about 40 room nights a night | Unchanged. Waits on R2.1, a data source (§3.7) |
| **No real language model has been evaluated** behind the copilot | Unchanged. Local Ollama pilots only; a paid run is excluded (R2.3, §3.8) |
| Stage 7.15 (vector retrieval) **cannot be assessed**: no real hotel documents exist | Unchanged (R2.3, §3.8) |

**Security gaps before any real deployment.**

| Limitation at v1.0.0 | Where it stands |
|---|---|
| The access token lives in `sessionStorage`, readable by script; there is no `HttpOnly` cookie session and no refresh token ([architecture.md](architecture.md) §7.1) | Done: R1.1, cookie sessions with refresh rotation and CSRF (E8) |
| No password reset, no email delivery, no account deactivation | Open: R1.2 waits on §3.4 |
| Granting platform administration is an out-of-band database write | Done as an audited operator command (R1.3a, E4); an API is §3.5 |
| Rate limits and copilot budgets are kept per worker process, not globally; 429 is not declared in OpenAPI; nothing bounds overall request volume | 429 declared on every limited route (R1.4c, E2) and a request-body cap added (R1.4b); limits are still per process and overall volume is still unbounded — R1.4a, §3.6 |
| `hotels.timezone` accepts any text (unknown zones fall back to UTC) | The API refuses an unknown zone on create and update (R1.5, E2); the column still accepts any text, so a row written earlier still reads as UTC |

**Data-model gaps.**

| Limitation at v1.0.0 | Where it stands |
|---|---|
| No history of room status or of which rooms were active, so past occupancy is measured against **today's** active rooms (it can exceed 100%) and out-of-order rooms cannot be reconstructed | Open: R3.1, decision 6 (§3.9) |
| No FX conversion (approved decision 14): a multi-currency hotel has no single total | Open: R3.4 (§3.10) |
| `daily_hotel_metrics` is reserved and empty, with five open decisions ([database README](../database/README.md#unresolved-before-any-population-job-issue-h9)) | Unchanged; settling it is R2.4, blocked by R2.2 |
| The three global catalogues have no `updated_at`, so same-field edits are last-write-wins | Done: R3.3, migration `0022_catalogue_versions` (E7) |
| `pending` bookings hold no inventory; there is no checkout or hold-expiry flow | Unchanged; R3.2 is blocked until a checkout flow exists (R5) |

**Operations.**

| Limitation at v1.0.0 | Where it stands |
|---|---|
| Certificates are replaced by hand; no off-site or point-in-time backups; no zero-downtime deploy | Unchanged: R4.1 (§3.11); zero-downtime deploy is on no roadmap row |
| The retention purges and audit archival are operator commands; nothing schedules them | An opt-in scheduler runs them (R4.2, E6) |
| No metrics exporter or alerting; no Dependabot or Renovate; no SBOM or image signing; transitive Python dependencies are resolved at build time | An internal `/metrics` (A5); Dependabot configured, inert until `main` (E5, §3.3); SBOMs in CI (A3); hash-locked transitive pins (R0.4b). Alerting and signing are §3.12 |

**Engineering.**

| Limitation at v1.0.0 | Where it stands |
|---|---|
| The backend CI job takes about 45 minutes, and a local full run is split into nine groups | Three parallel shards (R0.3) |
| CI pulls images from Docker Hub unauthenticated, so a Docker Hub rate limit or outage can block it (it did once, during H11) | A mirror for the two Docker jobs (R0.2, E8); the `services:` containers still come from Docker Hub (§3.2) |
| The frontend suite times out on a heavily loaded machine (`--maxWorkers=2` works around it) | `maxWorkers: '50%'` in the Vitest configuration (§2.5, E3) |
| 34 documents under `docs/`, many of them unmaintained stage snapshots; README counts are maintained by hand | An index with a status for every document (A8) and the six stage snapshots consolidated into maintained documents (R6.1); README counts computed by a test (R6.2) |

### 5.3 The stages, as proposed

Size: **S** ≈ a day or two, **M** ≈ a week, **L** = several weeks. **★** marks the stages the
proposal valued most. Each task's outcome is its row in §2.

**R0 — Release hygiene · S · do first**

- Cut `v1.0.1` so the released README carries the corrected counts.
- Make CI independent of unauthenticated Docker Hub pulls: a pull-through mirror (for example
  `mirror.gcr.io`) or authenticated pulls. Digests stay pinned either way.
- Shard the backend CI job so it runs in parallel.
- Dependabot, and transitive Python pinning (a hash-locked file).

**R1 — Production security ★ · M–L · needs decisions**

1. `HttpOnly; Secure; SameSite` cookie sessions with refresh tokens and CSRF protection.
2. An email channel, password reset and account deactivation. **Decision needed:** `users` is
   global across hotels, so who may deactivate an account.
3. A platform-administrator bootstrap and API.
4. A shared rate limiter (Redis or PostgreSQL), request-size limits, and 429 declared in OpenAPI.
5. Validate `hotels.timezone` against IANA zone names (deferred item D2).

**R2 — Evidence for the intelligence claims ★★ · L · the main credibility gap**

1. **A real or realistic data source** — a public hotel-booking dataset loaded through the existing
   declared-observation contract, or a pilot property. Without it, no accuracy question can be
   answered.
2. **Measure before adding models:** run the existing accuracy and drift protocols on that data,
   after aligning the accuracy protocol with declared observation (a protocol version bump).
3. **The live copilot evaluation** (`scripts/copilot_live_eval.py`: paid, on the owner's key), and
   only then decide Stage 7.15 on real documents.
4. **Only if the evidence supports it:** serve the 7/14/28-day horizon models with their own
   capacity rule, and settle `daily_hotel_metrics` — build its job or retire the table — on the
   basis of a measured need.

**R3 — Data-model correctness · M–L · migrations required**

1. **Room-status and active-room history** — a `room_blocks` table (deferred decision 6). It fixes
   historical occupancy, gives `out_of_order_rooms` a source, and unblocks the daily-metrics
   availability decision.
2. **Pending holds with expiry**, ahead of any booking-engine or checkout flow.
3. **`updated_at` and `If-Match` on the global catalogues.**
4. **FX rates** — a rate table and a rule for which day's rate applies. Only worth building for
   multi-currency properties.

**R4 — Operations · M**

- ACME certificate automation; off-site encrypted backups with point-in-time recovery.
- A scheduler for the conversation, invocation and prediction purges and the audit archival.
- A metrics exporter with basic alerts; SBOM and image signing.

**R5 — Product reach · L each · choose by audience**

- A portfolio view across the hotels a user belongs to.
- CSV/PDF report export.
- A payment-provider integration (payments are recorded today, not taken).
- Channel-manager / OTA booking import.
- Review sentiment.
- A housekeeping workflow, on top of R3's room history.

**R6 — Documentation · S–M**

- Consolidate the stage-snapshot documents into a small set of maintained current-state documents.
- Generate the README counts in CI so they cannot go stale.

### 5.4 The recommended order, as proposed

1. **R0**, because every later stage pays the CI cost.
2. **R2 — prove the intelligence.** The engineering is careful and heavily tested, but the AI half
   rests on synthetic data, an unmeasured model and an unevaluated copilot. Real data plus the
   measurement machinery already built would turn "implemented" into "shown to work" — or show,
   honestly, that it does not.
3. **R1 — security fit for a real deployment**, if the platform is to serve a real hotel.
4. **R3 room history** — one schema addition that resolves several documented analytics caveats.
5. **Hold off** on new models and features (more horizons, vector search, agents,
   recommendations) until R2 shows the existing ones are worth extending.

### 5.5 The decisions it asked for before any stage started

- **Audience:** a portfolio or academic project, or a real pilot hotel? This sets how far R1 and
  R5 go. *Still open: §3.13.*
- **Data:** is a real data source available for R2, and is a paid LLM evaluation acceptable?
  *Still open: §3.7 and §3.8; your evaluation constraints exclude paid runs.*
- **Naming:** keep "v2.0 / R-stages" as above, or extend the existing "V2" stage numbering.
  *Kept as v2.0 / R-stages throughout.*
