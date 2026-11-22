# Project completion audit — after the UI redesign

> **Audit of 2026-11-22**, against `52fde7b` on branch `r2.10c3c-adjudication`.
>
> **Read-only.** No application code, migration, test, frozen retrieval artifact or evaluation
> method was changed; this document is the only addition. Every figure below was re-measured
> from the repository, the CI logs or a command run for this audit. None is copied from an
> earlier report unless the report was checked against the code first, and where an earlier
> report disagreed with the code it is corrected here (§7.2).

---

## 1. Executive summary

- **The platform is feature complete for every decided scope.**
  - All 13 navigation areas are built and tested.
  - Every V1 and V2 stage (1 through 7.14) is implemented.
  - The UI redesign is finished (UI-0 to UI-10, plus your two decisions).
  - Every v2.0 roadmap task that needs no business decision is done, with one exception: R6.1, consolidating the documentation.
- **CI is green on HEAD.** Run [44829954281](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/44829954281) passed all 7 jobs:
  - 12,482 backend tests passed and 58 were skipped;
  - 1,353 frontend tests passed;
  - all 891 recorded mutations were killed;
  - ruff, the format check and mypy were clean;
  - the migration chain was checked, the image was verified at runtime, and both builds were reproducible.
- **Overall completion:** 67.6% of all tracked work (48 of 71 items, §8; it was 66.2% at the audit). For work that needs no decision from you, the figure is 85.7% (48 of 56).
- **Remaining autonomous work:** 8 tasks (§4; A1 is done). None is a functional gap. They cover supply-chain security, accessibility automation, metrics, and documentation that is now stale.
- **Remaining user decisions:** 13 open decision briefs (§5). Three of them have a do-nothing default.
- **Readiness:** the platform is demo ready. It is not production ready, and §7 says exactly why.

---

## 2. Repository state

| Check | Result |
|---|---|
| Branch | `r2.10c3c-adjudication` |
| HEAD | `52fde7b` "UI: make the frontend bundle reproducible again" |
| Working tree | clean (0 changed or untracked paths) |
| Local vs remote | identical: `52fde7b02c1c…` on both |
| Latest UI commits present | yes. UI-1 `f7f8c41` through UI-9 `b1d1b0d`, the rename `a08bb95`, the fonts `bf8bcef` and the fix `52fde7b` |
| Latest CI | run 44829954281 on `52fde7b`: **success, all 7 jobs** (§6) |
| PR #1 | **OPEN, not merged**, not a draft. `r2.10c3c-adjudication → main`, head `52fde7b`, 67 commits |
| `main` | **unchanged** at `2a14dc6` ("ci: raise mutation-check timeout to 90 minutes"), locally and on `origin`. No commit since 2026-11-10 |
| Frozen retrieval artifacts | untouched. Since pin 3 (`847adfe`), every commit changed only `frontend/`, so the retrieval fingerprint's inputs are unchanged. HEAD's CI passed the pin gate |

The previous CI run (44818913876, on `bf8bcef`) failed image reproducibility. The cause was a CSS
`composes` diamond introduced in `6ac7d44`. It was found, fixed in `52fde7b` and verified twice:
6 local builds came out byte-identical, and CI passed. Earlier green runs between `6ac7d44` and
`bf8bcef` had passed that job by chance.

---

## 3. Verified completed features

"Verified" means the implementation exists and has tests that ran green in run 44829954281. The
test counts are test functions found by name. Most functions are parametrised, so the 12,482
backend test executions come from far fewer functions.

| Area | Evidence in the code | Tests (files / functions) |
|---|---|---|
| Backend architecture and API | FastAPI app factory; 27 endpoint modules; 68 paths / 104 operations, 96 authenticated (README badge, computed by a test since R6.2); uniform error envelope; request-ID, request-size and security-header middleware | architecture audit, OpenAPI 401/403/422/429 contracts |
| Authentication, authorisation, sessions | Argon2id; bearer login for API clients; `HttpOnly; Secure; SameSite=Strict` cookie sessions with refresh rotation, replay detection and CSRF on every cookie-authenticated write (`deps.py` → `CSRF_FAILED`); `auth_sessions` (migration 0023); `is_active` enforced at login, refresh and session lookup; four hotel roles; tenant isolation answering 404; audited operator grant/revoke of platform administration; rate-limited login | 15 / 396 |
| Database and integrity | PostgreSQL 18.6; 29 application tables; 18 model modules; 23 linear migrations, head `0023_auth_sessions`, applied for real from empty in CI; the booking non-overlap calendar enforced by the database; `If-Match` optimistic concurrency on hotel-scoped resources and the three global catalogues | 4 / 67 dedicated, plus the integration suite (downgrade base → upgrade head) |
| Hotel operations and bookings | hotels, room types, rooms, amenities, guests, members; bookings with nightly pricing and the full lifecycle (modify, extend, early departure, check-in/out, cancel, no-show); availability search | 33 / 946 |
| Financial management | payments and capped refunds; revenue and expense ledger; categories | 6 / 282 |
| Analytics | occupancy, ADR, RevPAR, revenue by category, expenses and reviews, per currency and on the hotel's calendar | 5 / 256 |
| Intelligence and forecasting | deterministic day-of-week forecasts with intervals; anomalies; demand trend; insights with severity; attention list | 9 / 382 |
| Served ML model | `demand_baseline_v1`, served, stored predictions, and accuracy, drift and forecast-performance endpoints; frozen hold-out evaluation (R2.2) and hotel-transfer diagnostic (R2.2b). **Accuracy is not established**, and nowhere is it claimed to be | 21 / 766 |
| Copilot | read-only, seven tools, checked figures, document citations (PostgreSQL full-text search), per-owner conversations, retention, budgets, circuit breaker; local Ollama provider. Off unless an LLM is configured | 17 / 647 |
| Audit, retention, scheduling | append-only audit trail with archival; the three purges; opt-in scheduler (`docker-compose.scheduler.yml`) | 17 / 614 |
| Deployment | four-service Compose stack with TLS at nginx; the CI runtime job checks 36 steps on real containers (TLS, HSTS, forged forwarded headers refused, migration gate, persistence, nginx re-resolution, backup → restore → serve, damaged backup refused, model integrity) | 4 / 93, plus the Docker jobs |
| Frontend | React + TypeScript; 19 pages; 13 navigation areas, all built; self-hosted Inter and Source Serif 4; the UI redesign (tokens, shared Form, DataTable, Rail and VisuallyHidden modules, cards below 1024px, sticky panels); the `/intelligence` page is titled "Forecasting" | 34 files / 1,353 tests |
| Retrieval evaluation | Decision F C.1–C.7 complete; C.4 baseline pinned (third re-pin `05b9ba9` + `a69b44d` + `847adfe`, CI 44761729436 green); 51 fingerprinted sources | pin gate, fingerprint, frozen-artifact tests |

**Frontend behaviour, measured in the browser during UI-10 and again after the font change:** 17
routes × 375, 768 and 1440px gave 51 checks. None scrolls sideways, no element escapes the
viewport, and every page has exactly one `h1`. The two UI decisions are implemented and pushed:
Inter and Source Serif 4 (`bf8bcef`; woff2 subsets under `frontend/src/assets/fonts/` with their OFL
licences, served from `/assets/`) and "Forecasting" (`a08bb95`).

---

## 4. Remaining autonomous engineering tasks — 8 (A1 done)

### 4.1 A1, done (2026-11-22)

The frontend dev-toolchain advisories are fixed. Only `frontend/package.json` and
`frontend/package-lock.json` changed; nothing under `backend/`, `ci.yml` or the retrieval artifacts.

| | Before (`f2b8d50`) | After |
|---|---|---|
| `npm audit` | **6**: 3 high (`vite` ≤6.4.3, `undici` 8.0.0–8.12.1, `source-map-js` 1.0.0–1.2.1), 3 moderate (`vitest`, `@vitest/mocker`, `@vitest/coverage-v8` 5.0.0-beta.1–5.0.2) | **0** |
| `npm audit --omit=dev` | 0 | 0 |

The advisories by package:
- **vite:** `launch-editor` GHSA-587h-7wmj-h5pm (high), GHSA-698v-4r3x-3vcv, GHSA-r825-w8mm-5pxw; and Vite's own GHSA-9jrq-w75r-8gcw (low), GHSA-rq7h-c2jc-7f22.
- **undici:** GHSA-xjj6-f33q-929c, GHSA-4w7w-5xrc-9h26, GHSA-2m5v-pch6-74hx, GHSA-qrxm-fqhp-jwvm (all high), plus four moderate.
- **source-map-js:** GHSA-68fv-2mgg-jv7q.
- **@vitest/mocker:** GHSA-qxg6-89mg-rqv8, GHSA-xp26-3454-mjgp.

**How it was fixed:**
- `npm audit fix` (no `--force`) moved three packages within their existing ranges, changing only the lockfile:
  - `vite` 6.4.3 → 6.4.6;
  - `undici` 8.10.2 → 8.13.1;
  - `source-map-js` 1.2.1 → 1.2.4.
- `npm audit fix` and `npm update` could not move the vitest pair, because `@vitest/coverage-v8` pins an exact `vitest` peer. The pair therefore took a same-major minimum bump: `vitest` and `@vitest/coverage-v8` `^5.0.0` → `^5.0.5`, both 5.0.0 → 5.0.5. That is the latest 5.0.x patch, not 5.1.x.
- The 5.0.5 line also brings its own newer transitive dependencies (dev only):
  - `@vitest/mocker` and `@vitest/spy` 5.0.5;
  - `@vitest/istanbul-lib-*` 1.0.2;
  - `obug` 3.0.0 and `why-is-node-running` 3.2.1;
  - patch and minor bumps of `chai`, `magic-string`, `magicast`, `std-env`, `tinybench`, `tinyexec`, `tinyrainbow` and `ast-v8-to-istanbul`;
  - `siginfo` and `stackback` dropped.
- `esbuild` is unchanged at 0.25.12.

**Local verification** (all on the changed tree):
- `npm run typecheck` is clean.
- Vitest 5.0.5: `Test Files 34 passed (34)`, `Tests 1353 passed (1353)`.
- `npm run build` succeeds.
- Two builds from empty output directories are byte-identical: 61 files, manifest sha256 `5487d54373f9aef7233f1a48d15450910ba4f28f83a9309ec49195b71bca2e33`.
- The dev server (Vite v6.4.6) serves all 17 routes signed in. Each shows its expected `h1`, exactly one `h1`, no error alert and no sideways scroll, and there are no console errors.

CI for the A1 commit is recorded with stage A6.

### 4.2 The remaining tasks

Each task can be done without a decision from you. They are ordered by the requested priorities.
**F** marks a change to a fingerprinted file (`ci.yml`, `main.py`), which needs a re-measure and
re-pin under §12.6. Tasks marked F are batched into one re-pin stage.

| # | Task | Priority | Touches | Depends on | Evidence |
|---|---|---|---|---|---|
| ~~A1~~ | **Done** (§4.1): 6 advisories → 0 | — | — | — | — |
| A2 | **Scan dependencies for vulnerabilities in CI.** `npm audit --omit=dev` and `pip-audit` over both hashed locks; fail on high. Nothing in `ci.yml` scans today | security | F (`ci.yml`) | A1 first, or the new gate fails at once | grep of `ci.yml`: no `audit`, `osv`, `trivy` or `grype` |
| A3 | **Generate an SBOM in CI** (the half of R4.3b that needs no signing identity): one SBOM per image, kept as an artifact | ops | F (`ci.yml`) | batch with A2 | R4.3b is "SBOM partly; signing no" |
| A4 | **Automate accessibility checks.** axe over each page's rendered states in Vitest. No a11y tooling exists (`package.json` has no axe, Playwright or Cypress); UI-10's checks were run by hand | reliability / a11y | frontend only | A1 (toolchain first) | — |
| A5 | **Add a metrics exporter, internal only** (the half of R4.3a that needs no alert destination): request counts and latencies in Prometheus format on the internal network, with no nginx route | ops | F (`main.py`, README counts) | batch with A2/A3 | `grep /metrics backend/app`: none |
| A6 | **Correct the stale and contradictory documentation:** (a) README "Development stages" stops at 7.1–7.4, though 7.5–7.14 shipped; (b) the README says "any LLM/RAG/agent capability" is not implemented, which contradicts the copilot (`backend/app/copilot`, `llm`, `knowledge`) and the README's own Copilot sections; (c) the README screenshots date from 2026-09-22, before the redesign; (d) `frontend/README.md` predates the redesign: no fonts, no shared `components/ui` modules, breakpoints given as 768/1024 when the code uses 600/1024/1280; (e) `roadmap-v2.0-execution.md` pin-3 row still says "(this commit)" with no CI (it is `a69b44d` + `847adfe`, CI 44761729436 green); (f) the UI redesign record lives outside the repository (`AI_Hotel_Platform/ui-baseline/`). Add a stale-claim test as M2 did, so (b) cannot return | documentation (but (b) is a correctness error) | docs; the README is not fingerprinted | — | §7.2 |
| A7 | **Remove the dead placeholder-route scaffolding.** All 13 navigation items are in `BUILT_AREAS`, so `featureRoutes` builds no route; `PlaceholderPage`, `plannedStage` and `AREA_DESCRIPTIONS` are unused. Move the shared state styles out of `PlaceholderPage.module.css` (Not Found uses them) | maintainability | frontend, `splitting.node.test.ts` | — | `AppRouter.tsx:163–176` |
| A8 | **R6.1: consolidate the stage-snapshot documents** (35 under `docs/` + 6 under `docs/deployment/`) into a small set of maintained current-state documents | documentation | docs; tests that pin doc text (`test_current_state_docs.py`, `test_runbook_schema_facts.py`, others) must move with it | A6 (fix the facts before moving them) | the earlier "14 of 29" counted R6.1 as done; it was never performed |
| A9 | **Regression-test the responsive audit.** Codify the UI-10 51-check overflow and one-`h1` audit as an automated browser test against the CI stack | reliability | F (`ci.yml`) | batch with A2/A3/A5; needs a pinned browser image | the audit exists only as a manual browser script |

**Dependencies, so no work is done twice:** A1 → A2 and A1 → A4; A6 → A8. A2, A3, A5 and A9 share
one re-pin. Nothing here waits on you.

---

## 5. Remaining user decisions — 13 open briefs

The briefs are in [roadmap-v2.0-execution.md §3](roadmap-v2.0-execution.md#3-decision-briefs).
None can be inferred safely, because each one commits you to something outside the repository:
money, an account, a legal permission, a public act, or a change to past published figures.

| Brief | Decision | Why it cannot be inferred | Default if you say nothing |
|---|---|---|---|
| §3.1 R0.1 | which release tag, on which commit (recommended: merge PR #1, tag the merge `v1.1.0`) | a tag is public and permanent, and merging PR #1 is reserved to you | no release |
| §3.2 R0.2 | authenticated Docker Hub pulls for the `services:` containers | needs your Docker Hub account and two repository secrets | **keep the mirror (no action)** |
| §3.3 R0.4a | activate Dependabot | GitHub reads it only from `main`; it arrives with PR #1 | inert |
| §3.4 R1.2 | who may deactivate a global user; which email provider | `users` is shared across hotels, so the rule decides cross-tenant power; email needs your provider account and domain | no reset or deactivation flow |
| §3.5 R1.3b | a platform-administrator API, or the operator command only | an API lets a compromised administrator create more | **operator command (no action)** |
| §3.6 R1.4a | when the deployment scales past one worker (then PostgreSQL limiter) | the per-process limiter is correct for one worker; this depends on your deployment | per-process |
| §3.7 R2.1 | a real data source and its permission | no public dataset fits the contract (R2.4); real data needs an agreement | no accuracy claim |
| §3.8 R2.3 | a paid live copilot evaluation and real documents (unlocks Stage 7.15) | spending is excluded by your constraints, and no documents exist | local Ollama only |
| §3.9 R3.1 | decision 6: `room_blocks` and its occupancy rule | changes every past occupancy figure | unchanged |
| §3.10 R3.4 | FX rates | only matters if multi-currency properties are a target | **transaction currency only (no action)** |
| §3.11 R4.1 | domain, ACME CA account, off-site storage | external accounts and credentials | manual certificates, local backups |
| §3.12 R4.3 | where alerts go; which registry images are published and signed in | external destinations and a signing identity | none (A3/A5 build the local halves) |
| §3.13 R5 | the audience (portfolio/academic or pilot hotel) | sets the scope of R1 and R5 | none of R5 |

**Rows blocked by these, not decisions themselves:** R2.2 (waits on R2.1), R2.4 (waits on R2.2)
and R3.2 (waits on a checkout flow, R5).

**Decisions already made (2026-11-20):** Inter + Source Serif 4, and "Forecasting". Both are
implemented and green (§3).

---

## 6. Quality-gate evidence (run 44829954281, `52fde7b`, 2026-11-20 22:33–23:38 UTC)

| Gate | Evidence from the job log |
|---|---|
| Backend tests | shard 1: `10127 passed, 58 skipped`; shard 2: `1096 passed`; shard 3: `1259 passed` → **12,482 passed, 58 skipped, 0 failed**. The skips are evaluation regression tests whose source reports are kept outside the repository by design, plus documented architecture-audit exemptions |
| Integration tests (PostgreSQL) | inside the shards above, on the job's PostgreSQL service; the session fixture runs `downgrade base` then `upgrade head` first |
| Frontend tests | `Test Files 34 passed (34)`, `Tests 1353 passed (1353)` |
| Frontend typecheck and build | typecheck step success; `✓ built in 2.15s` |
| Mutation testing | `891 mutations: 891 killed, 0 survived, 0 not run, 0 errors` |
| Ruff | `All checks passed!`; format: `570 files already formatted` |
| mypy | `Success: no issues found in 545 source files` |
| Dependency locks | "Verify the dependency locks" success |
| Migration chain | declared head `0023_auth_sessions (head)`; `alembic upgrade head` from empty; current revision checked |
| Docker runtime | 36 verification steps, all success (§3) |
| Reproducibility | backend image contents byte-identical across two no-cache builds; frontend bundle byte-identical (the step that failed on `bf8bcef` passed here); every external image pinned by digest |
| Retrieval pin gate | inside shard 1's pytest; artifacts' SHA-256 printed and uploaded |

**Not covered by any gate today:** dependency vulnerability scanning (A2), accessibility (A4),
the responsive audit (A9) and runtime metrics (A5).

---

## 7. Readiness and risks

### 7.1 Three levels

| Level | Verdict | Basis |
|---|---|---|
| **Feature complete** | **Yes, for the decided scope.** | Every built area and every V1/V2 stage is implemented and tested; every decision-free roadmap task is done except R6.1. Stage 7.15 and R5 are out of reach only because they wait on decisions, not because engineering is missing |
| **Demo ready** | **Yes.** | Deterministic demo seed, synthetic data labelled as such, the full stack runs locally and in CI, and the UI is finished at three widths. Caveats: the README screenshots show the old UI and the README misstates the copilot (A6); the copilot can be shown only with a configured model, which under your constraints means local Ollama |
| **Production ready** | **No.** | No password reset, email or deactivation flow (R1.2); manual certificates, local-only backups, no point-in-time recovery (R4.1); no metrics or alerting (R4.3); Dependabot inactive and no vulnerability scanning (R0.4a, A2); intelligence accuracy unproven on real data (R2); open registration by design. Each needs a decision in §5 or a task in §4 |

### 7.2 Security and operational risks

| Risk | Severity | State |
|---|---|---|
| No password reset; deactivation only by setting `is_active` directly (enforced at login, refresh and session lookup, but there is no endpoint) | high for production | decision §3.4 |
| Certificates replaced by hand; backups local, unencrypted, unscheduled; no PITR | high for production | decision §3.11 |
| No dependency vulnerability gate; Dependabot inert off `main` | medium | A2, decision §3.3 |
| Dev-toolchain advisories (3 high) | resolved: 6 → 0 | A1 done (§4.1) |
| No runtime metrics or alerts | medium | A5, decision §3.12 |
| Per-process rate limiter and copilot budgets | low at one worker | decision §3.6 |
| Open registration, and hotel creation makes you its owner | by design for self-hosting | documented in the README |
| Unauthenticated health probes | low (reachable only through nginx, no business data) | documented |
| Unproven model accuracy and unevaluated copilot | credibility, not safety: never claimed | decisions §3.7, §3.8 |
| README contradiction about LLM capability | documentation correctness | A6(b) |

---

## 8. Roadmap completion metrics

### 8.1 The v2.0 roadmap matrix (29 rows), reconciled with the code

| Class | Rows | Count |
|---|---|---|
| 1. Implemented and verified | R0.3, R0.4b, R1.1, R1.3a, R1.4b, R1.4c, R1.5, R3.3, R4.2, R6.2, §2.5 | **11** |
| 2. Implemented, the rest awaiting verification on `main` or a decision | R0.2 (the mirror covers the Docker jobs; `services:` containers still pull from Docker Hub — §3.2), R0.4a (config tested; inert until on `main` — §3.3) | **2** |
| 3. Autonomous engineering | R6.1 (= A8) | **1** |
| 4. Blocked by a decision, or by a row that is | decisions: R0.1, R1.2, R1.3b, R1.4a, R2.1, R2.3, R3.1, R3.4, R4.1, R4.3a, R4.3b, R5; dependent: R2.2, R2.4, R3.2 | **15** |
| 5. Explicitly out of scope | none — R5 becomes out of scope if you confirm the portfolio audience (§3.13) | **0** |

**Reconciliation with the earlier "14 / 29 engineering, 15 decision-dependent":** the 15 is
confirmed exactly. The 14 becomes **13**, because R6.1 was counted as done when only the execution
matrix had been written; the stage-snapshot documents were never consolidated (35 + 6 remain).
R4.3a and R4.3b each have a local half that needs no decision, now tracked as A5 and A3. The rows
themselves stay decision-dependent.

**Per stage:**

| Stage | Rows | Done (classes 1 + 2) | % |
|---|---|---|---|
| R0 release hygiene | 5 | 4 | 80.0 |
| R1 security | 8 | 5 | 62.5 |
| R2 evidence | 4 | 0 | 0.0 |
| R3 data model | 4 | 1 | 25.0 |
| R4 operations | 4 | 1 | 25.0 |
| R5 product reach | 1 | 0 | 0.0 |
| R6 + §2.5 documentation and engineering | 3 | 2 | 66.7 |
| **v2.0 total** | **29** | **13** | **44.8** |

### 8.2 Whole project

**Method:** count every tracked work item once, and treat an item as done only if it is
implemented and its CI is green.

| Workstream | Items | Done | % |
|---|---|---|---|
| Delivered platform stages: the README's 13 stage groups (1 → 7.1–7.4) + V2 stages 7.5–7.14 (10). Stage 7.15 is counted under R2.3 | 23 | 23 | 100.0 |
| UI redesign UI-0 … UI-10 | 11 | 11 | 100.0 |
| v2.0 roadmap matrix | 29 | 13 | 44.8 |
| Audit findings not already on the roadmap (A1–A7, A9; A8 is R6.1) | 8 | 1 (A1) | 12.5 |
| **All** | **71** | **48** | **67.6** |

- **Within the decided scope** (the 15 decision-blocked rows removed): 48 of 56 = **85.7%**.
- **Strict** (R0.2 and R0.4a not counted as done): 46 of 71 = 64.8%.
- At the audit (`f2b8d50`): 47 / 71 = 66.2%, 47 / 56 = 83.9%, strict 45 / 71 = 63.4%. A1 is counted on its local verification (§4.1). Its CI run is recorded with A6, and A1 is uncounted if that run fails.

---

## 9. Recommended implementation sequence

1. ~~**A1**~~ — done (§4.1).
2. **A6** — correct the documentation, above all the README's LLM contradiction, with a stale-claim test.
3. **A4** — axe accessibility checks in Vitest.
4. **A7** — remove the dead placeholder routes.
5. **Re-pin batch: A2 + A3 + A5 + A9**, measured and re-pinned once under §12.6.
6. **A8 / R6.1** — consolidate the documents, last, so the facts move only once.
7. Then whatever your answers to §5 unlock. The first steps toward production are §3.4 (R1.2) and §3.11 (R4.1).
