# Project closure audit

> **Closure audit of 2026-11-25**, against `15a29d1` on branch `r2.10c3c-adjudication`.
>
> **Verification and documentation only.** No application code, migration, test, CI workflow or
> frozen evaluation artifact was changed, and nothing was merged, released or published. Every
> figure below was checked against the repository or GitHub on the day; where the verification
> found a document stale, the correction is listed in §11.
>
> The running record of the work is [project-completion-audit.md](project-completion-audit.md);
> this document closes it. The decision briefs it summarises are
> [roadmap-v2.0-execution.md §3](roadmap-v2.0-execution.md#3-decision-briefs).

## 1. Executive summary

- **All engineering that needs no decision from you is done.** The audit's nine tasks, A1–A9,
  are complete, and every one was verified in CI.
- **One deviation is recorded rather than hidden.** R6.1 asked for the stage snapshots to be
  consolidated into a small set of maintained documents. A8 met it with an index, a status for
  every document and a link checker. It did **not** merge or move any snapshot (§4).
- **The repository is green.** CI run
  [45517429233](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45517429233)
  on `15a29d1` passed all 8 jobs, with 987 of 987 mutations killed. The dependency scan passed on
  both its push run and its pull-request run.
- **The retrieval pin holds.** In that run the C.4 report was re-measured and came out
  byte-identical to the pinned report.
- **13 decisions are yours** (§7). Each one commits you to something outside the repository:
  money, an account, a legal permission, a public act, or a change to past figures.
- **The platform is demo ready and is not production ready** (§8). One known defect is open:
  the Analytics expenses table is clipped at 1440px (§9).

| Metric | Value | Counts |
|---|---|---|
| Audit completion | **56 / 71 = 78.9%** | every tracked item |
| Strict completion | **54 / 71 = 76.1%** | the same, without the two half-activated rows |
| Decision-free work | **56 / 56 = 100%** | only the items that need no decision from you |
| v2.0 roadmap matrix | **14 / 29 = 48.3%** | only the roadmap's 29 rows |
| Open user decisions | **13** | decision briefs §3.1–§3.13 |

These measure different things and are not combined (§3). All four rows count R6.1 as done; read
literally, each falls by that one row (§3.3).

## 2. Verified repository state

Checked live on 2026-11-25. Nothing here is copied from an earlier report.

| Check | Result | How it was checked |
|---|---|---|
| Branch | `r2.10c3c-adjudication` | `git branch --show-current` |
| HEAD | `15a29d15c0260be7adf9ac95c6df3a1197463c3e` ("docs: record A8 (R6.1) as complete") | `git rev-parse HEAD` |
| Remote branch | the same commit | `git fetch`, `git rev-parse origin/r2.10c3c-adjudication` |
| Working tree | clean, 0 paths | `git status --porcelain` |
| `main` | `2a14dc6852784a51934d0aee3543ba49f9e2fd90`, unchanged | `origin/main` after fetch |
| PR #1 | **open, not merged**, not a draft; `r2.10c3c-adjudication → main`, head `15a29d1`, 85 commits | `gh pr view 1` |
| CI on the completion commit | run [45517429233](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45517429233) (`pull_request`), **success, 8 of 8 jobs** | `gh run view` |
| Dependency scan, push | run [45517424526](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45517424526) (`push`, this branch), success | `gh run view` |
| Dependency scan, pull request | run [45517429151](https://github.com/geortsam/AI_Hotel_Intelligence_Platform/actions/runs/45517429151) (`pull_request`), success | `gh run view` |
| A1–A9 | each marked done in the completion audit's task table, each with its own section (§4.1–§4.7) | the audit |

**The 8 jobs of run 45517429233, from its log:**

| Job | Evidence |
|---|---|
| quality-gates (shard 1/3) | `10506 passed, 58 skipped`. Also `All checks passed!` (ruff), `587 files already formatted` and `Success: no issues found in 558 source files` (mypy) |
| quality-gates (shard 2/3) | `1096 passed` |
| quality-gates (shard 3/3) | `1259 passed` |
| Frontend quality gates | `Test Files 37 passed (37)`, `Tests 1444 passed (1444)` |
| Mutation checks | `987 mutations: 987 killed, 0 survived, 0 not run, 0 errors` |
| Image reproducibility | builds identical across two no-cache builds; the SBOMs list `2882 components` (backend) and `1305 components` (frontend), the same in both builds |
| Docker runtime verification | success |
| Responsive audit | `51 of 51 checks passed`, and the known defect still reproduces (§9) |

Backend: **12,861 passed, 58 skipped**, 0 failed. Frontend: **1,444 passed** in 37 files.

**Retrieval pins and frozen artifacts:**
- **Report, re-measured:** the run measured the C.4 report again. Its `report.json` (artifact
  `retrieval-eval-45517429233-1`, downloaded and hashed) is
  `4a697ebd6c05f6e566aae11a3d8378ff69c5324cf5fc89890481d2e40a89be2b`, exactly the pinned
  `tests/evaluation/retrieval_report_v2.json`.
- **Environment record:** its `environment.json` differs from the pinned one only in run
  metadata: the run id, the commit and tree checked out, the CPU model and the timestamps.
- **Fingerprint inputs:** recomputed at `15a29d1`, they match the pin. There are 78 sources, no
  documentation file among them, and the `sources` and `metrics` digests are unchanged.
- **Frozen records:** identical to `80ddaee`, the commit before A8. That covers the decision
  record (`24a0534d…`), §3.5's signed rubric (`c86149e5…`), the review sheet (`fb79c2fb…`), the
  sign-off (`dc524890…`), the dataset (`de0cd929…`), the pinned report (`4a697ebd…`) and its
  environment (`8594fc28…`). No file under `tests/evaluation/` changed since then.
- **Offline tests:** the pin-admission, sign-off, review and fingerprint suites pass locally, 282
  tests.

## 3. Completion accounting

### 3.1 What each metric measures

**The 71 items.** An item counts as done only when it is implemented and its CI is green.

| Workstream | Items | Done |
|---|---|---|
| Delivered platform stages: the README's 13 stage groups (1 → 7.1–7.4) and V2 stages 7.5–7.14 | 23 | 23 |
| UI redesign UI-0 … UI-10 | 11 | 11 |
| v2.0 roadmap matrix: 28 task rows and §2.5 | 29 | 14 |
| Audit findings not already on the roadmap (A1–A7, A9; A8 is R6.1) | 8 | 8 |
| **All** | **71** | **56** |

- **Audit completion, 56 / 71 = 78.9%.** How much of all tracked work is done, whether or not it
  needs a decision. It cannot reach 100% without your decisions.
- **Strict completion, 54 / 71 = 76.1%.** The same, but R0.2 and R0.4a are not counted as done.
  Both are implemented in the repository, but neither is fully in effect:
  - R0.2's mirror does not cover the CI `services:` containers;
  - R0.4a's Dependabot configuration is inert until it reaches `main`.
- **Decision-free work, 56 / 56 = 100%.** The 71 items minus the 15 matrix rows that wait on a
  decision or on such a row. This answers whether anything is left that could be done without
  you, and the answer is no.
  - R0.2 and R0.4a are in the 56. They are done as far as the repository can take them.
  - Their remaining halves are briefs §3.2 and §3.3.
- **v2.0 roadmap matrix, 14 / 29 = 48.3%.** Only the roadmap's own rows. It is low because 15 of
  its 29 rows wait on a decision or on such a row.
- **Open decisions: 13.** The briefs §3.1–§3.13. They map onto the 12 decision rows, plus R0.2
  and R0.4a, whose remaining halves are briefs §3.2 and §3.3. R4.3a and R4.3b share one brief,
  §3.12. The 3 blocked rows (R2.2, R2.4, R3.2) have no brief of their own: each opens when the
  decision it depends on is made.

### 3.2 The matrix, recounted on 2026-11-25

| Class | Rows | Count |
|---|---|---|
| Done | R0.3, R0.4b, R1.1, R1.3a, R1.4b, R1.4c, R1.5, R3.3, R4.2, R6.1 (as an index, §4), R6.2, §2.5 | 12 |
| Done as far as the repository can take it | R0.2, R0.4a | 2 |
| Waiting on a decision | R0.1, R1.2, R1.3b, R1.4a, R2.1, R2.3, R3.1, R3.4, R4.1, R4.3a, R4.3b, R5 | 12 |
| Blocked by such a row | R2.2 (R2.1), R2.4 (R2.2), R3.2 (a checkout flow, R5) | 3 |
| **Total** | | **29** |

R4.3a and R4.3b each have their local half done, in A5 and A3. The rows themselves stay
decision-bound.

### 3.3 With R6.1 read literally

If R6.1 counts only once the snapshots are merged, which A8 did not do (§4), each figure loses
one row:

| Metric | As recorded | R6.1 literal |
|---|---|---|
| Audit completion | 56 / 71 = 78.9% | 55 / 71 = 77.5% |
| Strict completion | 54 / 71 = 76.1% | 53 / 71 = 74.6% |
| Decision-free work | 56 / 56 = 100% | 55 / 56 = 98.2% |
| v2.0 roadmap matrix | 14 / 29 = 48.3% | 13 / 29 = 44.8% |

Which basis to use is your call. The repository records the first, with the deviation stated
wherever R6.1 is counted.

## 4. R6.1, reconciled

**What the roadmap asked.** [roadmap-v2.0.md](roadmap-v2.0.md) R6: "Consolidate the
stage-snapshot documents into a small set of maintained current-state documents." The execution
matrix's verification column asked that links resolve and doc tests pass.

**What was implemented (A8, `305f50c`, `b6c6c65`, `15a29d1`):**
- `docs/README.md`, a single entry point. Every document has exactly one entry and one of
  three statuses: 22 current, 15 frozen records, 6 superseded. Each superseded snapshot names its
  current replacement.
- `scripts/doc_links.py`, run in CI, which checks every relative link, every anchor and that
  every document is reachable from the index.
- Eight contradictions resolved, each guarded by a test.
- 24 recorded mutations.

**What was not.** No stage snapshot was merged into a current-state document, and no document was
moved, renamed or deleted. The repository still holds the 43 Markdown documents A8 indexed, plus
the index and this closure audit. That is not "a small set".

**Why the files and paths were kept:**
- **Fingerprinted sources cite them.** Nine documents are cited by path from the retrieval
  fingerprint's sources (`ci.yml`, the knowledge endpoint and service, `core/metrics.py`,
  migration `0023`, the C.4 harness modules). Moving one leaves a stale citation or forces a
  re-measure and re-pin.
- **Tests and the mutation inventory name them.** About forty test and harness modules, and four
  mutation-inventory constants, name documents by path. Several assert exact wording.
- **Some records are bound by checksum.** The decision record's path is a constant in a
  fingerprinted module, and its §3.5 is bound by checksum.
- **The A8 prompt forbade it.** It forbade rewriting historical decision records and asked that
  documents move only where navigation clearly improves.

**What of R6.1 remains unmet:**
- **The literal merge.** The 6 superseded snapshots have not been folded into the current
  documents: `backend-architecture.md`, `database-design.md`, `database-implementation.md`,
  `analytics-design.md`, `ml-design.md` and `roadmap-v2.0.md`.
- **The frozen records.** The 15 frozen records are separate by design and are not part of what
  remains.

**Can a future consolidation be done safely?** Yes for the 6 superseded snapshots, under
conditions. No for the 15 frozen records.
- **No re-pin needed for the six.** None of them is cited by a fingerprinted source (checked on
  2026-11-25 against the pin's 78 sources).
- **Their code citations can be kept valid.** Three are cited from code comments:
  `api/openapi.py`, `schemas/analytics.py` and `ml/__init__.py`. A redirect stub left at each old
  path keeps those citations valid, with no code change.
- **Tests and mutations would move with the text, equally strict.** Three of the six are held by
  A8's own guards: `tests/backend/test_consolidated_docs.py` and mutations A8-M21 to M23. A merge
  would have to carry those assertions and mutation anchors to the new text, as strictly as
  before. The index and its tests would change in the same commit.
- **What a merge would cost.** Their still-valid content (the schema rationale, the metric
  definitions, the statistical layer's design) would move into `architecture.md` and
  `database/README.md`. `database-design.md` alone is about 100 KB, so a merge makes those
  documents much larger. Whether that is better than a labelled snapshot is a judgment, not a
  defect.
- **The frozen records stay.** They are stage records, several are checksum-bound or
  test-pinned, and the decision record's path is fingerprinted. Merging them would rewrite
  history, which is out of bounds.

## 5. Completed engineering, and what waits on you

**Completed, verified in CI:**
- every delivered platform stage, 1 to 7.14;
- the UI redesign, UI-0 to UI-10;
- the v2.0 rows R0.3, R0.4b, R1.1, R1.3a, R1.4b, R1.4c, R1.5, R3.3, R4.2, R6.1 (as an index),
  R6.2 and §2.5;
- the repository halves of R0.2 and R0.4a;
- the local halves of R4.3a (A5) and R4.3b (A3);
- the audit's tasks A1–A9.

**Blocked by your decisions (§7):** R0.1, R1.2, R1.3b, R1.4a, R2.1, R2.3, R3.1, R3.4, R4.1,
R4.3a's alerting, R4.3b's signing and publication, R5, and the remaining halves of R0.2 and
R0.4a.

**Blocked by those rows:** R2.2 (needs R2.1), R2.4 (needs R2.2) and R3.2 (needs a checkout flow,
R5).

**Optional, needs no decision:** the literal R6.1 merge of the six superseded snapshots (§4).
Doing it or not is a choice of scope.

## 6. Capabilities and limitations

| Area | Completed capability | Limitation that remains |
|---|---|---|
| Backend and API | FastAPI; 68 paths / 104 operations, 96 authenticated (computed by a test); a uniform error envelope; request-ID, body-size and security-header middleware | one API worker |
| Authentication and authorisation | Argon2id; bearer login for API clients; `HttpOnly` cookie sessions with refresh rotation, replay detection and CSRF; four hotel roles; tenant isolation answering 404; audited operator grant and revoke of platform administration | no password reset, email or deactivation endpoint (R1.2); open registration by design |
| Database | PostgreSQL 18.6; 29 tables; 23 linear migrations, head `0023_auth_sessions`; the booking non-overlap rule enforced by the database; `If-Match` optimistic concurrency | no room-status history (R3.1); no FX (R3.4) |
| Hotel operations, bookings, finance, analytics | the full booking lifecycle, availability, payments and refunds, the ledger, occupancy, ADR, RevPAR, per currency and on the hotel's calendar | the expenses table clips at 1440px (§9) |
| Intelligence and ML | deterministic statistical forecasts, anomalies and an attention list; a served demand model with stored predictions and accuracy, drift and performance endpoints | **no production accuracy is established**. The model learned from a public research dataset, not this platform's data (R2.1) |
| Copilot | seven read-only tools, checked figures, document citations through PostgreSQL full-text search, conversations, retention, budgets, a circuit breaker; local Ollama or Anthropic, off by default | **no language model's answer quality is established**. Retrieval recall@5 was 0.6957 against a declared 0.90 on an author-written set (R2.3); Stage 7.15 is untriggered |
| Retrieval evaluation | Decision F complete; the C.4 report is pinned and re-measured byte-identically on every CI run | measures retrieval only, on an author-written dataset |
| Deployment | a four-service Compose stack, TLS at nginx, healthchecks, a migration gate; backup → restore → serve, proven in CI; an opt-in retention scheduler | manual certificates; local, unscheduled backups; no point-in-time recovery; no zero-downtime deploy (R4.1) |
| Supply chain and observability | hash-locked Python dependencies; digest-pinned images; byte-reproducible builds; CycloneDX SBOMs; a dependency scan on every push; an internal `/metrics` | no alerting; images neither published nor signed (R4.3); Dependabot inert (R0.4a) |
| Frontend | React and TypeScript; 13 navigation areas; the redesign; axe-core checks; the 51-check responsive audit in CI | Chromium only; one open layout defect |

## 7. Decision register

Every entry follows [roadmap-v2.0-execution.md §3](roadmap-v2.0-execution.md#3-decision-briefs).
**Default** is what happens if you say nothing, as documented in the completion audit's §5.
**Recommendation** is advice, **not an approved decision**. **Blocks** is this audit's assessment,
for a demo, a pilot hotel and a production deployment.

Files marked † are fingerprinted: changing one means a C.4 re-measure and re-pin under §12.6.
That includes every new migration, since every migration version is fingerprinted.

### §3.1 R0.1 — release
- **Meaning:** whether to cut a release, which tag, and on which commit.
- **Options:**
  - **Tag `v1.0.1` on `main` now.** This releases `2a14dc6`, without Decision F or anything
    since.
  - **Merge PR #1, then tag the merge.** The recommended tag is `v1.1.0`, since it adds features.
    Merging is reserved to you.
  - **No release.**
- **Risks and dependencies:** a tag is public and permanent. Merging PR #1 is a prerequisite for
  the second option and for §3.3.
- **Blocks:** demo no; pilot no; production yes, in this audit's view, since a deployment should
  run an identified release.
- **Default:** no release.
- **Recommendation:** merge PR #1, then tag `v1.1.0` on the merge commit, if and when you want a
  release.
- **Would change:** no file. The tag is one `git tag` and one push; the merge brings every
  branch commit to `main`.

### §3.2 R0.2 — Docker Hub for CI
- **Meaning:** whether CI's `services:` containers keep pulling unauthenticated from Docker Hub.
- **Options:**
  - **Keep the mirror.** It covers the jobs that pull in their own steps; `services:` containers
    can still hit Hub rate limits or outages.
  - **Authenticated pulls.** These need a Docker Hub account and two repository secrets.
- **Risks:** CI reliability only. An outage has delayed a run before; it did not break a result.
- **Blocks:** none.
- **Default:** keep the mirror.
- **Recommendation:** keep the mirror; add authenticated pulls only if outages recur.
- **Would change:** `.github/workflows/ci.yml`† (credentials on the `services:` containers) and
  two repository secrets, `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.

### §3.3 R0.4a — Dependabot activation
- **Meaning:** whether the prepared update configuration starts opening pull requests.
- **Options:**
  - **Activate.** GitHub reads `.github/dependabot.yml` only from `main`, so this needs PR #1
    merged. Weekly grouped pull requests follow. Any that touches `pyproject.toml`†, a lock† or
    `ci.yml`† needs a re-pin before it can merge.
  - **Leave inactive.** Pins go stale; every push is still scanned for known advisories.
- **Risks:** without updates, a pinned image or package with a later-disclosed vulnerability
  stays until bumped by hand.
- **Blocks:** demo no; pilot no; production recommended, not strictly blocking, because scanning
  exists.
- **Default:** inactive.
- **Recommendation:** activate, as prepared, when PR #1 merges.
- **Would change:** nothing on this branch. Later update pull requests would change locks,
  `pyproject.toml`†, `ci.yml`† or image digests.

### §3.4 R1.2 — password reset, email, deactivation
- **Meaning:** who may deactivate an account (accounts are shared across hotels), and which email
  provider sends reset messages.
- **Options:**
  - **Platform administrators only.** Hotel owners remove memberships, which already exists.
  - **Hotel owners too.** An owner could then lock a user out of other hotels.
  - **Email provider:** any SMTP provider, configured by environment.
- **Risks and dependencies:** a provider account, a sender domain and credentials. It is a
  security-policy choice across tenants.
- **Blocks:** demo no; pilot yes, in this audit's view, because real staff need a reset path;
  production yes.
- **Default:** no reset or deactivation flow. `is_active` is set directly, and is enforced at
  login, refresh and session lookup.
- **Recommendation:** platform administrators only, with SMTP by environment.
- **Would change:**
  - backend: a new migration† (reset tokens), `backend/app/models/user.py`,
    `services/auth.py`, `api/v1/endpoints/auth.py`, `api/v1/router.py`† for new routes,
    `schemas/auth.py`, `core/config.py` (SMTP settings), and `models/enums.py`† for new audit
    actions;
  - the frontend sign-in and reset pages;
  - tests and mutations;
  - `docs/architecture.md` and the README's security posture.

### §3.5 R1.3b — platform-administrator API
- **Meaning:** whether administrators may grant or revoke the capability over the API.
- **Options:**
  - **Operator command only.** This exists, audited.
  - **An API.** A compromised administrator could then create more administrators.
- **Risks:** privilege escalation through the API.
- **Blocks:** none.
- **Default:** operator command.
- **Recommendation:** operator command only.
- **Would change:** for the API, new endpoints under `backend/app/api/v1/endpoints/`,
  `api/v1/router.py`†, `repositories/platform_admin.py`, `models/enums.py`† if new audit actions
  are needed, the authorisation-surface tests and the frontend platform page. For the default,
  nothing.

### §3.6 R1.4a — shared rate limiter
- **Meaning:** when the deployment runs more than one API worker. The limiter and the copilot
  budgets are per process, so several workers would each allow the full limit.
- **Options:**
  - **PostgreSQL.** A table and a migration†, plus write load on every limited request.
  - **Redis.** A new service, a backup question and a new failure mode.
  - **Keep per-process.** Correct for one worker, which is the shipped stack.
- **Blocks:** none, until more than one worker runs; then production yes.
- **Default:** per-process.
- **Recommendation:** PostgreSQL, when a second worker is deployed.
- **Would change:**
  - always: `backend/app/core/rate_limit.py`;
  - for PostgreSQL: a migration† and a repository;
  - for Redis: `docker-compose.yml`, the Python requirements and locks†, and `core/config.py`;
  - the copilot budget accounting in either case.

### §3.7 R2.1 — data for the intelligence claims
- **Meaning:** whether a real data source, and permission to use it, will exist.
- **Options:**
  - **A public dataset.** R2.4 found none with the fields the contract needs.
  - **A pilot hotel's data under an agreement.**
  - **Neither.** No accuracy claim can be made, and none is.
- **Risks and dependencies:** a licence or a data agreement. It unblocks R2.2, then R2.4.
- **Blocks:** demo no; pilot no to run, yes to claim forecast accuracy; production yes for any
  reliance on the forecasts.
- **Default:** no accuracy claim.
- **Recommendation:** a pilot property, if one is available; otherwise leave the claims as they
  stand.
- **Would change:**
  - the dataset (raw data is never committed) and `ml/pipelines/build_demand_dataset.py`;
  - the evaluation and validation pipelines under `ml/pipelines/`;
  - a new artifact, and the `APPROVED_MODEL` identity if the model is retrained;
  - `docs/ml-model-card.md`, `docs/ml-training-data.md` and new evaluation records.

### §3.8 R2.3 — live copilot evaluation, then Stage 7.15
- **Meaning:** whether to pay for a live evaluation run and supply real hotel documents, which
  would let Stage 7.15 (pgvector) be assessed.
- **Options:**
  - **Approve.** A paid run of `scripts/copilot_live_eval.py` and real documents.
  - **Keep excluded.** Your evaluation constraints allow local Ollama only.
- **Risks:** spending, and the handling of real documents. Stage 7.15 depends on this.
- **Blocks:** demo no; the copilot is shown with a local model. Pilot and production: no to run
  it, yes to claim answer quality.
- **Default:** local Ollama only.
- **Recommendation:** keep excluded until real documents exist.
- **Would change:**
  - for the evaluation, no code; results are recorded in `docs/copilot-evaluation.md`;
  - for Stage 7.15, a migration† for the vector extension, the knowledge retrieval code, and a
    new retrieval dataset version and re-measure under Decision F.

### §3.9 R3.1 — room-status history (decision 6)
- **Meaning:** whether to add `room_blocks` for maintenance, out-of-order and owner use, sharing
  the bookings' overlap constraint, and whether historical occupancy is measured against it.
- **Options:**
  - **Build it.** Every past occupancy figure shifts.
  - **Keep as is.** Occupancy is measured against all active rooms.
- **Blocks:** none; it is an accuracy refinement.
- **Default:** unchanged.
- **Recommendation:** build it as its own stage once you rule on decision 6, and only if a pilot
  property needs it.
- **Would change:**
  - backend: a new migration†, a model, `repositories/analytics.py`, `services/analytics.py`
    and `services/availability.py`;
  - the frontend rooms, availability and analytics pages;
  - tests, and the README's analytics caveats.

### §3.10 R3.4 — FX rates (decision 14)
- **Meaning:** whether figures in different currencies are ever converted.
- **Options:**
  - **Keep transaction currency only.** This is today's behaviour: per-currency figures, never
    summed.
  - **Build a rate table and a which-day rule.**
- **Blocks:** only multi-currency properties.
- **Default:** transaction currency only.
- **Recommendation:** keep, unless a multi-currency property is a target.
- **Would change:** a new migration†, `services/analytics.py`, `repositories/analytics.py`,
  `schemas/analytics.py`, the analytics pages and the README's currency statements.

### §3.11 R4.1 — certificates and backups
- **Meaning:** a public domain, an ACME account, and off-site encrypted backup storage with point-
  in-time recovery.
- **Options:**
  - **Provide them at the first real deployment.**
  - **Keep the current setup:** manual certificates and local, operator-run backups.
- **Risks and dependencies:** external accounts and credentials. Until then a lost host loses
  the data.
- **Blocks:** demo no; pilot yes if it is reachable from the internet or holds real data;
  production yes.
- **Default:** manual certificates, local backups.
- **Recommendation:** at the first real deployment, decided together with §3.12.
- **Would change:**
  - deployment: `frontend/nginx.conf`, `docker-compose.yml` (an ACME client and its volume),
    and a backup job under `backend/app/jobs/` or a separate service with its schedule;
  - `docs/deployment/tls.md` and `docs/deployment/backup-restore.md`;
  - secrets, which stay outside the repository.

### §3.12 R4.3 — alerts, registry, signing (R4.3a, R4.3b)
- **Meaning:** where alerts are delivered, and which registry images are published to and
  signed in.
- **Options:**
  - **Choose a destination and a registry.** Then add scraping and alert rules, and publish and
    sign images in CI.
  - **Keep as is.** The metrics stay internal with no alerts; images are built and verified but
    published nowhere.
- **Risks and dependencies:** a signing identity, and registry credentials. Decide with §3.11.
- **Blocks:** demo no; pilot partly, since no one is alerted; production yes.
- **Default:** none, with the local halves done (A5, A3).
- **Recommendation:** decide with R4.1, at the first real deployment.
- **Would change:** `.github/workflows/ci.yml`† (publish and sign steps), new alerting and
  scraping configuration, `docs/deployment/reproducibility.md` and the README.

### §3.13 R5 — product reach
- **Meaning:** the audience. Portfolio or academic, or a pilot hotel.
- **Options:**
  - **Portfolio.** None of R5 is needed, and R5 becomes out of scope.
  - **A pilot hotel.** Exports, payments, OTA import, sentiment or housekeeping as chosen. It
    also raises the priority of §3.4, §3.7 and §3.11.
- **Blocks:** it sets the scope of everything else. Demo no.
- **Default:** none of R5.
- **Recommendation:** portfolio, which matches how the project has been run, unless a pilot
  hotel is available.
- **Would change:** for portfolio, nothing. For a pilot, it depends on which R5 items are chosen;
  each would be its own stage.

## 8. Production-readiness gap register

The platform is **not production ready**. Each gap is supported by the repository.

| # | Gap | Severity for production | Closed by |
|---|---|---|---|
| G1 | No password reset, email or deactivation endpoint; `is_active` is set directly | high | §3.4 R1.2 |
| G2 | Certificates are replaced by hand; no ACME client; CI proves TLS only with a self-signed certificate | high | §3.11 R4.1 |
| G3 | Backups are operator-run, local, unencrypted and unscheduled; no off-site copy; no point-in-time recovery; `docker compose down -v` destroys the volume | high | §3.11 R4.1 |
| G4 | No alerting. `/metrics` is in-process, internal and unscraped | medium–high | §3.12 R4.3 |
| G5 | Images are published nowhere and not signed; SBOMs exist only as CI artifacts | medium | §3.12 R4.3 |
| G6 | Dependabot is inert; the Python locks are regenerated by hand; `backend/requirements-llm.txt` is not locked | medium (every push is scanned) | §3.3 R0.4a |
| G7 | The rate limiter and copilot budgets are per process; the stack runs one API worker | low at one worker | §3.6 R1.4a |
| G8 | No zero-downtime deploy: up to about 10 s of 502 on `/api` when the API is recreated; one nginx and one API | medium | not on the roadmap |
| G9 | No load or performance test exists in the repository | medium | not on the roadmap |
| G10 | Forecast accuracy is not established on real data | credibility | §3.7 R2.1 |
| G11 | No language model's answer quality is established; retrieval recall@5 was 0.6957 against 0.90 on an author-written set | credibility | §3.8 R2.3 |
| G12 | Occupancy ignores rooms out of service; no FX | accuracy, by declared scope | §3.9, §3.10 |
| G13 | Open registration, and creating a hotel makes you its owner | by design for self-hosting | §3.13 R5 |
| G14 | Unauthenticated health probes (`/health/db` opens a connection) and `/metrics` | low (internal, or no business data) | documented |
| G15 | CI's `services:` containers pull from unauthenticated Docker Hub | CI reliability | §3.2 R0.2 |
| G16 | Nothing is released: `main` is `2a14dc6`, PR #1 is unmerged, and the last tag is `v1.0.0` | release state | §3.1 R0.1 |

**Unverified assumptions, as the repository states them:**
- **Deployment evidence.** Claims about the deployment rest on CI: GitHub-hosted runners, a
  self-signed certificate, synthetic data. The stack has not been exposed to the public internet.
- **Demo data.** It is synthetic (`scripts/seed_demo.py`), and the screenshots show it.
- **The model's training data.** The model learned from a public research dataset about other
  hotels. How it would forecast for a property using this platform is unknown.
- **Copilot quality.** The copilot evaluation pins the pipeline's behaviour on hand-written
  reference exchanges. It measures no model.

## 9. Known defects

- **Analytics expenses table, clipped at 1440px. Open, not fixed.** The "Ledger expenses" table
  is 525px wide inside a 482px box, so its last column is clipped. It was found on 2026-11-24.
  Every responsive-audit run probes and reports it, most recently run 45517429233. It is not one
  of the 51 checks, so the audit still passes. Severity: low, cosmetic.
- **The responsive audit runs Chromium only**, as UI-10 did.

## 10. What this closure does not claim

- **Not production ready.** The project is not ready for production use.
- **No measured accuracy or quality.** Neither the demand model's production accuracy nor any
  language model's answer quality is established.
- **R6.1 is not met in its literal sense.** No stage snapshot was consolidated (§4).
- **No decision is assumed.** None of the 13 decisions has been taken here: the defaults above
  are what happens if nothing is decided, and the recommendations are advice.
- **Nothing was released, merged or published.**

## 11. Documentation corrected by this audit

The verification found three statements stale or understated, and corrected only those:
- **R6.1's row** in [roadmap-v2.0-execution.md](roadmap-v2.0-execution.md) said "done in A8". It
  now says R6.1 was met as an index, not as a merge, names what is not done, and points to §4.
- **That file's §1** said the fingerprint covers "47 files and every migration version". That was
  true when written; at `15a29d1` it is 55 files and all 23 migrations, 78 sources.
- **[project-completion-audit.md](project-completion-audit.md)** counted R6.1 as done without
  saying in its summary and metrics that the snapshots were not merged. Its summary, §4.7
  heading, §8.1 class and §8.2 now say so, with the literal-reading figures.

The completion audit's dated sections (§2, §3, §6, at `52fde7b`) stay as recorded: they are the
audit's own evidence, and this document supersedes them for the current state.
