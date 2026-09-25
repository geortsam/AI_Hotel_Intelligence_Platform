# V2 stage roadmap — definition, Stage 7.1

> The ordered plan produced by Stage 7.1. Each stage is specified well enough to be implemented
> on its own, in order, without re-deciding anything.
>
> **Implemented so far: Stages 7.2 to 7.9** — all of Track A, and the first five stages of
> Track B. Every other stage below is a specification and nothing more; none of its code
> exists.
> A stage carries `· *done*` in its heading once it ships, with a note recording what was actually
> built and where that differed from the plan.
>
> **The circuit-breaker gap Stage 7.5 left open is settled.** Stage 7.6 specified it —
> threshold, window, open duration, half-open probe, failure definition, owner and error code —
> as architecture **Amendment A1, §5.8**, and implemented it in `app.llm.circuit`. See Stage 7.6.
>
> Read [v2-architecture.md](v2-architecture.md) first: it holds the decisions these stages
> implement — the single-deployable topology, the LLM abstraction, the RAG design, the tool
> contract, the security model and the evaluation strategy.

---

## Ordering principle

Three rules shaped the order, and each one removed rework:

1. **Ship what already exists before building anything new.** Two whole capability areas from the
   V2 brief are already served by V1 endpoints and need only a front end or a route. Those come
   first: they are the lowest risk and the highest visible value in the product.
2. **Infrastructure before the feature that needs it.** The LLM abstraction, the tool boundary and
   the evaluation harness are built before a copilot answer is offered to anyone — matching the
   repository's own recorded position that an evaluation harness *"would be required before any of
   the above could be claimed"*.
3. **Measure before paying.** The pgvector decision is a stage of its own, gated on a retrieval
   measurement, so the pinned-image change is paid for against evidence rather than assumption.

There are two independent tracks. **Track A** (7.2–7.4) is product surface over existing V1
capability and involves no AI. **Track B** (7.5–7.13) is the generative platform. They share no
code until 7.9, so they can proceed in parallel if desired — but within each track the order is
strict.

```
Track A   7.2 ──► 7.3 ──► 7.4
                              ╲
Track B   7.5 ──► 7.6 ──► 7.7 ──► 7.8 ──► 7.9 ──► 7.10 ──► 7.11 ──► 7.12 ──► [7.13]
                                                                        │
                                          7.14 (ML, independent) ───────┘
```

---

## Track A — product surface over existing capability

### Stage 7.2 — Analytics dashboard view · *done*

> **Implemented.** `/analytics` renders a real reporting view; `PlaceholderPage` has no remaining
> consumer and every navigation area is now built. The stage went slightly beyond its original
> objective in one respect, for a reason found while implementing it: **every analytics and
> intelligence endpoint was already consumed by some screen except `/ml/demand-forecast`**, which
> had no frontend anywhere. Rebuilding the same figures in a new place would have been
> duplication, so the screen pairs period reporting with the first interface to the served demand
> model — fenced off in its own panel, labelled as modelled rather than recorded, carrying the
> model's version and its own `production_ready: false`, and drawing no confidence band.
>
> Delivered: 4 new components, 1 new service, 1 new type module, 50 tests, 0 backend changes,
> 0 migrations, 0 dependencies. Frontend suite 998 → 1050.

| | |
|---|---|
| **Objective** | Replace `PlaceholderPage` on `/analytics` with a real view over the five existing analytics endpoints |
| **Why it exists** | `/analytics` is the single navigation area of twelve that is not built. The backend has served its data since V1; the gap is entirely front end, and it is the most visible hole in the product |
| **V1 reused** | `analytics/overview`, `daily`, `revenue-by-category`, `expenses-by-category`, `reviews`; the feature-module pattern; the HTTP seam; `ApiError` |
| **New components** | `frontend/src/features/analytics/` — `useAnalytics.ts`, KPI cards, a daily chart, category breakdowns, a period comparison control |
| **Database** | none |
| **API** | none — no new endpoint, no changed contract |
| **ML** | none |
| **LLM** | none |
| **Security** | none beyond existing route protection; the view is member-scoped like every other |
| **Testing** | feature test against a mocked client; `architecture.node.test.ts`; loading, empty and error states; a test that the view requests only the five documented endpoints |
| **Docs** | README screenshot refreshed; the "eleven of twelve areas" statement becomes twelve |
| **Non-goals** | no new metric, no room-type breakdown, no backend change of any kind |
| **Depends on** | nothing |
| **Acceptance** | (1) `/analytics` renders real data for a member; (2) `BUILT_AREAS` in `AppRouter.tsx` contains every nav route and `PlaceholderPage` has no remaining consumer; (3) every figure shown is returned by an endpoint, none computed in the browser; (4) frontend suite green, typecheck clean |
| **Done when** | CI green, the README no longer describes Analytics as a placeholder, and a screenshot of the real view is committed |

### Stage 7.3 — Forecast performance API · *done*

> Both frozen services are now reachable over HTTP and the surface is **54 paths / 86
> operations**, exactly as planned. Two things came out differently from the plan above, both
> found while reading the code rather than decided in advance.
>
> **The `Page` envelope does not apply.** Neither endpoint returns a collection — each returns one
> measurement over one window — so paginating would have wrapped a single object in a pager that
> could only ever report one page. What the envelope was there to prevent, an unbounded read, is
> instead prevented by a **366-day maximum window**, which is the one new policy this stage
> introduces. It bounds the HTTP surface only: neither protocol gained a field, both checksums are
> unchanged, and a programmatic caller is as unbounded as before.
>
> **Two request rules had to be written, because neither frozen service validates its window** —
> they were never reachable from a client. A reversed window returned an *empty measurement* from
> both, which reads as "nothing happened" rather than "you asked wrongly"; it is now a 422, as is
> half a baseline pair. Both rules live in the new thin service, never in a router.
>
> Delivered: 2 routes, 1 service, 16 response models, 1 stage document, 141 tests, 0 migrations,
> 0 dependencies, 0 changes to either protocol or to the model artifact. The three tests asserting
> "this stage adds no endpoint" were restated as what they actually protected — that the frozen
> dataclasses are still not HTTP contracts and that the withheld digests appear in no published
> schema — which is a stronger claim than the path count it replaced.

| | |
|---|---|
| **Objective** | Expose `DemandAccuracyService.evaluate` and `DemandDistributionService.observe` over read-only, tenant-scoped HTTP |
| **Why it exists** | Both services are complete, frozen, protocol-checksummed and tested, and reachable only from tests. "Forecast vs actual" and "historical forecast performance" are a routing problem, not a modelling one |
| **V1 reused** | both services unchanged; `HotelScopeResolver`; `require_role`; the `ErrorResponse` contract; request-id middleware |
| **New components** | `api/v1/endpoints/ml_performance.py` (2 routes), `services/ml_performance.py` (validate, delegate, project), `schemas/ml_performance.py` (16 models) |
| **Database** | none — head still `0011_demand_prediction_public_id` |
| **API** | `GET …/ml/forecast-accuracy`, `GET …/ml/prediction-distribution`. Surface moved 52/84 → **54/86** |
| **ML** | none — no retraining, no artifact change, no protocol change, digest untouched |
| **Security** | manager role for accuracy (it exposes how wrong the model was, an operational judgement); membership alone for distribution, argued from the fact that every field it summarises is calendar arithmetic or a figure the same caller already reads from `/analytics/daily`; identical-404 for unknown and non-member |
| **Testing** | 98 contract/delegation/layering tests without a database; 43 against real PostgreSQL covering the real authorization chain, tenant isolation, request-id and that measuring writes nothing |
| **Docs** | [ml-forecast-performance-api.md](ml-forecast-performance-api.md); the API count updated in `architecture.md` and the two places in `README.md` |
| **Non-goals** | no threshold, no verdict, no alert, no drift *detection* — the protocols decide nothing and the endpoints do not imply otherwise |
| **Depends on** | nothing |
| **Acceptance** | (1) both endpoints return the services' own output unchanged — asserted by an AST walk proving the projection contains no arithmetic and imports neither pure-calculation module; (2) `accuracy_v1` and `distribution_v1` checksums identical to Stage 6.9/6.10; (3) every response carries the "no production accuracy established" statement in its payload; (4) no internal id and no digest in any response, asserted across every schema in the OpenAPI document |
| **Caveat** | a distribution window holding a single prediction publishes that prediction's own feature vector, since the summary of one observation is the observation. Stated in §5 of the stage document rather than glossed |

### Stage 7.4 — Forecast-vs-actual visualisation · *done*

> Built as a fifth section on the intelligence page, under its own heading and fetching its own
> data. Two things the plan above did not anticipate, both found by reading the contracts before
> writing any code, and both settled with the user rather than improvised.
>
> **Neither Stage 7.3 endpoint can draw a forecast-vs-actual chart.** `forecast-accuracy` returns
> aggregate error with no dates in it; `prediction-distribution` returns summary statistics with
> no actuals. That is deliberate and predates this stage — Stage 6.9 wrote down that "shipping
> the paired series would make it an export instead" — so the roadmap's acceptance criterion (1)
> assumed data that 7.3 was specified never to return. The chart is therefore built from two
> endpoints that already existed: `/analytics/daily` for the actual series and
> `/ml/demand-predictions` for the stored predictions. The actual line is the *same*
> `occupied_room_nights` definition the accuracy protocol scores against, so the picture and the
> measured error rest on one ground truth rather than two.
>
> **The frontend cannot know the caller's role.** `HotelResponse` carries none, `/auth/me`
> deliberately carries none, and `/hotels/{id}/members` — which would say — itself requires the
> manager role. So "do not issue the request for a viewer" is not expressible without a backend
> change. The accuracy request is issued and its **403 is treated as the answer**: the panel does
> not render, the reader is told the rest of the section is still theirs, and the server stays the
> only authority on access. This is the pattern every role-sensitive screen here already uses.
>
> A third thing worth recording: a `target_date` may legitimately carry more than one stored
> prediction, because `uq_demand_predictions_identity` includes `feature_digest`. Choosing
> between them is the accuracy protocol's rule (earliest `generated_at`, lowest `id`, executed by
> the database). **This screen does not reimplement it** — such days are drawn as a gap and the
> count is stated on screen, because picking silently would assert that one prediction existed
> when two did.
>
> Delivered: 1 feature module (6 files), 4 endpoints consumed, 91 new tests, 0 backend changes,
> 0 migrations, 0 dependencies. Frontend suite 1050 → 1141. Initial bundle unchanged at 306.55 kB
> — the section rides in the lazily-loaded intelligence chunk.

| | |
|---|---|
| **Objective** | Extend the intelligence view with measured forecast performance from 7.3 |
| **Why it exists** | 7.3 makes the data reachable; this makes it legible |
| **V1 reused** | `ForecastChart` unchanged (it already draws two never-blended series and omits the band when bounds are null); `PeriodSelector`; `StateMessage`; `describeFailure`; `formatCount`/`formatDate`; the `rangeFor` period helpers; the shared HTTP seam and `ApiError` |
| **New components** | `features/forecastPerformance/` — `useForecastPerformance.ts`, `pairing.ts`, `AccuracyPanel.tsx`, `DistributionPanel.tsx`, `ForecastPerformanceSection.tsx`, plus `types/mlPerformance.ts` |
| **Database / ML / LLM** | none |
| **API** | none added. Four consumed: `analytics/daily`, `ml/demand-predictions`, `ml/prediction-distribution`, `ml/forecast-accuracy`. Surface stays **54 / 86** |
| **Security** | the accuracy panel renders only when the API returns it; a 403 removes it and is shown as an ordinary status, not an alert. No client-side role check exists, because no role reaches the client |
| **Testing** | 44 feature tests + 47 source-level architecture tests; 5 existing intelligence tests updated for the new section, none weakened |
| **Docs** | this entry |
| **Non-goals** | forecasts and actuals are never blended; no confidence band; no combined segment metric; no threshold, verdict or the word "drift" anywhere on screen |
| **Depends on** | **7.3** |
| **Acceptance** | (1) actual and forecast are distinct by shape and by name, in the chart and in its table; (2) the settlement lag is in the panel body — a test asserts it is not in a `title` attribute; (3) the "not established" caveat is the first thing in the accuracy panel and the server's `statement` is rendered verbatim; (4) no metric is computed in the browser, asserted by a source scan that forbids `Math.`, `reduce`, `+=`, `parseFloat` and `toFixed` across the feature |
| **Known limitation** | the stored-prediction read is one page of 100. A window holding more discloses "showing N of M" rather than paginating; a longer history needs a shorter period |

---

## Track B — the generative platform

### Stage 7.5 — LLM provider abstraction · *done*

> The seam exists and nothing sits on it: no endpoint, no tool, no retrieval, no copilot, and no
> service or router imports `app.llm` at all — asserted, so that 7.6 cannot arrive early by
> accident. The application is unchanged from outside: the OpenAPI document is byte-identical
> with `llm_enabled` true and false, and the surface stays 54 / 86.
>
> **One piece of the specified architecture was deliberately not built.** §2 calls for "a
> timeout, a budget and a circuit-breaker **inside** the process (§5.7)". §5.7 specifies the
> first two and says nothing about the third — it is not in the failure table, and there is no
> threshold, window, open duration, half-open probe, reset rule or error code for a call refused
> while open. There was no contract to implement, so none was invented: a guessed threshold in
> the runtime path would be inherited by every later stage as though it had been decided. **This
> is the open specification gap of Track B and needs a decision before 7.6.**
>
> **Two smaller divergences from the documents, both recorded rather than resolved silently.**
> §5.6 names three doubles — `ScriptedModel`, `RecordedModel`, `FailingModel` — and the stage
> brief asked for "deterministic, failing, slow". Those overlap in two of three. All four were
> built, because they test different things: `FailingModel(LlmUnavailableError)` proves the error
> propagates, while only a call that genuinely hangs proves the **deadline is enforced**, which
> is what §5.7's timeout rule actually claims. Separately, §5.1 lists a tool catalogue on
> `ChatRequest`; tools are 7.6's, so the field is not there yet and the request has five fields,
> pinned by a test.
>
> **Failure mode 4 is declared but not exercised.** §5.7 row 4 is "tool raises", whose behaviour
> is a loop rule rather than a status, and this stage introduces no tools for anything to raise
> inside. `LlmToolFailedError` exists so the taxonomy is whole; a test asserts it is declared and
> states that its status is a placeholder for the stage that adds tools.
>
> Delivered: 10 new modules, 1 optional dependency pinned in its own file, 1 prompt, 6 failure
> types, 4 doubles, 132 tests. Backend 5152 → 5284. Frontend untouched at 1141.

| | |
|---|---|
| **Objective** | The `ChatModel` protocol, one provider adapter, settings, prompt records, and the test doubles — with no product feature on top |
| **Why it exists** | Every later stage depends on it. Building it alone means the abstraction is designed against its contract, not bent around the first feature |
| **V1 reused** | `Settings`; the `AppError` contract and its one `ErrorResponse` envelope; the `accuracy_v1` checksum mechanism, applied to prompts; `RequestIdFilter` correlation |
| **New components** | `app/llm/` — `base.py`, `errors.py`, `boundary.py`, `factory.py`, `testing.py`, `prompts/registry.py`, `providers/anthropic_provider.py`; `backend/requirements-llm.txt` |
| **Database** | none — no migration, no table, and the package imports no session, no repository and no SQLAlchemy |
| **API** | none. The seam is exercised from tests only |
| **LLM** | the abstraction; enforced timeout, one retry with jitter, per-request budget, structured-output validation, and five of the six failure modes of §5.7 |
| **Security** | the key is read from `Settings`, handed to the client and never logged, never in an error, never in a response — asserted. No session, SQL, row, credential or tenant identifier can cross the seam: there is no field on `ChatRequest` for one, and a test pins the field set |
| **Testing** | the failure matrix; adapter translation against a stand-in with the SDK's shape; a socket-disabling test proving no network call; a repo-wide AST scan proving the SDK is imported in **exactly one** module; tests that no service and no router reaches `app.llm` |
| **Docs** | this entry. The design is in the module docstrings, which carry the reasoning at the code they govern |
| **Non-goals** | no endpoint, no tool, no copilot, no RAG, no embeddings, no agent, no user-visible change — each asserted by a source scan over the package |
| **Depends on** | nothing |
| **Acceptance** | (1) `llm_enabled=false` leaves the app fully functional — the OpenAPI document is byte-identical either way; (2) each of the five failures §5.7 gives a status to produces its documented code, and the sixth is declared; (3) prompts carry id, version and a SHA-256 checksum reproducible by hand; (4) no test opens a socket, asserted by disabling them; (5) one dependency added, pinned, and installed by neither CI nor the image |
| **Known limitations** | **no circuit breaker** (§2 requires one, §5.7 does not specify it — see above); failure mode 4 declared but not exercised, since no tool exists to raise; the enforced deadline abandons a hung call rather than killing it, because a Python thread cannot be killed — that bounds the caller's latency and leaks a thread for the duration; the live provider is not exercised by any test, so nothing here establishes that the vendor behaves as documented |
| **Done when** | CI green with the provider SDK absent and no credential set |

### Stage 7.6 — Tool boundary · *done*

> Built as specified by the Stage 7.6 brief, which widened the plan below in three ways the
> original entry did not anticipate, and narrowed it in one. Every divergence from the documents
> is recorded in architecture **Amendment A1** rather than resolved silently.
>
> **Widened by the brief.** (1) The circuit breaker 7.5 could not build is decided and built
> (§5.8): 5 availability failures in a rolling 60 s open it for 30 s, then one probe; while open
> the provider is never reached and the caller gets `503 LLM_UNAVAILABLE`. (2) The **bounded tool
> loop and failure mode 4** — planned here as "no LLM involvement" and implicitly 7.7's — are
> built in 7.6: 3 rounds, 4 calls per round, the first tool failure returned to the model flagged
> as an error, the second ending the loop with a labelled partial result. (3) `ChatRequest` now
> carries the deterministic tool catalogue §5.1 lists, and the adapter translates tool use.
>
> **Narrowed: five tools, not six.** `search_hotel_knowledge` delegates to a `KnowledgeService`
> that only Stage 7.9 builds; a tool over a missing service would have had to invent business
> logic. It is deferred to after 7.9 — a decision taken with the user, not by default.
>
> **Found in the code, not in the plan.** §7.2 named `AnalyticsService.revenue_by_category`,
> which does not exist (the service method is `revenue_breakdown`). `get_forecast_accuracy`
> delegates to the Stage 7.3 `ForecastPerformanceService`, not to `DemandAccuracyService.evaluate`,
> because the raw evaluation carries the digests §7.4 forbids a tool to return. And
> `get_demand_forecast` is **not strictly read-only**: its service records every prediction it
> serves (Stage 6.8). With the user's agreement it keeps that behaviour as a **declared side
> effect** — idempotent, no business record touched — and every other tool is asserted to write
> nothing.
>
> **The planned `copilot_tool_invocations` table was not built.** The brief required the
> existing append-only trail and forbade a second audit system, so invocations are
> `tool.invoked` events on `audit_events`. That needed the smallest schema change possible:
> migration **0012** widens the two closed vocabulary CHECKs by one value each, and touches
> nothing else. Head `0011` → **`0012_audit_tool_invoked`**.
>
> Delivered: `app/copilot/` (contracts, registry, catalogue, loop, 5 tool modules),
> `app/services/tool_invocation.py`, `app/llm/circuit.py`, 1 migration, 0 dependencies,
> 0 endpoints (surface stays **54 / 86**), 235 new tests. Backend 5284 → 5519.
> Frontend untouched at 1141.

| | |
|---|---|
| **Objective** | The registry, the contracts and the read-only tools of architecture §7.2, the bounded loop that lets a model call them, and the circuit breaker 7.5 left undecided |
| **Why it exists** | The tools are the security boundary. They are built and tested before anything non-deterministic can call them in production |
| **V1 reused** | `AnalyticsService`, `DemandPredictionService`, `ForecastPerformanceService` (7.3), `HotelScopeResolver`, `AuditTrail` and its append-only table, the `AppError` envelope; 7.5's seam, boundary and doubles |
| **New components** | `app/copilot/contracts.py`, `registry.py`, `catalogue.py`, `loop.py`, `tools/` (5 modules); `app/services/tool_invocation.py`; `app/llm/circuit.py` |
| **Database** | migration `0012_audit_tool_invoked`: `tool.invoked` and `tool` added to the two closed audit vocabularies. No table, column, index or trigger change |
| **API** | none. The OpenAPI document gains one enum value (`tool.invoked` in the audit-history `action` filter); paths and operations unchanged at 54 / 86 |
| **Security** | the hotel is a parameter of the invocation service, never of a tool: every input model forbids unknown keys, and the registry refuses at import any schema naming a hotel, tenant, property, user or `*_id`. Role is checked before arguments are parsed and before any service runs; the model is offered only the tools the caller's role permits, and a name it was not offered is unknown. Every call that reaches a resolved hotel is audited — no question, answer, prompt, argument or output in the event |
| **Testing** | a unit suite over the breaker (injected clock), the seam and adapter, the registry, the five contracts, the catalogue, every loop termination and the invocation service's ordering; the brief's security tests A–M; and a real-PostgreSQL suite with two hotels and four callers — including model output that names hotel B in an argument, a date, a tool name and the question |
| **Docs** | architecture Amendment A1 (§4.3, §4.4, §5.4, §5.7, §5.8, §7.2, §7.3); this entry; module docstrings carrying each tool's §7.3 template |
| **Non-goals** | no endpoint, no `CopilotService`, no prompt for a copilot, no conversation, no retrieval, no embeddings, no recommendation, no agent framework, no demand-model change — each asserted |
| **Depends on** | **7.3** (for `get_forecast_accuracy`), **7.5** |
| **Acceptance** | (1) five tools, each delegating to one existing service method and adding no business logic — asserted per tool by source scan; (2) no tool schema contains a tenant identifier, at any depth, input or output; (3) a non-member gets the hotel's 404 through the invocation service, unaudited; (4) each call writes exactly one audit row with no question text — asserted with planted sentinels |
| **Known limitations** | the breaker is per process, so each worker learns an outage separately (at most 5 failed calls each); `LLM_TOOL_FAILED` stays declared and unraised — FM4 yields a labelled partial result, and how an endpoint presents one is 7.7's decision; the forecast tool's recorded prediction commits before its audit event, so an audit failure after a successful forecast leaves the prediction recorded (and no result reaches the model); `search_hotel_knowledge` deferred |
| **Done when** | CI green, migration verified on a disposable database first |

### Stage 7.7 — Copilot, single turn · *done*

> Built as planned, with every decision the pre-inspection raised taken by the user before any
> code was written, and recorded as architecture **Amendment A2**.
>
> **One question, one labelled answer.** `POST …/copilot/ask` authorizes the caller (any
> member), charges the per-actor then per-hotel hourly allowance (20 / 100, one shared window,
> `429 LLM_BUDGET_EXHAUSTED` with `Retry-After`), and hands the question to `CopilotService`,
> which composes Stage 7.6 unchanged: permitted tools → deterministic catalogue →
> `copilot_answer@v1` → the bounded loop, its executor bound to the path hotel. A bounded stop is
> a **200 with `complete: false`**, a stop reason and a fixed notice; only a model failure before
> any tool ran is re-raised with §5.7's status. The ordering "authorized before charged" is a
> property of the dependency graph -- the budget dependency depends on the role dependency --
> and a mutation test proves both halves of it are pinned.
>
> **Acceptance (2) is enforced, not requested.** The prompt asks for figures only from tool
> results; `app.copilot.grounding` then checks every number in the answer against the numbers the
> tools returned (rounded to the written precision, or a returned fraction written as a
> percentage). An answer carrying any other number is **withheld** and labelled
> `ungrounded_figures` -- including another hotel's figure, which the integration suite plants.
> What this cannot prove is that a figure is attached to the right label; that is Stage 7.8's.
> **Acceptance (3)** -- decline rather than guess -- is instructed by the prompt and backstopped by
> the figure check, but whether a real model declines is a behavioural claim no scripted test can
> make; it is the first thing the evaluation harness should measure.
>
> **One table, content-free.** `llm_invocations` (migration **0013**) records the actor, hotel,
> prompt identity, the provider and model the request was routed to, the stop reason, counts,
> tokens, latency and the request id -- never the question, the answer, the prompt text or any
> tool output. Append-only by trigger; its CHECKs describe the data, not the loop's limits.
> Retention is deferred. Head `0012` → **`0013_llm_invocations`**; 23 application tables.
>
> **Two corrections to earlier stages, made rather than papered over.** Stage 7.5's
> `ChatResponse` docstring claimed a test stopped services reading `.provider` / `.model`; none
> existed, and now one does. And six "no endpoint yet" guards from 7.5 and 7.6 were restated to
> what they protect now (only the copilot service reaches `app.llm`; only `deps.py` among API
> modules; no router reaches the tool machinery) rather than deleted.
>
> Delivered: 1 route, `CopilotService`, `LlmInvocationLog` + repository + model, the figure
> check, 1 prompt, 1 migration, 3 settings, 0 dependencies. Surface 54 / 86 → **55 / 87**.
> 154 new tests; backend 5519 → 5673. Frontend untouched at 1141. The design
> is in the module docstrings and Amendment A2 §7.5 rather than a separate document.

| | |
|---|---|
| **Objective** | One endpoint: a question in, a grounded answer out, using 7.6's tools and 7.5's abstraction. Stateless, no memory, no retrieval |
| **Why it exists** | The smallest thing that is actually the product. Retrieval and memory are separable and each carries its own risk |
| **V1 reused** | scope resolver, error contract, rate limiter, audit; from 7.6 the registry, catalogue, `ToolInvocationService` and the bounded `ToolLoop` — 7.7 wires them, it does not rebuild them |
| **New components** | `app/services/copilot.py`; one router; request/response schemas; `llm_invocations` table |
| **Database** | `llm_invocations` — migration `0013_llm_invocations`, one append-only table, no existing table touched |
| **API** | `POST …/copilot/ask`. Surface 54/86 → **55/87** |
| **Security** | the whole of architecture §4: the model never names a hotel; tools re-check; per-actor and per-hotel budgets; refusals are typed |
| **Testing** | orchestration against `ScriptedModel`; the bounded tool loop; budget exhaustion; every failure mode; **a test that an answer containing a figure absent from tool results fails** |
| **Docs** | architecture Amendment A2 (§4.1, §4.4, §4.5, §5.1, §5.7, new §7.5); module docstrings; this entry |
| **Non-goals** | no memory, no retrieval, no writing, no streaming |
| **Depends on** | **7.5, 7.6** |
| **Acceptance** | (1) the answer cites which tools ran; (2) tool results are the only source of figures; (3) a question outside the tool set is declined, not guessed; (4) `llm_enabled=false` → clean 503; (5) no prompt, provider name or key in any response or log |
| **Done when** | CI green with a scripted model; no live provider needed in CI |
| **Known limitations** | budgets are per worker process; a malformed body from an authorized caller is charged before the 422, because the budget runs as a route dependency; the figure check proves existence, not correct labelling; token counts cover only model calls that returned; no retention rule for `llm_invocations`; the live provider is exercised by no test |

### Stage 7.8 — Evaluation harness · *done*

> **The harness exists; no model has been evaluated yet, and the harness says so.** No live
> provider has ever been called from this repository and CI has neither the SDK nor a key, so
> every exchange CI can replay was written by hand. The stage was built around that fact rather
> than despite it, with the user's agreement: CI replays hand-written **reference** exchanges
> through the production copilot stack and fails the build if the pinned report moves -- a gate
> on the pipeline and the scorers, labelled "No language model was evaluated" in the report
> itself. The first real evaluation is an opt-in live capture (`scripts/copilot_live_eval.py`,
> paid, refuses to start without `--confirm-paid-api-call`) that records in the same format CI
> replays.
>
> **Real stack, fixed data.** Each case runs through `CopilotService`, `ToolLoop` and
> `ToolInvocationService` -- registry, argument and output validation, the role check, the figure
> check -- with only the three data services (a fictional 20-room hotel built through the
> production schemas), the membership lookup, the two write sinks and the model replaced. No
> database, no network (asserted by disabling sockets).
>
> **Two design points worth recording.** The grounding scorer is deliberately independent of the
> copilot's own figure check -- same rule, different implementation, no shared import -- and the
> two are compared on every case (`scorer_agreement`). And the report records the catalogue each
> case was *offered*, because a mutation test showed that without it a change offering viewers the
> manager-only tool would not move the pinned report. Seven mutations -- three in the scorers,
> four in production code -- each fail the suite by name.
>
> **Not built, by necessity:** citation validity and retrieval quality (nothing to cite until
> 7.9/7.10); label correctness (not mechanical); latency and cost in replay (meaningless). The
> prompt gives the model no current date, so every question names its dates.
>
> Delivered: `tests/evaluation/` (question set, fixture hotel, replay, scorers, harness, 24
> reference exchanges, pinned report), the live capture script, [copilot-evaluation.md](copilot-evaluation.md),
> Amendment A3. 0 migrations, 0 API changes, 0 production code changes, 0 dependencies.
> 92 new tests; backend 5673 → 5765. Frontend untouched at 1141.

| | |
|---|---|
| **Objective** | The fixed question set and the scoring described in architecture §9, run in CI against recorded exchanges |
| **Why it exists** | The repository's own V2 backlog says the harness is required **before** any generative capability is claimed. This stage is where that promise is kept — before the copilot reaches a user interface |
| **V1 reused** | the test infrastructure; the frozen-protocol pattern from 6.9/6.10 |
| **New components** | `tests/evaluation/` — question set, expected tool calls, scorers for tool selection, grounding and refusal |
| **Database / API / Frontend** | none |
| **Security** | none |
| **Testing** | the harness *is* the test; plus tests of the scorers themselves |
| **Docs** | an evaluation document stating what is measured, the set size, the prompt version, and **what the result does not mean** |
| **Non-goals** | no aggregate "accuracy" figure for the copilot; no claim about product usefulness |
| **Depends on** | **7.7** |
| **Acceptance** | (1) grounding is scored mechanically, not by judgement; (2) the result names its question set, prompt version and date; (3) a hallucinated figure is detected by the harness in a deliberate negative test |
| **Done when** | CI runs it on every push and a regression fails the build |

### Stage 7.9 — Hotel knowledge documents · *done*

> **Built as specified, with four decisions taken with the user** (architecture Amendment A4,
> [knowledge-documents.md](knowledge-documents.md)): withdrawal keeps the rows and leaves
> retrieval through the search's WHERE clause; the audit vocabulary widened in 0014 by three
> `document.*` actions and the `document` resource; no guest personal data is an attestation
> (`contains_no_guest_personal_data: true`) plus a stated policy; and each document carries its own
> text-search language, one of seven.
>
> **Migration `0014_hotel_documents`**: `hotel_documents` (one row per immutable version; a
> composite `(supersedes_id, hotel_id)` foreign key so a version can supersede only its own
> hotel's; a unique `supersedes_id` so two concurrent re-uploads cannot both win) and
> `hotel_document_chunks` (append-only; GIN index on `search_vector`). Head `0013` →
> **`0014_hotel_documents`**; 25 application tables.
>
> **Six operations**, surface **55/87 → 60/93**: upload, list, read one version with its chunks,
> new version, withdrawal (manager for the three writes), and `GET …/knowledge/search` (any
> member; `q` ≤ 200 characters, `limit` 1–20, default 5). The planned "~60/92" became 93 because
> reading one version is its own operation — it is how a withdrawn version's citation is
> explained.
>
> **Retrieval**: `hotel_id` and `status = 'active'` in the same WHERE clause as the
> `websearch_to_tsquery` match; ordered by `ts_rank_cd`, then document, then ordinal. The score
> itself is not returned — comparable only within one search, and the project keeps floats out of
> repositories. Chunking lives in a pure module, `app/knowledge/chunking.py`.
>
> **Not built**: the `search_hotel_knowledge` tool, citations, retrieval-quality measurement
> (all 7.10); hard deletion; retention of documents.
>
> Delivered: models, repository, service, schemas, routes, migration, the design document,
> Amendment A4. 0 dependencies, 0 frontend changes. 113 new tests; backend 5765 → 5878. Frontend untouched at
> 1141.

| | |
|---|---|
| **Objective** | Ingest, version, chunk and retrieve hotel documents with PostgreSQL full-text search. No LLM involvement |
| **Why it exists** | Retrieval is separable from generation and must be correct on its own before an answer depends on it |
| **V1 reused** | scope resolver; `Page` envelope; audit trail |
| **New components** | `app/services/knowledge.py`; a repository; models; upload, list, withdraw and search routes |
| **Database** | `hotel_documents`, `hotel_document_chunks` — one migration, GIN index on the FTS column |
| **API** | document CRUD and a search endpoint. Surface **55/87 → ~60/92** |
| **Security** | tenant filter in the query, not after it; documents are hotel-owned; upload requires manager; **no guest personal data** accepted, stated and reviewed |
| **Testing** | two hotels with identical documents never see each other's chunks; versioning supersedes; withdrawal removes from retrieval but preserves citation identity; chunk ordinals stable |
| **Docs** | the knowledge design document, including the versioning and deletion policy |
| **Non-goals** | no embeddings, no vector column, no LLM, no automatic ingestion from external sources |
| **Depends on** | nothing in Track B (can precede 7.5) |
| **Acceptance** | (1) retrieval bounded by `hotel_id` in the SQL; (2) a re-uploaded document creates a version and supersedes cleanly; (3) every chunk is citable by `public_id`; (4) no internal id in any response |
| **Done when** | CI green, migration verified on a disposable database first |

### Stage 7.10 — Grounded retrieval answers

| | |
|---|---|
| **Objective** | Add `search_hotel_knowledge` to the tool set and make the copilot cite its sources |
| **Why it exists** | Joins 7.9's retrieval to 7.7's orchestration under the citation contract |
| **New components** | the retrieval tool; citation schema; the untrusted-content prompt block |
| **Database** | none |
| **API** | the copilot response gains a `citations` array — an additive change to a V2 endpoint only |
| **Security** | retrieved text is delimited and labelled untrusted; the structural defence of §4.2 is the one relied upon |
| **Testing** | **an injected instruction inside a document does not change which hotel is read** — the test that matters; an answer with no citation returns "not found"; cited chunks belong to the caller's hotel |
| **Docs** | the grounding contract |
| **Non-goals** | no web retrieval, no cross-tenant corpus |
| **Depends on** | **7.7, 7.8, 7.9** |
| **Acceptance** | (1) every retrieval answer cites chunk and document `public_id`; (2) an uncited answer is refused; (3) the injection test passes; (4) the harness gains retrieval and citation scoring |
| **Done when** | CI green and the harness measures citation validity |

### Stage 7.11 — Conversations

| | |
|---|---|
| **Objective** | Multi-turn memory, scoped to a hotel and an actor |
| **Why it exists** | Deferred deliberately to here: it introduces stored free text, a retention question and a context-window budget, none of which should complicate the first copilot |
| **Database** | `copilot_conversations`, `copilot_messages` — one migration, with a stated retention rule |
| **API** | conversation create/list/continue. Surface grows by ~3 operations |
| **Security** | a conversation is readable only by its creator within its hotel; retention documented; message text never enters the audit trail |
| **Testing** | another member cannot read a conversation; context truncation is deterministic; deletion removes messages |
| **Non-goals** | no cross-hotel conversation, no sharing, no unbounded history |
| **Depends on** | **7.7** |
| **Acceptance** | (1) turn N sees turns 1…N−1 and nothing else; (2) context truncation is explicit and tested; (3) retention is enforced, not merely documented |

### Stage 7.12 — Recommendations

| | |
|---|---|
| **Objective** | Structured, actionable insights over existing analytics and forecast data |
| **Why it exists** | The recorded V1 backlog says recommendations must be *"evaluated with ranking metrics against a popularity baseline"* — so this stage carries a measurement obligation from the start |
| **V1 reused** | `AnalyticsService`, `IntelligenceService` (anomalies, trend, insights), the demand model |
| **New components** | `app/services/insight.py`; a structured-output schema |
| **Database** | none initially — insights are computed, not stored, following the Stage 6.9/6.10 precedent |
| **Security** | read-only; no action is taken on the hotel's behalf |
| **Testing** | every recommendation traces to a figure from a deterministic service; a baseline comparison exists |
| **Non-goals** | no automated pricing action, no autonomous behaviour, no claim of business value |
| **Depends on** | **7.7, 7.8** |
| **Acceptance** | (1) each insight names the measure that produced it; (2) a baseline is defined and measured against; (3) nothing is phrased as advice the platform cannot support |

### Stage 7.13 — Copilot front end

| | |
|---|---|
| **Objective** | The question surface, the answer, the visible tool calls, the citations |
| **New components** | `frontend/src/features/copilot/` following the established pattern |
| **Security** | role-aware tool availability; refusal states rendered as explanations |
| **Testing** | feature tests for answer, citation, refusal, disabled and budget-exhausted states |
| **Non-goals** | no redesign of the shell or navigation |
| **Depends on** | **7.7**, and **7.10** for citations |
| **Acceptance** | (1) every answer is labelled generated; (2) citations open their source text; (3) tool calls are visible, not hidden; (4) a viewer is not offered manager-only tools |

### Stage 7.14 — Multi-horizon forecasting *(ML track, independent)*

| | |
|---|---|
| **Objective** | Forecast beyond the single 7-day horizon |
| **Why it exists** | The recorded backlog is precise: *"the 7-day horizon admits 9 of 15 contract columns; a horizon-matched on-the-books and rolling-window feature set does not exist."* This is a dataset and modelling stage, not a serving one |
| **V1 reused** | the Stage 6.1 feature contract, the 6.3 rolling-origin protocol, the 6.4 acceptance policy |
| **New components** | horizon-matched features; a model per horizon or a horizon-aware model; a second registry entry |
| **Database** | possibly a horizon column on stored predictions — a migration on `0011` |
| **ML** | new training, new backtest, new acceptance run. **A new canonical digest** — the V1 artifact is untouched and keeps its own |
| **Security** | none new |
| **Testing** | the full 6.3/6.4 discipline: chronological splits, leakage checks, metric-blind acceptance |
| **Non-goals** | **no confidence intervals unless the protocol produces them and the backtest measures their coverage.** The brief asks for "appropriate uncertainty representation *if scientifically supportable*" — for a gradient-boosted point forecaster it is not supportable without quantile regression or conformal prediction, and either is its own stage with its own measurement |
| **Depends on** | **7.3** (so performance is measurable before a second model exists) |
| **Acceptance** | (1) each horizon has its own measured backtest; (2) acceptance is metric-blind, as in 6.4; (3) the V1 model's digest and claims are untouched; (4) no accuracy claim beyond what is measured |

### Stage 7.15 — pgvector *(conditional)*

| | |
|---|---|
| **Objective** | Semantic retrieval, **only if** 7.8's retrieval measurement shows lexical search is insufficient |
| **Why it exists conditionally** | The pinned `postgres:18.6-alpine` image does not ship `vector` — verified by inspecting the image. Adopting it replaces a digest-pinned official base image and touches the image-reproducibility job. That cost should be paid against evidence |
| **Trigger** | recall at the working cut-off below the threshold 7.8 declares **in advance** |
| **Database** | an embeddings column or table; an HNSW or IVFFlat index; a re-indexing procedure |
| **Non-goals** | no dedicated vector database — at this corpus size it would add a second datastore, a second backup story and a second tenant boundary to replace a WHERE clause |
| **Acceptance** | (1) the measurement that triggered it is recorded; (2) the new image is digest-pinned; (3) reproducibility stays green; (4) retrieval quality is re-measured and the improvement stated with its method |

---

## Dependency summary

| Stage | Depends on | May run in parallel with |
|---|---|---|
| 7.2 | — | all of Track B |
| 7.3 | — | 7.5, 7.9 |
| 7.4 | 7.3 | Track B |
| 7.5 | — | 7.2, 7.3, 7.9 |
| 7.6 | 7.3, 7.5 | 7.9 |
| 7.7 | 7.5, 7.6 | 7.9 |
| 7.8 | 7.7 | — |
| 7.9 | — | 7.2–7.7 |
| 7.10 | 7.7, 7.8, 7.9 | — |
| 7.11 | 7.7 | 7.10, 7.12 |
| 7.12 | 7.7, 7.8 | 7.10, 7.11 |
| 7.13 | 7.7 (+7.10 for citations) | 7.11, 7.12 |
| 7.14 | 7.3 | all of Track B |
| 7.15 | 7.8 measurement | — |

## Definition of Done — applies to every stage

1. CI green on all four jobs.
2. Ruff, format and mypy clean.
3. Any migration verified on a disposable database **before** the code that uses it is written.
4. Tenant isolation tested against real PostgreSQL, with the identical-404 property preserved.
5. No internal `BIGINT` identifier in any response, log, error or documentation example.
6. A stage document recording what was built, what was deliberately not, and the limitations.
7. The claims boundary intact: nothing asserts accuracy, reliability or business value that was
   not measured under a declared protocol.
8. V1 contracts unchanged, or a versioned extension explicitly argued in the stage document.
