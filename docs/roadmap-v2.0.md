# Roadmap after v1.0.0 — the v2.0 line

> **Status: proposal, not approved.** Written on 2026-10-10 against commit `be96736` (the
> `v1.0.0` tag is `4dae239`; `be96736` adds only the README count fix). Nothing below is
> implemented, scheduled or approved. Each stage needs its own authorization, as every earlier
> stage did.
>
> **Naming.** [v2-roadmap.md](v2-roadmap.md) already uses "V2" for Stages 7.2–7.14 (the copilot,
> knowledge documents and multi-horizon models), and those shipped **inside** `v1.0.0`. To avoid
> two meanings of "V2", this document calls the next major line **v2.0** and numbers its stages
> **R0–R6**.

---

## 1. What the platform does at v1.0.0

Figures verified at `be96736` (CI run 38046070392, all five jobs green): 65 API paths / 101
operations (96 require authentication), 28 application tables, migration head
`0020_guest_email_rules`, 7,984 backend tests (6 skipped), 1,346 frontend tests, 312 recorded
mutations all killed.

| Area | Capability |
|---|---|
| **Property management** | Hotels, room types, rooms, amenities; guests (format-checked, case-insensitive unique email); bookings with per-night pricing and a database-enforced non-overlap calendar; the full lifecycle — modify, extend, early departure, check-in/out, cancel, no-show; payments and refunds with a refund cap; a revenue and expense ledger; reviews; availability search; optional `If-Match` on PATCH |
| **Security** | Argon2id passwords; HS256 access tokens, revoked by a password change; login rate limiting; four hotel roles plus a separate platform-administrator capability; hotel-scoped tenant isolation that answers 404 to non-members; an append-only audit trail with archival |
| **Analytics** | Occupancy, ADR, RevPAR, revenue by category, expenses and reviews, computed on demand, per currency, on the hotel's own calendar |
| **Intelligence** | A deterministic statistical layer — day-of-week forecasts with prediction intervals, anomaly detection, demand trend and an attention list — and, separately, one served demand model whose predictions are stored (730 days by target date) with read, accuracy, drift and forecast-performance endpoints |
| **Copilot** | Read-only, seven tools, checked figures, citations from the hotel's documents (PostgreSQL full-text search), per-owner conversations with retention, budgets and a circuit breaker |
| **Frontend** | React + TypeScript single-page app, thirteen navigation areas |
| **Deployment** | TLS-terminated Docker Compose stack whose runtime, backup/restore and image reproducibility are exercised on real containers in CI |

---

## 2. Limitations at v1.0.0

### 2.1 The intelligence claims are unproven — the largest gap

- The served model's production accuracy, reliability and generalisation are **not established**
  ([ml-model-card.md](ml-model-card.md)). The demo data is small and synthetic, and the model
  returns roughly the same figure for any hotel at or below about 40 room nights a night.
- **No real language model has been evaluated** behind the copilot.
- Stage 7.15 (vector retrieval) **cannot be assessed**: no real hotel documents exist.

### 2.2 Security gaps before any real deployment

- The access token lives in `sessionStorage`, readable by script; there is no `HttpOnly` cookie
  session and no refresh token ([architecture.md](architecture.md) §7.1).
- No password reset, no email delivery, no account deactivation.
- Granting platform administration is an out-of-band database write.
- Rate limits and copilot budgets are kept per worker process, not globally; 429 is not declared in
  OpenAPI; nothing bounds overall request volume.
- `hotels.timezone` accepts any text (unknown zones fall back to UTC).

### 2.3 Data-model gaps

- No history of room status or of which rooms were active, so past occupancy is measured against
  **today's** active rooms (it can exceed 100%) and out-of-order rooms cannot be reconstructed.
- No FX conversion (approved decision 14): a multi-currency hotel has no single total.
- `daily_hotel_metrics` is reserved and empty, with five open decisions
  ([database-design.md](database-design.md) §8).
- The three global catalogues have no `updated_at`, so same-field edits are last-write-wins.
- `pending` bookings hold no inventory; there is no checkout or hold-expiry flow.

### 2.4 Operations

- Certificates are replaced by hand; no off-site or point-in-time backups; no zero-downtime deploy.
- The retention purges and audit archival are operator commands; nothing schedules them.
- No metrics exporter or alerting; no Dependabot or Renovate; no SBOM or image signing; transitive
  Python dependencies are resolved at build time.

### 2.5 Engineering

- The backend CI job takes about 45 minutes, and a local full run is split into nine groups.
- CI pulls images from Docker Hub unauthenticated, so a Docker Hub rate limit or outage can block
  it (it did once, during H11).
- The frontend suite times out on a heavily loaded machine (`--maxWorkers=2` works around it).
- 34 documents under `docs/`, many of them unmaintained stage snapshots; README counts are
  maintained by hand.

---

## 3. Stages

Size: **S** ≈ a day or two, **M** ≈ a week, **L** = several weeks. **★** marks the highest-value
stages.

### R0 — Release hygiene · S · do first

- Cut `v1.0.1` so the released README carries the corrected counts.
- Make CI independent of unauthenticated Docker Hub pulls: a pull-through mirror (for example
  `mirror.gcr.io`) or authenticated pulls. Digests stay pinned either way.
- Shard the backend CI job so it runs in parallel.
- Dependabot, and transitive Python pinning (a hash-locked file).

### R1 — Production security ★ · M–L · needs decisions

1. `HttpOnly; Secure; SameSite` cookie sessions with refresh tokens and CSRF protection.
2. An email channel, password reset and account deactivation. **Decision needed:** `users` is
   global across hotels, so who may deactivate an account.
3. A platform-administrator bootstrap and API.
4. A shared rate limiter (Redis or PostgreSQL), request-size limits, and 429 declared in OpenAPI.
5. Validate `hotels.timezone` against IANA zone names (deferred item D2).

### R2 — Evidence for the intelligence claims ★★ · L · the main credibility gap

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

### R3 — Data-model correctness · M–L · migrations required

1. **Room-status and active-room history** — a `room_blocks` table (deferred decision 6). It fixes
   historical occupancy, gives `out_of_order_rooms` a source, and unblocks the daily-metrics
   availability decision.
2. **Pending holds with expiry**, ahead of any booking-engine or checkout flow.
3. **`updated_at` and `If-Match` on the global catalogues.**
4. **FX rates** — a rate table and a rule for which day's rate applies. Only worth building for
   multi-currency properties.

### R4 — Operations · M

- ACME certificate automation; off-site encrypted backups with point-in-time recovery.
- A scheduler for the conversation, invocation and prediction purges and the audit archival.
- A metrics exporter with basic alerts; SBOM and image signing.

### R5 — Product reach · L each · choose by audience

- A portfolio view across the hotels a user belongs to.
- CSV/PDF report export.
- A payment-provider integration (payments are recorded today, not taken).
- Channel-manager / OTA booking import.
- Review sentiment.
- A housekeeping workflow, on top of R3's room history.

### R6 — Documentation · S–M

- Consolidate the stage-snapshot documents into a small set of maintained current-state documents.
- Generate the README counts in CI so they cannot go stale.

---

## 4. Recommended order

1. **R0**, because every later stage pays the CI cost.
2. **R2 — prove the intelligence.** The engineering is careful and heavily tested, but the AI half
   rests on synthetic data, an unmeasured model and an unevaluated copilot. Real data plus the
   measurement machinery already built would turn "implemented" into "shown to work" — or show,
   honestly, that it does not.
3. **R1 — security fit for a real deployment**, if the platform is to serve a real hotel.
4. **R3 room history** — one schema addition that resolves several documented analytics caveats.
5. **Hold off** on new models and features (more horizons, vector search, agents,
   recommendations) until R2 shows the existing ones are worth extending.

---

## 5. Decisions needed before any stage starts

- **Audience:** a portfolio or academic project, or a real pilot hotel? This sets how far R1 and
  R5 go.
- **Data:** is a real data source available for R2, and is a paid LLM evaluation acceptable?
- **Naming:** keep "v2.0 / R-stages" as above, or extend the existing "V2" stage numbering.
