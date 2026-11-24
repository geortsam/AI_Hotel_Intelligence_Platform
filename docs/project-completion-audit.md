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
- **Overall completion:** 77.5% of all tracked work (55 of 71 items, §8; 66.2% at the audit, 67.6% after A1, 69.0% after A6, 70.4% after A4, 71.8% after A7, 73.2% after A2). For work that needs no decision from you, the figure is 98.2% (55 of 56).
- **Remaining autonomous work:** 1 task (§4; A1, A6, A4, A7, A2, A3, A5 and A9 are done, A2 with the trigger correction of §4.6). It is not a functional gap: documentation consolidation (A8 = R6.1).
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

## 4. Remaining autonomous engineering tasks — 1 (A1, A6, A4, A7, A2, A3, A5 and A9 done)

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

**CI:** [45035486687](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45035486687) on `044df52`, all 7 jobs green, `891 mutations: 891 killed`, and all 68 served frontend files byte-identical across two no-cache image builds.

### 4.2 A6, done (2026-11-22)

The stale and contradictory documentation is corrected. Only documentation, screenshots and tests
changed; nothing under `backend/app`, no migration, no `ci.yml`, no frozen artifact.

- **README.md:**
  - The stage table now runs past 7.1–7.4. It adds 7.5–7.14, 7.15 as conditional and untriggered, the done R-stage work, UI-1–UI-10 and A1, each checked against the code.
  - The false "any LLM/RAG/agent capability" sentence is replaced by what exists: seven read-only tools named as the registry names them, PostgreSQL full-text search, off by default, Ollama or Anthropic as configured, no autonomous action, and the one declared idempotent write. The not-implemented list now names vector search, agents and writing tools explicitly.
  - `app/llm/` is no longer said to hold a single provider adapter.
  - The screenshot intro states the dataset actually shown, and two image captions and one alt text were corrected.
  - The mutation count is 899.
- **Screenshots:** all 12 under `docs/screenshots/` were replaced.
  - Same file names and sizes: 1440×900, and Analytics 1440×1760.
  - Captured from the redesigned UI with headless Edge over the DevTools protocol, signed in as the seeded demo owner.
  - The data is synthetic (`scripts/seed_demo.py --reference-date today`: 2 properties, 2,483 bookings, 25 rooms, 2,181 guests, 669 reviews; `@example.com` addresses). No credential, token or secret appears in any image; each was reviewed.
- **frontend/README.md:**
  - Covers the self-hosted Inter and Source Serif 4 (`fonts.css`, `assets/fonts`, OFL licences); the shared `components/ui` modules with measured usage (Form 8, DataTable 12, VisuallyHidden 12, Rail 3 components); and the one-`composes`-path rule.
  - Gives the breakpoints as the code has them: 600 / 768 / 1024 / 1280 documented in `tokens.css`, cards below 1024px, and the remaining one-off widths named as open work. The stage brief's "600 / 1024 / 1280" would have omitted the 768px rules the code still uses.
  - Also corrected: the directory tree (features, lib, session, fonts), the `services/` description ("no endpoints yet") and the cookie-session behaviour of the API client.
- **docs/roadmap-v2.0-execution.md:**
  - The pin-3 row is completed (`05b9ba9`, `a69b44d`, `847adfe`; 44752923061 red on E501 only, 44761729436 green).
  - Rows added for UI-1–UI-10, the reproducibility fix, the audit and A1, with exact hashes and run IDs.
  - The R6.1 row now says it was not done.
- **docs/ui-redesign.md (new):** direction, tokens, typography, shared modules, breakpoints, accessibility and reduced motion, every stage with its commits, and the `52fde7b` fix.
- **Regression tests** (`tests/backend/test_capability_and_breakpoint_docs.py`, 12 tests):
  - Both stale claims are checked for absence, with positive controls for the original wrapping and a re-wrapped form.
  - The README's tool list must equal the registry, and its stated count must match.
  - Its declared write must be the only tool with a side effect.
  - No mapped column may be a vector type, and knowledge search must be `TSVECTOR`.
  - Every breakpoint row must be a media query the stylesheets use and must be documented in `tokens.css`.
  - The stated card switch must be `useIsCompact`'s default.
- **Mutations:** stage A6, 8 recorded (A6-M1–M8): 8 killed, 0 survived. The inventory holds 899 (828 pytest, 71 Vitest, 166 needing a database), and `tests/README.md` is updated to match. The first run had A6-M8 survive: the original-wrapping control passed with or without whitespace normalisation, because both claims sat on one line. The re-wrap test was added because of it.
- **Local verification:**
  - ruff `All checks passed!`; format `573 files already formatted`; mypy `Success: no issues found in 546 source files`.
  - The affected pytest files: 1,896 passed. Every test reading the changed docs: 234 passed.
  - Vitest: 34 files, 1,353 tests passed.

**Found during A6, not yet counted in the 71-item framework:** on Analytics at 1440px the
"Expenses by category" table is 542px wide in a 475px box. Its "Fixed cost" badges widen it, so
the last column (Entries) is cut, with a scrollbar inside the card. The page itself does not
scroll sideways, which is why the UI-10 audit did not flag it. It is a small UI fix, a candidate
for the next frontend stage.

**CI:** [45046381883](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45046381883) on `485dd15`, all 7 jobs green: 12,510 backend tests passed (shards 10,155 + 1,096 + 1,259; 58 skipped), `899 mutations: 899 killed`, 9,548 backend and 68 frontend files byte-identical across two no-cache builds, Docker runtime verification passed.

### 4.3 A4, done (2026-11-23)

**Dependency:** `axe-core` **4.14.0**, a dev dependency pinned exactly (`"axe-core": "4.14.0"`).
- Source: `https://registry.npmjs.org/axe-core/-/axe-core-4.14.0.tgz`, integrity `sha512-9WTZxEjsZ7b13TH8JPmbV2z8CHbl80/2hm3XPEG4JgNdQLK81IBRXmSxHfMAOkSqQeRxT/0dwNDz2GOm3zzpcQ==`.
- MPL-2.0, 31 files, 3,430,531 bytes unpacked, no dependencies.
- Used directly. `vitest-axe` (0.1.0, January 2025, six dependencies) and `jest-axe` (Jest matchers, an older axe) were considered and not used.
- 4.14.0 rather than the six-day-old 4.14.1.
- `npm audit` and `npm audit --omit=dev` both report 0. axe-core is in no file of the production bundle.

**Helper:** `frontend/src/test/axe.ts`.
- **`color-contrast` is off**, because jsdom lays nothing out and `css: false` applies no stylesheet. Contrast is measured in a real browser and recorded beside the tokens.
- **`html-has-lang` and `document-title` are off for page renders.** `src/index.node.test.ts` asserts that `index.html` has both.
- **`region` and `landmark-one-main` are off for page renders and on for the shell**, which owns the landmarks.
- **One project rule:** every `<table>` must have a caption or an `aria-label`. axe has no such rule, and every table here is named.
- `src/test/harness.tsx` gains `stubCompactViewport()`.

**Coverage:** 56 new tests: 54 axe checks in 16 existing test files, plus 2 `index.html` checks in a new file, `src/index.node.test.ts`.

| Page | States checked |
|---|---|
| Dashboard | loaded (KPI tiles, trend charts), loading, empty period, failure |
| Bookings | table, compact cards, loading, empty, failure |
| Booking detail | loaded (operations, stay, rooms, money, payments) |
| Guests | table, compact cards, open create form, loading, empty, failure |
| Guest detail | loaded |
| Rooms | table, compact cards, open create form, loading, empty, failure |
| Room detail | loaded |
| Availability | before search, an answer, empty answer, failure |
| Financials | revenue journal and totals, compact cards, expense journal, open posting form, loading, empty, failure |
| Analytics | loaded (KPIs, charts, tables, demand estimate), loading, failure |
| Forecast performance | loading, three measured panels |
| Forecasting (`/intelligence`) | loaded, revenue tab, compact anomaly cards, loading |
| Reviews | summary and list, open form, loading, empty, failure |
| Copilot | question form, an answer with citations and tool calls, conversation mode, switched off |
| Property | record and room-type table, compact cards, open property and room-type forms, loading, empty, failure |
| Administration | account, password form and member table, compact cards, open add-member form, loading, empty page, failure |
| Platform | catalogue table, other tabs, open form, audit trail, compact cards, loading, failure |
| Sign-in | page, refused sign-in with its alert, empty-submit field errors |
| Not found | inside the real shell, landmark rules on |
| Shell | navigation drawer open, landmark rules on |

**Violations found and fixed** (all genuine; no rule was silenced):

| Rule (impact) | Where | Impact | Fix |
|---|---|---|---|
| `heading-order` (moderate) | booking detail | section cards' `<h3>` directly under the `<h1>`, rooms `<h4>`: the outline skipped a level for heading navigation | `CardHeader` gains an optional `level` (default 3); the page's 10 cards pass `level={2}`; room titles `<h3>` |
| `heading-order` | Bookings, Guests, Analytics, Dashboard: empty, failure and no-hotel messages | `StateMessage`'s `<h3>` directly under the `<h1>` | `StateMessage` gains `level` (default 3); the 16 page-level messages pass `level={2}` |
| `heading-order` | Guests, compact | guest card names `<h3>` under the `<h1>` | `<h2>` |
| `heading-order` | Financials | totals and journal titles `<h3>` under the `<h1>` | `<h2>` |
| `heading-order` | Dashboard | section titles and board cards `<h3>` under the `<h1>` | `<h2>` (2 titles, 5 cards) |
| `heading-order` | Copilot answer | citation and tool-call list titles `<h4>` under the thread's `<h2>` | `<h3>` |
| `dlitem` (serious) | Analytics | KPI `<dt>`/`<dd>` with no `<dl>`: term and value not paired for assistive technology | KPI grid is a `<dl>` (`margin: 0`) |
| `definition-list` (serious) | Dashboard, loading included | `<p>` caption and delta inside the tiles' `<dl>` groups | caption and delta are further `<dd>`s of the same term |
| `aria-prohibited-attr` (serious) | Reviews | `aria-label` on a plain `<p>` rating, with the visible score `aria-hidden`: some screen readers announced no rating | `role="img"` on the rating |

Every changed heading keeps its computed size, because its class sets it. Measured on the dev server:
- dashboard section and card titles 16px/600; booking detail card titles 16px/600; room titles 14px/600; Financials block titles 14px/500;
- 0 skipped heading levels on all 17 routes.

**Mutations:** stage A4, 10 recorded (A4-M1–M10, Vitest killers):
- the targets: a form label (guest form, sign-in), the rating's alternative text, a table caption, the navigation button's name, the `<main>` landmark, booking detail's heading level, `StateMessage`'s level, a KPI `<dd>`, and the rating's role;
- result: **10 killed, 0 survived**;
- the inventory now holds 909 (828 pytest, 81 Vitest, 166 needing a database).

**Local verification:**
- `npm audit` 0 and `--omit=dev` 0; typecheck clean.
- Vitest: `Test Files 35 passed (35)`, `Tests 1409 passed (1409)`, the 1,353 before plus 56, none regressed.
- Build passes; two clean builds byte-identical (61 files, manifest sha256 `66c12fc6d5e907ee12164e375c865549a90b57f7ae4e63f1eff6def8d2900a3c`).
- ruff, format and mypy clean (546 files); the affected pytest files: 2,092 passed.
- Dev server: all 17 routes signed in, each with its `h1`.

**CI:** [45063664775](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45063664775) on `4c9a2a0`, all 7 jobs green:
- 12,530 backend tests passed (shards 10,175 + 1,096 + 1,259; 58 skipped).
- Frontend: `Test Files 35 passed (35)`, `Tests 1409 passed (1409)`.
- `909 mutations: 909 killed, 0 survived, 0 not run, 0 errors`.
- 9,548 backend files and 68 served frontend files byte-identical across two no-cache builds.
- Docker runtime verification passed.

A4 is counted complete from this run.

### 4.4 A7, done (2026-11-23)

**Established first, from the code:**
- All 13 `NAV_ITEMS` entries were in `BUILT_AREAS`, so `featureRoutes` (`NAV_ITEMS` minus `BUILT_AREAS`) was an empty list.
- No placeholder route had been produced since Stage 7.2.
- `AREA_DESCRIPTIONS` was empty.
- `plannedStage` was read only by the placeholder.

**Removed:**
- `frontend/src/pages/PlaceholderPage.tsx`;
- from `AppRouter.tsx`: `AREA_DESCRIPTIONS`, `BUILT_AREAS`, `featureRoutes` and its spread into the route tree, plus the now-unused imports;
- the `plannedStage` field from `NavItem`, and its 13 values in `navigation.ts`;
- the placeholder's entries in `splitting.node.test.ts`'s eager-import lists.

**Kept exactly:**
- Every real route, the route order and the `*` fallback.
- The Not Found styles. `PlaceholderPage.module.css` is renamed to `NotFoundPage.module.css` (a git rename, contents unchanged). Every class in it was already the Not Found page's.

**Guarantee replaced, not dropped.** The scaffolding existed so a sidebar entry could never point at nothing.
- The route tree is now exported as `APP_ROUTES`, and the router is built from it unchanged.
- `src/router/navigationRoutes.test.tsx` (20 tests) resolves every navigation item, the three detail pages and sign-in against it with React Router's `matchRoutes`. Each must land on a route of its own, never the `*` fallback.

**Computed-style digests of `/nowhere`** (every element in the document, before and after):

| Width | Before | After | Differing elements |
|---|---|---|---|
| 375px | `179e140f9c9299111fcff1e2cdc9b4c6df7dbb5291dc788b118f10f00e18e260` (174 elements) | identical | 0 |
| 1440px | `ebf4753008a835f1bce9ac01701fa52c116b6f7463a3348f58a4c774c4094488` (174 elements) | identical | 0 |

The heading's class hash changed to `_headline_s8jox_…`, which shows the renamed stylesheet was the one being served.

**Mutations:** stage A7, 4 recorded, Vitest killers:
- the Reviews route dropped;
- the dashboard index route dropped;
- Platform declared at a path its entry does not link to;
- the room-detail route dropped.

Result: 4 killed, 0 survived. The inventory holds 913 (828 pytest, 85 Vitest, 166 needing a database).

The harness matches a Vitest killer by its literal title. The killers are therefore two fixed-title tests ("every sidebar entry…", "every detail page…"), and the per-entry `it.each` tests remain for diagnosis.

**Local verification:**
- typecheck clean;
- Vitest `Test Files 36 passed (36)`, `Tests 1427 passed (1427)`: 1,409 + 20 new − 2 removed list entries, no other test changed;
- build passes; two clean builds byte-identical (61 files, manifest sha256 `457e3e6e2b9a55aa1a9193456d65cf26697d838b9b0b6965ae5d09395e7e0a37`); no placeholder text in the bundle;
- `npm audit` 0 and `--omit=dev` 0;
- dev server: all 17 routes render their own `h1`, none falls through to "Page not found", 0 skipped heading levels;
- ruff, format and mypy clean (546 files); harness and doc tests 2,100 passed.

**CI:** [45150368805](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45150368805) on `5125b5b` (implementation), all 7 jobs green:
- 12,538 backend tests passed (shards 10,183 + 1,096 + 1,259; 58 skipped).
- Frontend: `Test Files 36 passed (36)`, `Tests 1427 passed (1427)`.
- `913 mutations: 913 killed, 0 survived, 0 not run, 0 errors`.
- 9,548 backend files and 68 served frontend files byte-identical across two no-cache builds.
- Docker runtime verification passed.

A7 is counted complete from this run. This record is the documentation commit that follows it.

### 4.5 A2, done (2026-11-24)

**Measured before the gate.** pip-audit 2.10.1 over both locks; `npm audit` with and without dev dependencies.

| Package | Version | Where | Advisories | Fixed in |
|---|---|---|---|---|
| PyJWT | 2.13.0 | **shipped** (image lock and dev lock) | 15: 1 critical (GHSA-ffc3-869f-jxw9), 6 high (GHSA-p4g4-x82p-q773, GHSA-9v7f-9g4p-ffgj, GHSA-9j54-fg26-wv3r, GHSA-w2cx-738m-mc7w, GHSA-r6x4-923q-g947, GHSA-pxh4-856f-4h89), 8 moderate | 2.14.0 / 2.15.0; GHSA-gvp8-978c-rx2q lists 2.13.0 as its last affected version |
| certifi | 2026.7.22 | development only (via httpx) | 1 critical: GHSA-6q48-6c7w-9gw2, compromised emSign roots | 2026.10.18 |
| npm (frontend) | — | shipped and dev | 0 | — |

**Fixed before the gate landed:**
- **PyJWT → 2.15.1.** A direct pin in `backend/requirements.txt`, released before the locks' existing `--exclude-newer` cutoff.
- **certifi → 2026.10.23.** Released after the cutoff, so it moves through one per-package `--exclude-newer-package` exception. The global timestamp, and with it every other package, is unchanged.

Both locks were regenerated with uv 0.12.24, and nothing else in either lock moved. The 247 auth unit tests and about 300 auth integration tests (on the disposable `c4_scratch_test` database) pass on PyJWT 2.15.1. After the fixes there are no findings: 40 of 40, 57 of 57 and 28 of 28 entries audited, and npm 0.

**The scanner.** pip-audit 2.10.1:
- Published by the Python Packaging Authority (github.com/pypa/pip-audit), Apache-2.0, requires Python ≥ 3.10.
- Wheel 62,023 bytes, sha256 `99ef3f60…4611a`.
- Locked with its dependencies in `scripts/dependency_audit_lock.txt`: 28 packages, about 47 MB installed, every entry hashed.
- Installed with `--no-deps --require-hashes` into a venv of its own, so it never shares an environment with what it scans.

**The gate.** CI job "Dependency vulnerability scan" (`dependency-audit`), as first landed in `ci.yml`: on every run of that workflow (not every push; corrected in §4.6), with no `if:`, no `needs:`, no `env:`, no services and no secret. The checkout does not persist its token. In order:
1. Install pip-audit from its lock.
2. **Positive controls:** audit `tests/fixtures/dependency_audit/pyjwt-2.13.0.lock`, which must report GHSA-ffc3-869f-jxw9, and an npm fixture pinning lodash 4.17.20, which must report GHSA-35jh-r3h4-6jhm.
3. `npm audit --omit=dev --audit-level=high` on `frontend/`.
4. pip-audit `--require-hashes --disable-pip --strict` on `backend/requirements-lock.txt`, `backend/requirements-dev-lock.txt` and the scanner's own lock.

**Policy:**
- **Frontend:** high and critical fail.
- **Backend:** pip-audit has no severity option, so any known advisory fails, a superset of high and critical.
- **No suppressions:** no advisory is ignored or suppressed.

**What `scripts/dependency_audit.py` adds:**
- **Outages fail.** Both tools exit 1 both for findings and for an unreachable service, so an attempt counts only if it produced a real report. There are three attempts (10 s and 30 s apart), then exit 2.
- **Every entry is audited.** pip-audit evaluates environment markers for the running interpreter and silently drops the rest: on the Linux runner, `tzdata` and `colorama`. Those are audited in a second pass from a marker-stripped copy, hashes kept, and an entry audited by neither pass fails the job.
- **Fresh data.** Each run gets an empty advisory cache, and `setup-python` has no pip cache.
- **Two exit codes.** It cross-checks npm's exit code against the report's counts.

**Checked locally against the real services:**
- the lock fixture as an audited lock gives exit 1 with 15 findings;
- an unreachable proxy or registry gives exit 2 after 3 attempts, for both pip-audit and npm;
- an unhashed entry is refused;
- `--audit-level=critical` lets the lodash fixture's high advisory through, so the threshold matters.

**Tests and mutations:**
- `tests/backend/test_ci_dependency_audit.py`: 28 tests. They cover:
  - the workflow: the job and step order, that it runs on every push, no secret, the hashed install, the npm scan, both locks, and the controls first;
  - the pins and fixtures;
  - the wrapper's decisions, checked offline with a fake scanner.
- Mutation stage A2, 9 recorded, all killed:
  1. the scanning steps removed;
  2. the threshold lowered to critical;
  3. the dev lock left out;
  4. the frontend scan removed;
  5. high dropped from the failing severities;
  6. `--require-hashes` dropped;
  7. an outage passing once the retries are spent;
  8. a skipped entry tolerated;
  9. the positive control removed.

  The inventory holds 922 (837 pytest, 85 Vitest, 166 needing a database).

**Re-pin (Decision F §12.6).** `ci.yml` and both locks are fingerprinted.
1. Run [45390743636](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45390743636) on `6a91858` was red only on the pin gate: `inputs changed: sources, python`. Its other 7 jobs passed, with 922/922 mutations killed and the scan job green.
2. Its artifact `retrieval-eval-45390743636-1` verified offline against the job log's digests: files, digests, fingerprint, components, tree and repository groups.
3. The `report`, `results`, `signoff` and `manifest` sections are identical to the previous pin. Only `inputs.sources` (`ci.yml` and the two locks), `inputs.python.installed_set_sha256`, the `sources` and `python` components and the fingerprint moved.
4. It was pinned, bytes unchanged, in `fda8718`:
   - report `567ace340acb33370cf75bc6a0ad2696ad98554f5ad1183bde5099a951c09116`;
   - environment `28adf2c2da00abf25a8c5fbfa765c80d920d602bfe6249129c72b07297c5891a`;
   - fingerprint `3213ac9f2cb3554c52e8f01a778df1a5270d9b59edd8f3d77245c02099ab8f36`.
5. `9fec999` admitted the digests and recorded the re-pin in the decision record.

**CI:** [45398092718](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45398092718) on `9fec999`, all 8 jobs green (the new scan job makes a run 8 jobs, not 7):
- 12,584 backend tests passed (shards 10,229 + 1,096 + 1,259; 58 skipped), with the pin gate passing against the new pin;
- Frontend: `Test Files 36 passed (36)`, `Tests 1427 passed (1427)`;
- `922 mutations: 922 killed, 0 survived, 0 not run, 0 errors`;
- 9,548 backend files and 68 served frontend files byte-identical across two no-cache builds;
- Docker runtime verification passed;
- the scan job: both controls reported, npm 0, and 40, 57 and 28 entries audited with 0 advisories.

A2 was counted complete from this run, but its trigger fell short of "every push" (see the limitations below); that was corrected and verified in §4.6. The plan in §9 batched A2 with A3, A5 and A9. The A2 stage prompt asked for its own re-pin, so A3, A5 and A9 remained the batch.

**Limitations:**
- The gate needs the public advisory services (the PyPI JSON API and the npm registry's bulk advisory endpoint). An outage fails the run rather than passing it.
- pip-audit sees what PyPI's vulnerability data holds, so an advisory that has not reached it yet cannot be caught.
- **As first landed, the scan did not run on every push.** It was a job in `ci.yml`, whose `push` trigger is restricted to `main`, so it ran on a push to `main` and on pull requests to `main`, but a push to any other branch without a pull request was never scanned. The test that claimed "runs on every push" checked only that the job had no `if:`, not the workflow's triggers. **A2's acceptance criterion was therefore not fully met by `6a91858`.** It is corrected, and the correction verified, in §4.6.

### 4.6 A2 trigger correction, A3, A5 and A9, done (2026-11-24)

One batch, one re-pin. Implementation `7858c27`; re-pin `133c5be` (the report and environment bytes alone) and `f4503e4` (the digests admitted, the decision-record row).

**A2 correction: the scan now runs on every push to any branch.**
- The job moved, unchanged in what it runs, from `ci.yml` to a workflow of its own, `.github/workflows/dependency-audit.yml`, triggered by `push:` with no branch filter and by `pull_request` to `main`. `main` and pull-request behaviour are preserved; `ci.yml` itself is untouched in its triggers.
- Its concurrency group includes the event name, so a branch's push run and its pull-request run never cancel each other; `permissions: contents: read`; the checkout does not persist its token.
- Nothing in the scan was weakened: the same hash-locked pip-audit, the same positive controls first, `npm audit --omit=dev --audit-level=high`, every lock entry, outages failing. The Python-lock list gains A3's `scripts/sbom_lock.txt`.
- `tests/backend/test_ci_dependency_audit.py` now reads the workflow's **triggers**: a push to an arbitrary non-`main` branch (`feature/x`, `r2.10c3c-adjudication`) must start it, a pull request to `main` must start it, and `ci.yml` must no longer hold the job. Mutations A2-M10 to M13 restore a `main`-only filter, drop the `push` trigger, drop the pull-request trigger, and put the job back in `ci.yml`; all are killed.
- **Verified live:** [45463421113](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45463421113) and [45476878966](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476878966) are `push`-event runs of the scan on `r2.10c3c-adjudication`, a branch `ci.yml`'s `push` trigger does not match, both green; the pull-request runs ([45476887470](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476887470)) are green too. With that, A2 meets its criterion of scanning on every push.

**A3: an SBOM per image, from the images themselves.**
- In the `image-reproducibility` job, after both no-cache builds of each image: Syft 1.57.1 (Anchore, Apache-2.0), its release archive checked against the pinned SHA-256 `bf52519e…44eef0c` before it is unpacked, catalogues each built image (`syft scan docker:<tag>`) into **CycloneDX 1.6 JSON**, for both builds of both images.
- `scripts/sbom_check.py`, run from a hash-locked venv (`scripts/sbom_lock.txt`, cyclonedx-python-lib 11.12.0 with its JSON validation, 27 entries, 0 advisories), requires of every SBOM:
  - **valid:** CycloneDX 1.6, valid against the official schema in strict mode, generated by Syft 1.57.1, with components;
  - **accurate:** for the backend, every entry of `backend/requirements-lock.txt` that installs on the image's platform (linux, x86_64, CPython 3.14.7: 39 of the 40, `tzdata` being Windows-only) as a `pkg:pypi` component at its exact locked version, and the Debian base; for the frontend, the pinned nginx 1.27.5 and the Alpine base;
  - **reproducible:** the two builds' SBOMs list the same components (type, name, version, purl);
  - **clean:** no runner host path and no credential value or credential shape.
- A deliberately invalid SBOM (`tests/fixtures/sbom/invalid-component-type.cdx.json`) must be rejected first, for the schema reason, so a checker that accepts everything cannot pass.
- One SBOM per image is kept as the artifact `sbom-<run>-<attempt>`. Nothing is published, signed or sent anywhere; signing stays with decision §3.12.
- In run [45476887432](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476887432): 2,882 backend and 1,305 frontend components, identical across the two builds; 39 of 39 locked packages; Debian 13.6 and Alpine 3.21.3; artifact `sbom-45476887432-1`.
- **Coverage:** the two images this repository builds, `backend` (also run as `migrate`) and `frontend`. The `postgres` image is upstream's, pinned by digest, and has no SBOM from this job; no claim is made for it.
- `tests/backend/test_ci_sbom.py`, 43 tests; mutation stage A3, 12 recorded.

**A5: an internal metrics endpoint.**
- `GET /metrics` serves the Prometheus text format 0.0.4: `http_requests_total` (counter, by `method`, `route`, `status`) and `http_request_duration_seconds` (histogram, by `method` and `route`, fixed buckets 5 ms to 10 s).
- **Bounded labels:** `route` is the matched route's template (`/api/v1/hotels/{hotel_id}`), never the path; an unmatched request is `unmatched`; a method outside the seven routable ones is `OTHER`. No identifier, e-mail address, query string or body can become a label.
- **Internal only:** nginx forwards only `/api/`, the API publishes no host port, and a request carrying any forwarding header (`Forwarded`, `X-Forwarded-*`, `X-Real-IP`) gets the ordinary 404. It is not in the OpenAPI document. It reads no database and sends nothing anywhere; where alerts go remains decision §3.12.
- `RequestMetricsMiddleware`, pure ASGI, sits just inside the security headers and only observes; a failing request is counted as 500.
- **Verified on the real stack** (`docker-runtime`): from inside the network the API answers 200 with the right content type, both families and the health probes counted, and 404 to a request with `X-Forwarded-For`; through nginx, `/metrics`, `/api/metrics`, `/api/v1/metrics` and `/api/../metrics` never return a metric (nginx answers the first and last with the frontend's page and the others with 404).
- `/metrics` joins the public-path list of `tests/backend/test_authorization_surface.py`, with its argument; every other boundary is unchanged.
- **Fingerprint:** `core/metrics.py` and `middleware/metrics.py` are fingerprinted (`METRICS_SOURCES`) rather than excluded, because the middleware runs on every measured request; `api/routes/metrics.py` is excluded as not run; 55 sources, 170 exclusions. Recorded for your review in the decision record's fifth re-pin row.
- `tests/backend/test_metrics_endpoint.py`, 25 tests with a strict exposition parser; mutation stage A5, 12 recorded.

**A9: UI-10's 51 checks, automated.**
- `frontend/e2e/responsive-checks.mjs` holds UI-10's specification unchanged: the 17 routes in its order, 375, 768 and 1440px at a 900px height, and per page no sideways scroll, no element escaping the viewport and exactly one `h1`, measured with UI-10's own method. `frontend/e2e/responsive-audit.mjs` drives it.
- The CI job `responsive-audit` builds and starts the Compose stack, seeds it with the **synthetic** demo data (`scripts/seed_demo.py`, generated credentials, masked), grants the platform role, and runs the audit in Chromium from `mcr.microsoft.com/playwright:v1.65.0-noble`, pinned by digest, whose version is the `playwright-core` 1.65.0 the lockfile installs. Viewports are set explicitly, so nothing depends on a local display. Any failing check fails the job and is named; the JSON report is uploaded.
- In run [45476887432](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476887432): **51 of 51 checks passed.**
- **The known defect stays visible.** The Analytics expenses table at 1440px is not one of the 51 and is not marked fixed: the job probes it separately and reports, without counting it, that it still reproduces ("Ledger expenses": a 525px table in a 482px box).
- `frontend/e2e/responsive-checks.test.mjs`, 18 Vitest tests; `tests/backend/test_ci_responsive_audit.py`, 13 tests; mutation stage A9, 13 recorded (9 Vitest, 4 pytest).

**Mutations:** stage A2 13 (9 + 4 for the correction), A3 12, A5 12, A9 13: **963 in all**, every one killed in run [45476887432](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476887432).

**Re-pin (Decision F §12.6).** `ci.yml`, `main.py`, `retrieval_fingerprint.py` and the new metrics modules are fingerprinted.
1. Run [45463427755](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45463427755) on `7858c27` was red only on the pin gate: `inputs changed: sources`. Its other 7 jobs passed, including the SBOM steps, the metrics step and the 51 checks, with 963/963 mutations killed.
2. Its artifact `retrieval-eval-45463427755-1` verified offline with `python -m tests.evaluation.retrieval_artifacts verify` against the job log's digests: files, digests, fingerprint, components, tree and repository groups.
3. The `report`, `results`, `signoff` and `manifest` sections are identical to the previous pin. Only `inputs.sources` (exactly `ci.yml`, `core/__init__.py`, `core/metrics.py`, `main.py`, `middleware/__init__.py`, `middleware/metrics.py` and `retrieval_fingerprint.py`), the `sources` component and the fingerprint moved.
4. Pinned, bytes unchanged, in `133c5be`:
   - report `4a697ebd6c05f6e566aae11a3d8378ff69c5324cf5fc89890481d2e40a89be2b`;
   - environment `8594fc287defc30b3f5c489b5484dfb2a948ecbc26d8287efa054752665b656c`;
   - fingerprint `068f8bd16983e66f3391945b89becec4e9b5c03c0fd9e304e3fffe776f963f1b`.
5. `f4503e4` admitted the digests and recorded the re-pin.

**CI:** [45476887432](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476887432) on `f4503e4`, all 8 jobs green (the scan left `ci.yml` and `responsive-audit` joined it, so a run is still 8 jobs):
- 12,771 backend tests passed (shards 10,416 + 1,096 + 1,259; 58 skipped), with the pin gate passing against the new pin;
- Frontend: `Test Files 37 passed (37)`, `Tests 1444 passed (1444)`;
- `963 mutations: 963 killed, 0 survived, 0 not run, 0 errors`;
- 9,551 backend files and 68 served frontend files byte-identical across two no-cache builds, then the SBOMs above;
- Docker runtime verification passed, the metrics step included;
- the responsive audit: 51 of 51.

The dependency scan ran beside it as its own workflow: [45476878966](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476878966) (`push`) and [45476887470](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45476887470) (`pull_request`), both green.

A3, A5 and A9 are counted complete from this run, and A2's limitation is closed by it.

**Limitations:**
- The SBOMs describe what Syft can catalogue in the image. Files no package manager owns (the application's own source) are not components.
- The metrics live in the worker's memory and start at zero with it; nothing scrapes them, and no alert exists (§3.12).
- The responsive audit runs Chromium only, as UI-10 did.

### 4.7 The remaining task

It can be done without a decision from you.
**F** marks a change to a fingerprinted file (`ci.yml`, `main.py`), which needs a re-measure and
re-pin under §12.6. Tasks marked F are batched into one re-pin stage.

| # | Task | Priority | Touches | Depends on | Evidence |
|---|---|---|---|---|---|
| ~~A1~~ | **Done** (§4.1): 6 advisories → 0 | — | — | — | — |
| ~~A2~~ | **Done** (§4.5, corrected in §4.6): "Dependency vulnerability scan" (npm audit at high, pip-audit 2.10.1 on every lock entry, positive controls first, outages fail); PyJWT 2.13.0 → 2.15.1 and certifi → 2026.10.23 fixed first; re-pinned, CI 45398092718 green. It first ran only on `main` and pull requests; since `7858c27` it is its own workflow on every push, verified by `push`-event runs; 13 mutations | — | — | — | — |
| ~~A3~~ | **Done** (§4.6): Syft 1.57.1 (checksum-verified) CycloneDX 1.6 SBOMs of both builds of both images, schema-valid, every locked package at its version, identical across builds, no host path or credential, kept as `sbom-<run>-<attempt>`; 12 mutations; CI 45476887432 green | — | — | — | — |
| ~~A4~~ | **Done** (§4.3): axe-core checks on every route and state, 9 genuine violation kinds fixed, 10 mutations; CI 45063664775 green | — | — | — | — |
| ~~A5~~ | **Done** (§4.6): `GET /metrics`, Prometheus text, request counts and latencies by method, route template and status; no nginx route, 404 to proxied requests, proven on the real stack; 12 mutations; CI 45476887432 green | — | — | — | — |
| ~~A6~~ | **Done** (§4.2): README, screenshots, frontend README, execution log, `ui-redesign.md`, 12 regression tests, 8 mutations | — | — | — | — |
| ~~A7~~ | **Done** (§4.4): placeholder scaffolding removed, Not Found styles identical (digests), the navigation-to-route guarantee now a test, 4 mutations; CI 45150368805 green | — | — | — | — |
| A8 | **R6.1: consolidate the stage-snapshot documents** (35 under `docs/` + 6 under `docs/deployment/`) into a small set of maintained current-state documents | documentation | docs; tests that pin doc text (`test_current_state_docs.py`, `test_runbook_schema_facts.py`, others) must move with it | A6 (fix the facts before moving them) | the earlier "14 of 29" counted R6.1 as done; it was never performed |
| ~~A9~~ | **Done** (§4.6): CI job `responsive-audit`, UI-10's 51 checks in a digest-pinned Playwright 1.65.0 Chromium against the seeded Compose stack; the Analytics expenses-table clipping reported separately and still open; 13 mutations; CI 45476887432 green | — | — | — | — |

**Dependencies, so no work is done twice:** A6 → A8, which is last so the facts move only once.
Nothing here waits on you.

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

**Not covered by any gate at the audit:** dependency vulnerability scanning (A2), accessibility
(A4), the responsive audit (A9) and runtime metrics (A5). All four are gated now (§4.3, §4.5,
§4.6).

---

## 7. Readiness and risks

### 7.1 Three levels

| Level | Verdict | Basis |
|---|---|---|
| **Feature complete** | **Yes, for the decided scope.** | Every built area and every V1/V2 stage is implemented and tested; every decision-free roadmap task is done except R6.1. Stage 7.15 and R5 are out of reach only because they wait on decisions, not because engineering is missing |
| **Demo ready** | **Yes.** | Deterministic demo seed, synthetic data labelled as such, the full stack runs locally and in CI, and the UI is finished at three widths. The README's screenshots and copilot description are current (A6). One caveat: the copilot can be shown only with a configured model, which under your constraints means local Ollama |
| **Production ready** | **No.** | No password reset, email or deactivation flow (R1.2); manual certificates, local-only backups, no point-in-time recovery (R4.1); an internal metrics endpoint but no alerting (R4.3, A5); Dependabot inactive, though every push is scanned (R0.4a, A2); intelligence accuracy unproven on real data (R2); open registration by design. Each needs a decision in §5 or a task in §4 |

### 7.2 Security and operational risks

| Risk | Severity | State |
|---|---|---|
| No password reset; deactivation only by setting `is_active` directly (enforced at login, refresh and session lookup, but there is no endpoint) | high for production | decision §3.4 |
| Certificates replaced by hand; backups local, unencrypted, unscheduled; no PITR | high for production | decision §3.11 |
| No dependency vulnerability gate; Dependabot inert off `main` | resolved for scanning: every push is scanned (A2, §4.5–§4.6); Dependabot still inert | decision §3.3 |
| Dev-toolchain advisories (3 high) | resolved: 6 → 0 | A1 done (§4.1) |
| No runtime metrics or alerts | metrics resolved, internal only (A5, §4.6); no alerts | decision §3.12 |
| Analytics expenses table clipped at 1440px (last column) | low, cosmetic | **open**: found on 2026-11-24, after A2; reported by every responsive-audit run, not one of the 51 checks and not fixed |
| Per-process rate limiter and copilot budgets | low at one worker | decision §3.6 |
| Open registration, and hotel creation makes you its owner | by design for self-hosting | documented in the README |
| Unauthenticated health probes | low (reachable only through nginx, no business data) | documented |
| Unproven model accuracy and unevaluated copilot | credibility, not safety: never claimed | decisions §3.7, §3.8 |
| README contradiction about LLM capability | resolved, and now guarded by a test | A6 done (§4.2) |

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
| Audit findings not already on the roadmap (A1–A7, A9; A8 is R6.1) | 8 | 8 | 100.0 |
| **All** | **71** | **55** | **77.5** |

- **Within the decided scope** (the 15 decision-blocked rows removed): 55 of 56 = **98.2%**. The one left is R6.1 (A8).
- **Strict** (R0.2 and R0.4a not counted as done): 53 of 71 = 74.6%.
- A3 and A5 are the local halves of R4.3b and R4.3a. They count as audit findings; the two roadmap rows stay decision-blocked, because signing and alert destinations still need §3.12.
- At the audit (`f2b8d50`): 47 / 71 = 66.2%, 47 / 56 = 83.9%, strict 45 / 71 = 63.4%. After A1 (`044df52`, CI 45035486687 green): 48 / 71 = 67.6%. After A6 (`485dd15`, CI 45046381883 green): 49 / 71 = 69.0%. After A4 (`4c9a2a0`, CI 45063664775 green): 50 / 71 = 70.4%. After A7 (`5125b5b`, CI 45150368805 green): 51 / 71 = 71.8%. After A2 (`6a91858`, re-pinned in `fda8718` and `9fec999`, CI 45398092718 green): 52 / 71 = 73.2%. After the A2 correction, A3, A5 and A9 (`7858c27`, re-pinned in `133c5be` and `f4503e4`, CI 45476887432 green): 55 / 71 = 77.5%.

---

## 9. Recommended implementation sequence

1. ~~**A1**~~ — done (§4.1).
2. ~~**A6**~~ — done (§4.2).
3. ~~**A4**~~ — done (§4.3).
4. ~~**A7**~~ — done (§4.4).
5. ~~**A2**~~ — done (§4.5), re-pinned on its own as its stage prompt asked; its trigger corrected in §4.6.
6. ~~**Re-pin batch: A2 correction + A3 + A5 + A9**~~ — done (§4.6), re-pinned once under §12.6.
7. **A8 / R6.1** — consolidate the documents, last, so the facts move only once.
8. Then whatever your answers to §5 unlock. The first steps toward production are §3.4 (R1.2) and §3.11 (R4.1).
