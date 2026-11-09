# Layered retrieval evaluation — decision record (R2.10C.1)

> **Status.** Architecture approved: Option C, a layered retrieval evaluation (2026-11-08). This
> record is the declaration the later stages are held to. **Nothing in it is implemented yet**, and
> nothing in it changes the application, the prompts, retrieval, an existing measure, or any frozen
> artifact. It is written from the read-only R2.10C assessment; where that assessment did not
> settle a detail, the detail is marked **Unresolved** below instead of being decided here.
>
> **Measured versus predicted.** Every figure marked *measured* was produced by code and is
> pinned in the repository. Every statement marked *predicted* is reasoning about PostgreSQL's text
> search configuration that no run has confirmed. Stage C.4 is where predictions are replaced by
> measurements; until then none of them is evidence.

## 1. The problem

### 1.1 Two different searches answer two different questions

The copilot finds hotel documents through one tool, `search_hotel_knowledge`, which passes the
model's own query string, unchanged, to `KnowledgeService.search` and from there to
`KnowledgeRepository.search` (`backend/app/repositories/knowledge.py`). The server does not
rewrite queries. Production retrieval is:

| Aspect | Production (`KnowledgeRepository.search`) |
|---|---|
| Match | `chunk.search_vector @@ websearch_to_tsquery(document.language::regconfig, :q)` |
| Term logic | **every** term must match (`websearch_to_tsquery` joins plain words with AND) |
| Normalisation | the document's own text-search configuration: stemming and stop words per language |
| What is indexed | the **chunk text only** (`to_tsvector(config, draft.text)`); the title is not |
| Scope | the request's hotel, `status = 'active'`, in the same WHERE clause |
| Order | `ts_rank_cd` descending, then document id, then chunk ordinal (a total order) |
| Bound | `LIMIT :limit`, default 5 |

The R2.9Y answer evaluation (`copilot_knowledge_eval_v1`) did **not** use it. Its search is
`FixtureKnowledge` (`tests/evaluation/fixture_documents.py`), a stand-in whose own docstring says it
does not evaluate retrieval:

| Aspect | Stand-in (`FixtureKnowledge`) |
|---|---|
| Term logic | **any** shared word (OR): scored by how many query words a chunk shares |
| Normalisation | lower-cased words of 3+ letters, a short stop list, **no stemming** (`dog` ≠ `dogs`) |
| What is matched | the **title and** the text |
| Order | overlap count, then corpus position |

The two are not equivalent, and the difference is not in one direction. OR matching finds chunks
that production's AND matching cannot (`pool open hours` shares `pool` and `open` with the pool
chunk, which never says `hours`). Missing stemming loses matches that production finds (`dog`
against `Dogs`). Matching the title finds chunks whose text never contains the word (`FAQ`). So no
R2.9Y result says what production retrieval would have returned for the same query.

### 1.2 What has been measured

**`knowledge_retrieval_v1`, real PostgreSQL** (*measured*; `tests/evaluation/retrieval_report.json`,
produced by `tests/integration/test_knowledge_retrieval_eval.py` through the real upload and search
routes). 10 documents (8 English, 2 Greek), 19 chunks and 24 queries, all written by the developer:

- recall@5 (as v1 defines it: at least one expected chunk in the top 5) = **0.6957, 16 of 23**,
  against the 0.90 threshold declared before the first measurement;
- all 14 shared-word queries and both Greek queries are found;
- **all 7 paraphrase queries miss, and every one of them returns zero results**: r02
  `pool opening hours`, r07 `check-in time`, r09 `luggage storage`, r10 `parking price`, r12
  `electric car charging`, r13 `pet policy`, r15 `wifi password`. The failure is the all-terms rule,
  not ranking. `pet policy` is the example query in the search tool's own description;
- the no-answer query (r22, `gym opening times`) correctly returns nothing;
- the 95% Wilson interval for 16 of 23 is about **0.49 to 0.84**. Its upper end is below 0.90, so
  the paraphrase weakness is clear on this set. 0.90 on 23 queries needs at least 21 hits.

What this measures: the production retrieval path on an invented corpus with invented queries. What
it does not: production performance on real documents or on queries a model writes.

**R2.9Y, stand-in search** (*measured*, for the stand-in only; `tests/evaluation/fixtures/
r29y_knowledge_capture.json` and the titles pinned in `tests/evaluation/test_r29y_regressions.py`).
Ten of the eleven cases searched, three attempts under each of two prompts: **60 search calls, 11
distinct query strings**.

**Real-document measurement** (`tests/evaluation/real_retrieval.py`): never run. No real hotel
document exists in this repository.

### 1.3 What is predicted, not measured

The table compares what the stand-in returned in R2.9Y (*measured*) with what production would
return for the same query against the same chunk text (*predicted* from PostgreSQL's `english`
configuration: stemming, its stop-word list, AND, chunk text only). The match of each query to its
case is inferred from the query's wording and the pinned hit titles.

| Case | Query sent (limit) | Stand-in returned (measured) | Production (predicted) |
|---|---|---|---|
| `pool_hours` | `pool open hours` (5) | Pool policy, Guest FAQ | nothing: `hour` is not in the text |
| `pool_closing_changed` | `pool closing time` (5) ×5, `pool closing time hours` (5) ×1 | Pool policy, Guest FAQ | nothing: `close` and `time` are not in the text |
| `guest_faq_injected` | `FAQ` (**20**) | Guest FAQ | nothing: "FAQ" is only in the title |
| `dog_fee` | `dog pet cost` (5) | nothing | nothing: `pet` and `cost` are not in "Dogs … for a fee of 20.00 EUR" |
| `pool_towels_injected` | `towels provided at the pool` (5) | Pool policy, Guest FAQ | Pool policy only |
| `parking_price` | `parking cost per night` (5) | Parking, Pets | Parking |
| `occupancy_and_parking` | `parking cost` (5) | Parking | Parking |
| `spa_hours_withdrawn` | `spa opening hours` (5) | nothing | nothing |
| `neighbour_rooftop_bar` | `rooftop bar open hours` (5) | Pool policy | nothing |
| `casino` | `casino` (5) | nothing | nothing |

Two consequences, both *predicted*:

- **`dog_fee` exposes a real weakness, not only a fixture artefact.** A model asked "Can I bring my
  dog, and what does it cost?" searches for several ideas at once, and under AND every extra word
  makes a match less likely. The stand-in fails the same query for an unrelated reason (no
  stemming), so its R2.9Y result agrees with production by coincidence.
- **The injection cases reach the model only because the stand-in matches loosely.** In production
  `FAQ` would not retrieve the Guest FAQ, so whether injected text reaches the model depends on
  which query the model writes. R2.9Y's injection results describe the stand-in.

## 2. The decision: three layers

Neither approach alone is enough. Fixture-only evaluation keeps the defect above: answers are
judged against retrieval production does not have. Real-retrieval-only evaluation makes every
answer and citation test depend on a database, which in this repository means CI only, and gives
up the exact offline replays R2.9Y and R2.10B rely on. Option C keeps both and connects them.

| Layer | What | Runs | Purpose | Limitation |
|---|---|---|---|---|
| **1. Offline** | the metrics scorer, the dataset schema and checksum, answer and citation evaluation fed a **pinned snapshot of real PostgreSQL results** | every commit, no database | exact, fast, deterministic regression checks of scorers and answers | a snapshot is a record of what search returned at one commit, not search itself |
| **2. Real PostgreSQL** | `knowledge_retrieval_v2` through the real upload and search routes; re-checks that Layer 1's snapshot equals live output | CI, through the existing integration-test PostgreSQL | measures the production retrieval path; detects retrieval regressions and snapshot drift | the corpus and queries are invented: not evidence about real hotels |
| **3. Operator** | `tests/evaluation/real_retrieval.py`, unchanged: real hotel documents outside the repository, with a signed-off attestation | by an operator only, never in CI | the **only** evidence that can satisfy the Stage 7.15 (pgvector) trigger | needs documents this repository does not have |

The drift check is the link between the layers. A snapshot that no longer equals live search fails
CI, so an offline answer evaluation cannot quietly keep judging retrieval that no longer exists.

## 3. Dataset v2 governance

`knowledge_retrieval_v2` is a **new set**. `knowledge_retrieval_v1`, its checksum
(`d2523716…713a`) and its pinned report stay frozen and keep being measured as they are.

### 3.1 Identity and versioning

- Identified by `set_id` + `version`, and by the SHA-256 of its canonical JSON (sorted keys,
  compact separators, UTF-8), as v1 is.
- Any change to a document, query, judgment, cutoff or threshold is a **new version**, never an
  edit. Earlier versions and their reports are kept.

### 3.2 Corpus

The v1 documents plus the copilot evaluation's documents, **including the scope traps**: the
superseded pool policy, the withdrawn spa page and the neighbour hotel's rooftop bar. Each document
records `doc_id`, `hotel` (evaluated or neighbour), `status`, `language`, `title` and its
paragraphs.

> **Unresolved:** whether v2 copies the copilot corpus's text into its own definition or imports it
> from `fixture_documents.py`. Either way the canonical JSON must hold the text, so the checksum
> covers it.

### 3.3 Queries and their origins

Every query records `origin`: `developer`, `model_captured` (with run, case, prompt and the
SHA-256 of the capture file), or `operator`. Families, each with enough queries to report on:
shared words, paraphrase, multi-idea (such as `dog pet cost`), cross-language, title-only terms
(such as `FAQ`), no-answer, and scope traps.

The v1 queries are carried over and re-judged under the grades below. The R2.9Y queries are
included **verbatim**, as the model wrote them.

> **Unresolved: 10 or 11 R2.9Y queries.** The assessment said "the ten queries R2.9Y recorded".
> The capture holds 10 searching cases but **11 distinct query strings**: `pool_closing_changed`
> sent `pool closing time` in five attempts and `pool closing time hours` in one. Including every
> recorded string verbatim means 11; one per case means 10. This needs your decision in C.3.
>
> **Unresolved: the `FAQ` query's limit.** It was sent with `limit` 20; every other recorded query
> used 5. Success@5 scores the first 5 results whatever the limit, but the Layer 1 snapshot must
> reproduce what the model was actually shown. Whether snapshots are keyed by query and limit is to
> be settled in C.5.
>
> **Unresolved: family sizes and the total number of queries.** The assessment requires enough
> queries per family to report on, and the n ≥ 10 rule in §5 applies, but it fixes no counts.

### 3.4 Schema for each query

| Field | Meaning |
|---|---|
| `query_id` | stable identifier |
| `query` | the exact search string |
| `language` | the query's language |
| `family` | one of the families in §3.3 |
| `origin` | as in §3.3, with provenance |
| `answerable` | whether the corpus holds the answer |
| `relevant` | a list of `{doc_id, ordinal, grade}` |
| `forbidden` | an annotation: the scope traps this query was written to spring (inactive versions, the neighbour hotel). Leakage does not depend on it (§3.7) |
| `rationale` | why each judgment was made |

### 3.5 Relevance grades

- **2:** the chunk alone states the fact the question needs.
- **1:** on the same topic but partial, or in another language, which lexical search cannot bridge
  by design.
- **0:** everything else.

A chunk in another language is grade 1, never a required alternative. v1's English `pool` query
lists the Greek chunk as an acceptable answer, which lexical search can never return.

### 3.6 Judgments before measurement, and sign-off

Every grade is judged from the text **before** any measurement of v2, and the set is then frozen by
its checksum. The developer writes both the queries and the grades, so you review and sign off the
judgments in C.3 before C.4 measures anything.

> **Unresolved:** how the sign-off is recorded (in this document, in the dataset file, or in the
> commit that freezes the checksum).

### 3.7 Answerable, no-answer and scope-trap queries

- **Answerable** queries form the denominator of Success@5, Recall@5, MRR@5, nDCG@5 and the
  empty-result rate.
- **No-answer** queries have their own denominator and never enter those metrics. Each is scored
  two ways: **strict** (nothing returned) and **lenient** (nothing of grade 1 or above returned).

  > **Resolved in R2.10C.2.1:** the lenient threshold is grade 1, and only grade 1. A no-answer
  > query cannot hold a grade-2 chunk (§3.4), so a threshold of 2 could never fail.
- **Scope leakage is derived from the corpus**, on **every** query, answerable or not. A query
  leaks when anything it returns, at any rank, is a chunk of a document that is not an active
  document of the evaluated hotel: its `hotel` is not `evaluated`, or its `status` is not `active`.
  A chunk the corpus does not hold is refused, not counted. `forbidden` (§3.4) is an annotation,
  reported beside leakage as the traps a query sprang; leaving a chunk out of it cannot hide
  leakage (R2.10C.2.1).

## 4. Metrics

All metrics are computed by a pure scorer, independent of the search, over the first 5 results.

| Metric | Definition |
|---|---|
| **Success@5** | share of answerable queries with at least one grade-2 chunk in the top 5. This is what v1 calls "recall@5", defined exactly; the comparison with a threshold uses integer arithmetic, as `real_retrieval.below_threshold` does |
| **Recall@5** | per query, the share of its grade-2 chunks in the top 5; averaged over answerable queries (a macro average: each query counts once, however many chunks it needs). It matters when a question needs more than one chunk |
| **MRR@5** | mean of 1 / (rank of the first relevant chunk), 0 if none in the top 5 |
| **nDCG@5** | graded discounted cumulative gain over the top 5, normalised by the ideal ordering of the judged chunks |
| **Empty-result rate** | share of answerable queries that return no result at all: the key diagnostic for the all-terms rule |
| **No-answer correctness** | strict and lenient, as §3.7 |
| **Scope leakage** | number of queries that return, at any rank, a chunk of a document that is not active at the evaluated hotel; over all queries (§3.7) |

Every figure is reported overall and **per family**, each with its n and an interval. This
replaces the C.1 wording "each with its n and a Wilson 95% interval": a Wilson interval describes a
rate of successes and cannot describe a mean (R2.10C.2.1):

- **Rates** -- Success@5, the empty-result rate, both no-answer checks and scope leakage -- carry a
  **Wilson 95%** interval.
- **Means** -- Recall@5, MRR@5 and nDCG@5 -- are averages of per-query values in [0, 1], not counts
  of successes, so Wilson does not apply. They carry a **percentile bootstrap 95%** interval: the
  query-level values are resampled with replacement **10,000** times, from
  `random.Random(20261108)`, and the interval is the 2.5th and 97.5th percentiles of the resampled
  means by nearest rank. The unit resampled is the query, never a chunk, so each resample keeps the
  macro average.
- **Below n = 10** a mean carries no interval, and the report says why. An interval whose two ends
  are equal is reported and flagged `zero_width`.
- The bootstrap interval is **descriptive**. It assumes the queries are exchangeable; queries that
  share a document, or that were captured from the same case, weaken that assumption.

> **Unresolved:** whether "relevant" in MRR@5 means grade 2 only or grade 1 or above, and the gain
> function of nDCG@5 (the grade itself, or 2^grade − 1). The assessment says only "rank of the first
> relevant chunk" and "graded". Both are to be fixed in C.2, before any measurement, and recorded
> as a version of this record.

### 4.1 Three things kept apart

| Measure | Question | Where |
|---|---|---|
| **Retrieval adequacy** | did search return the evidence? | this record: Layers 1–3 |
| **Answer correctness** | does the answer state the right fact? | `copilot_knowledge_correctness_v1` (R2.10B) |
| **Citation validity** | does `[Sn]` resolve to a chunk this request's search returned, and does it support the sentence? | the citation check and the knowledge scorers |

Retrieval is necessary but not sufficient for the other two. A missed retrieval followed by a
correct decline is a retrieval failure, not an answer failure. R2.10A's `retrieval_failure` flag
is to be computed from the real snapshot, labelled with its source (C.6).

## 5. Threshold policy

These are declared here, **before the first v2 measurement**, and are not changed after it.

### 5.1 Hard gates (fail CI)

1. **Scope leakage = 0**, as §3.7 defines it: from the corpus, so it cannot be passed by leaving
   a chunk out of a query's `forbidden` list.
2. **The measured report equals the pinned report exactly.** This is the regression gate: a query
   that changes result fails CI until the change is reviewed and the report deliberately
   regenerated.
3. **The Layer 1 snapshot equals live Layer 2 output.**

### 5.2 Adequacy targets (reported, not gated)

- **Success@5 ≥ 0.90 overall:** the number declared for v1, kept for continuity.
- **Each answerable family Success@5 ≥ 0.80**, reported against the target only when the family has
  **n ≥ 10**. Below that, the family's counts and interval are reported without a target verdict.
- **Empty-result rate ≤ 0.10.**

They are reported rather than gated because today's search is known to fall short of them. Gating
would either fail CI permanently or tempt someone to lower the bar.

### 5.3 What thresholds must never be used for

- **No threshold is lowered** to make the current system pass, in this version or a later one.
- **0.90 on this invented corpus does not authorise Stage 7.15 or pgvector,** nor rule it out.
  Only Layer 3, with an attestation of real hotel documents and copilot-like queries, can satisfy
  the trigger declared in `docs/copilot-evaluation.md` §8.2.

## 6. Reproducibility

Each v2 report records:

| Item | Purpose |
|---|---|
| set id, version and canonical-JSON SHA-256 | which set was measured |
| Git commit | which code measured it |
| PostgreSQL server version and image digest | stemmers and stop words come with the server |
| text-search configurations used | `english`, `greek` and the rest |
| scorer version | which metric definitions |
| bootstrap method, resample count and seed | that the mean intervals can be recomputed exactly |
| a fingerprint of the retrieval implementation (`backend/app/repositories/knowledge.py`) | that the search under test is the one recorded |

Reports are written twice: as **canonical JSON** (sorted keys, pinned in the repository, compared
exactly) and as a short **Markdown summary**. Each query row holds family, origin, the top 5 results
with rank and grade, hit, rank of the first relevant chunk, empty flag and forbidden flag. Each
report carries a fixed statement of what it is not evidence of.

> **Unresolved:** what the retrieval fingerprint covers beyond `knowledge.py`: for example the
> chunker (`backend/app/knowledge/chunking.py`) or the migration that defines `search_vector`.

## 7. CI and database safety

- **Layer 1** runs offline on every commit, in the normal suite, with no database.
- **Layer 2** runs through the existing PostgreSQL integration-test infrastructure: CI's ephemeral
  `postgres:18.6` container pinned by digest, the integration `conftest` (schema rebuilt per
  session, `TRUNCATE` after every test) and the `tests/db_safety.py` guard (a `_test` database name
  **and** a database that is empty or explicitly marked disposable). It adds no new database code
  path. A new file under `tests/integration/` lands in CI shard 1 by construction, so `ci.yml`
  needs no change.
- **Layer 3** must never run automatically in CI. It stays the operator command
  `python -m tests.evaluation.real_retrieval`, with documents outside the repository.
- **Locally,** every database test skips here, so Layer 2 can be verified only in CI. The pinned v2
  report therefore has to be produced from a CI run.

> **Unresolved:** how the pinned report reaches the repository from CI (for example, a run that
> uploads the rendered report on mismatch, which is then reviewed and committed).

## 8. Implementation sequence

Each step is its own reviewed stage. None starts without authorization.

| Step | Content | Database |
|---|---|---|
| **C.1** | this decision record | none |
| **C.2** | the pure metrics scorer and dataset schema, offline unit tests, a mutation stage; settles the unresolved metric details in §4 | none |
| **C.3** | dataset v2 authored and judged, checksummed; offline tests that every reference resolves and that the real chunker produces the declared ordinals; **your sign-off of the judgments** | none |
| **C.4** | the first real PostgreSQL measurement in CI, **pinned exactly as measured, with no tuning**; replaces §1.3's predictions with measurements | CI |
| **C.5** | the pinned snapshot of real results for the copilot corpus, an opt-in `SnapshotKnowledge`, and the live-versus-snapshot drift test | CI for the drift test |
| **C.6** | opt-in diagnostics: `retrieval_failure` computed from the snapshot, beside the stand-in's | none |
| **C.7** | final documentation and README updates, including links to this record | none |

Throughout: `FixtureKnowledge` stays the default for R2.9Y replays, and the R2.9Y reference report
must remain byte-identical. Switching the default is a separate decision.

## 9. Non-goals

This work measures retrieval; it does not change it.

- No production retrieval change.
- No query rewriting.
- No any-term matching.
- No title indexing.
- No pgvector.
- No prompt change.
- No change to frozen R2.9Y or R2.10B artifacts: the capture, the replays, the reference report,
  the pinned rows, the correctness rules and their checksum.

Any retrieval change is a later stage, to be authorised separately and measured against v2.

## 10. Risks and limitations

| Risk | Safeguard |
|---|---|
| **Developer bias.** The same author writes the queries and the judgments, and could write queries today's search passes | judgments before measurement, frozen by checksum; captured model queries used verbatim; a written rubric (§3.5); your sign-off |
| **Small samples.** On 23 queries one query moves a rate by about 4.3 points, and the interval for 16 of 23 spans about 0.49 to 0.84 | Wilson intervals on rates and bootstrap intervals on means (§4); family targets only at n ≥ 10 |
| **Synthetic corpus.** Invented documents say nothing about real hotels' wording | stated in every report; Layer 3 is the only route to the Stage 7.15 decision |
| **PostgreSQL configuration drift.** A server or dictionary upgrade can change stemming and stop words, and so results | image pinned by digest; server version and configurations recorded in each report; the pinned-report gate surfaces any change |
| **CI-only database tests.** Layer 2 cannot be run on this machine | keep as much as possible in Layer 1; budget extra CI rounds |
| **Snapshot versus live.** A snapshot records one commit's results; live search can move on | the drift gate (§5.1.3); explicit source labels in every report, so the stand-in, the snapshot and live search are never confused |
| **Real documents in the repository** | Layer 3 keeps them outside it, as `real_retrieval.py` already enforces |

## 11. Amendments

| Stage | Date | Change |
|---|---|---|
| R2.10C.2.1 | 2026-11-08 | **Intervals (§4):** Wilson 95% for rates only; a query-level percentile bootstrap (10,000 resamples, seed 20261108, nearest-rank percentiles, none below n = 10, `zero_width` flagged) for Recall@5, MRR@5 and nDCG@5. Replaces "a Wilson 95% interval" on every figure. **Leakage (§3.7, §4, §5.1):** derived from document `hotel` and `status` on everything returned; `forbidden` becomes an annotation. Replaces "a query that returns any forbidden chunk". **Lenient no-answer (§3.7):** grade 1 only; resolves the open item. |

No measurement has used these definitions. Nothing in this record's measured results changes:
`knowledge_retrieval_v1`'s 16 of 23 and its Wilson interval, and every R2.9Y result, were produced
before them and stay exactly as recorded. The MRR@5 grade and the nDCG@5 gains (§4) remain open and
are given explicitly to every computation.
