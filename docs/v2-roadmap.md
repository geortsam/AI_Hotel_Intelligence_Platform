# V2 stage roadmap — definition, Stage 7.1

> The ordered plan produced by Stage 7.1. Each stage is specified well enough to be implemented
> on its own, in order, without re-deciding anything.
>
> **Implemented so far: Stage 7.2.** Every other stage below is a specification and nothing more —
> none of its code exists. A stage carries `· *done*` in its heading once it ships, with a note
> recording what was actually built and where that differed from the plan.
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

### Stage 7.4 — Forecast-vs-actual visualisation

| | |
|---|---|
| **Objective** | Extend the intelligence view with measured forecast performance from 7.3 |
| **Why it exists** | 7.3 makes the data reachable; this makes it legible |
| **V1 reused** | the intelligence feature module and its chart components |
| **New components** | an accuracy panel; a forecast-vs-actual chart |
| **Database / API / ML / LLM** | none |
| **Security** | the accuracy panel is hidden from viewers, matching 7.3's role requirement |
| **Testing** | feature tests including the viewer-cannot-see case |
| **Docs** | screenshot |
| **Non-goals** | forecasts and actuals are never blended into one series; no confidence band is drawn that the protocol does not produce |
| **Depends on** | **7.3** |
| **Acceptance** | (1) actual and forecast are visually distinct; (2) the settlement lag is stated on screen; (3) the "not established" caveat is visible, not buried in a tooltip |
| **Done when** | CI green and the claim boundary survives a reading of the rendered screen |

---

## Track B — the generative platform

### Stage 7.5 — LLM provider abstraction

| | |
|---|---|
| **Objective** | The `ChatModel` protocol, one provider adapter, settings, prompt records, and the three test doubles — with no product feature on top |
| **Why it exists** | Every later stage depends on it. Building it alone means the abstraction is designed against its contract, not bent around the first feature |
| **V1 reused** | `Settings`; `FixedWindowRateLimiter`; the error contract; request-id correlation |
| **New components** | `app/llm/` — `base.py`, `providers/`, `prompts/`, `testing.py` |
| **Database** | none |
| **API** | none |
| **LLM** | the abstraction itself; timeouts, retries, budgets and the six failure modes of architecture §5.7 |
| **Security** | the API key is read from the environment and never logged, never in an error, never in a response |
| **Testing** | the full failure matrix against `FailingModel`; a provider contract suite; **a test asserting no test makes a network call**; a test that no `app/services/` module imports a provider SDK |
| **Docs** | an LLM design document: provider abstraction, prompt versioning, budgets, failure behaviour |
| **Non-goals** | no endpoint, no tool, no copilot, no user-visible change |
| **Depends on** | nothing |
| **Acceptance** | (1) `llm_enabled=false` leaves the app fully functional; (2) every failure in §5.7 produces its documented code; (3) prompts are versioned and checksummed; (4) CI makes no network call; (5) one dependency added, pinned |
| **Done when** | CI green with the provider unreachable |

### Stage 7.6 — Tool boundary

| | |
|---|---|
| **Objective** | The registry, the contracts and the six read-only tools of architecture §7.2, callable programmatically — not yet by a model |
| **Why it exists** | The tools are the security boundary. They are built and tested before anything non-deterministic can call them |
| **V1 reused** | `AnalyticsService`, `DemandPredictionService`, `DemandAccuracyService`, `HotelScopeResolver`, the audit trail |
| **New components** | `app/copilot/registry.py`, `contracts.py`, `tools/` |
| **Database** | `copilot_tool_invocations` — one migration on `0011` |
| **API** | none |
| **Security** | every tool re-asserts authorization; no tool accepts a tenant identifier; every invocation audited |
| **Testing** | per tool: schema, role, tenant isolation, withheld fields, error behaviour, audit row. A registry test that **every** registered tool declares a role and is read-only |
| **Docs** | the tool catalogue, one section per tool using the §7.3 template |
| **Non-goals** | no LLM involvement; no writing tool; no room-type tool (the analytics repository does not support it yet) |
| **Depends on** | **7.3** (for `get_forecast_accuracy`) |
| **Acceptance** | (1) six tools, each delegating to an existing service and adding no business logic; (2) no tool signature contains a hotel identifier; (3) a non-member gets the identical 404 through every tool; (4) each call writes exactly one audit row containing no question text |
| **Done when** | CI green, migration verified on a disposable database first |

### Stage 7.7 — Copilot, single turn

| | |
|---|---|
| **Objective** | One endpoint: a question in, a grounded answer out, using 7.6's tools and 7.5's abstraction. Stateless, no memory, no retrieval |
| **Why it exists** | The smallest thing that is actually the product. Retrieval and memory are separable and each carries its own risk |
| **V1 reused** | scope resolver, error contract, rate limiter, audit |
| **New components** | `app/services/copilot.py`; one router; request/response schemas; `llm_invocations` table |
| **Database** | `llm_invocations` — one migration |
| **API** | `POST …/copilot/ask`. Surface 54/86 → **55/87** |
| **Security** | the whole of architecture §4: the model never names a hotel; tools re-check; per-actor and per-hotel budgets; refusals are typed |
| **Testing** | orchestration against `ScriptedModel`; the bounded tool loop; budget exhaustion; every failure mode; **a test that an answer containing a figure absent from tool results fails** |
| **Docs** | the copilot design document and its claims boundary |
| **Non-goals** | no memory, no retrieval, no writing, no streaming |
| **Depends on** | **7.5, 7.6** |
| **Acceptance** | (1) the answer cites which tools ran; (2) tool results are the only source of figures; (3) a question outside the tool set is declined, not guessed; (4) `llm_enabled=false` → clean 503; (5) no prompt, provider name or key in any response or log |
| **Done when** | CI green with a scripted model; no live provider needed in CI |

### Stage 7.8 — Evaluation harness

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

### Stage 7.9 — Hotel knowledge documents

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
| 7.6 | 7.3 | 7.9 |
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
