# Layered retrieval evaluation — decision record (R2.10C.1)

> **Status.** Architecture approved: Option C, a layered retrieval evaluation (2026-11-08). This
> record is the declaration the later stages are held to. **Nothing in it is implemented yet**, and
> nothing in it changes the application, the prompts, retrieval, an existing measure, or any frozen
> artifact. It is written from the read-only R2.10C assessment; where that assessment did not
> settle a detail, the detail is marked **Unresolved** below instead of being decided here.
>
> **R2.10C.3a (2026-11-09).** Decisions A–F are approved and recorded: each settled item is
> marked **Resolved** with its decision, and F is §12. What remains open is listed in §11.
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
records `doc_id`, `hotel` (evaluated or neighbour), `status`, `language`, `title`, its
paragraphs, `source` and `supersedes`.

> **Resolved (R2.10C.3a, decision C): copied.** v2's corpus is an immutable JSON snapshot,
> `tests/evaluation/datasets/knowledge_retrieval_v2.json`, loaded only through
> `parse_dataset_json` and pinned by its checksum. Each document records `source` -- the module
> and symbol its text was copied from, and the SHA-256 of its paragraphs joined by a blank line --
> and `supersedes`, the doc_id of the version it replaces or null. A superseded version comes
> before its successor in upload order and is replaced exactly once, so the status can be
> produced through the real route. A drift test (C.3b) compares every copy with its source: a
> changed source fails the test and never changes the copy.

### 3.3 Queries and their origins

Every query records `origin`: `developer`, `model_captured` (with the capture file and its
SHA-256, the case, the limit, and every occurrence: the run key as captured and the attempt), or
`operator`. Families, each with enough queries to report on:
shared words, paraphrase, multi-idea (such as `dog pet cost`), cross-language, title-only terms
(such as `FAQ`), no-answer, and scope traps.

The v1 queries are carried over and re-judged under the grades below. The R2.9Y queries are
included **verbatim**, as the model wrote them.

> **Resolved (R2.10C.3a, decision D): 11 R2.9Y queries.** One query per distinct (case, query
> string, limit), so all 11 strings the capture recorded, none merged or dropped. Each counts once:
> occurrence counts are not weights. Ids are `r29y.<case>.<n>`, with n in order of first occurrence
> (run order as captured, attempts ascending), so `pool closing time hours` is
> `r29y.pool_closing_changed.1` and `pool closing time` is `r29y.pool_closing_changed.2`. Each
> query's provenance lists every occurrence. `occupied_nights_may` made no search and has no query.
> A C.3b test re-extracts the queries from the hash-verified capture and requires the 11 queries and
> their 60 occurrences to match exactly.
>
> **Resolved (R2.10C.3a, F4 Option A): how an occurrence is identified.**
>
> - **Identity:** an occurrence is identified by `(run, attempt)` alone. Schema v2 has no
>   `call_index`.
> - **Storage:** the occurrences are stored as a set, sorted by `(run, attempt)`. A pair recorded
>   twice is refused.
> - **Original call order:** this is not stored in the dataset. It is reconstructed from the capture
>   file, which each provenance names together with its SHA-256.
> - **Repeats within one attempt:** a search repeated identically within the same run and attempt
>   therefore cannot be recorded as a separate occurrence; it would be refused as a duplicate, not
>   silently merged. The R2.9Y capture has no such repeat: no case searched more than once in one
>   attempt, so each of its 60 calls is a distinct `(case, query, limit, run, attempt)`.
> - **Future captures:** if a later capture needs such repeats, `call_index` is introduced in a new
>   schema version; v2 is not changed silently.
>
> **Resolved (R2.10C.3a, decision D): the `FAQ` query's limit.** Every query has a `limit`, 1 to
> 20, sent verbatim, so `FAQ` is searched with 20 as captured. Success@5, Recall@5, MRR@5 and
> nDCG@5 read the first 5 results; emptiness, both no-answer checks and leakage read everything
> returned; a ranking longer than its limit is refused. Whether Layer 1 snapshots are keyed by query
> and limit is still settled in C.5.
>
> **Unresolved: family sizes and the total number of queries.** The assessment requires enough
> queries per family to report on, and the n ≥ 10 rule in §5 applies, but it fixes no counts. They
> are decided while authoring (C.3b) and reviewed at sign-off (§3.6).

### 3.4 Schema for each query

| Field | Meaning |
|---|---|
| `query_id` | stable identifier |
| `query` | the exact search string |
| `language` | the query's language |
| `family` | one of the families in §3.3 |
| `origin` | as in §3.3 |
| `provenance` | `model_captured` only: capture file and SHA-256, case, limit, occurrences (§3.3) |
| `limit` | the `limit` its search sends, 1 to 20 |
| `answerable` | whether the corpus holds the answer |
| `relevant` | a list of `{doc_id, ordinal, grade}` |
| `forbidden` | an annotation: the scope traps this query was written to spring (inactive versions, the neighbour hotel). Leakage does not depend on it (§3.7) |
| `rationale` | why each judgment was made |

Each document also carries `source` and `supersedes` (§3.2). The schema is
`knowledge_retrieval_schema_v2` in `tests/evaluation/retrieval_schema.py` (R2.10C.3a).

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

> **Resolved (R2.10C.3a, decision E).** The developer writes the queries, grades and rationales;
> you review them, may change any grade before signing, and have the final say. You approve the
> exact dataset -- its canonical SHA-256, which covers the corpus text, queries, grades,
> `answerable`, families, `forbidden`, limits and provenance -- together with the rubric in §3.5 and
> a review sheet generated deterministically from the dataset, whose SHA-256 is also recorded.
>
> The record is `tests/evaluation/datasets/knowledge_retrieval_v2.signoff.json`: dataset identity,
> schema version and SHA-256; the review sheet's SHA-256; the commit of the rubric; the reviewer's
> name, as you choose it, and the date; the decision (approved, approved with reservations, or
> rejected) and any reservations; the query ids reviewed. It is its own commit, after the dataset
> commit and before any measurement, made by you, or by me once you approve the exact checksum in
> chat. An offline test requires its SHA-256 to equal the dataset's checksum, and the C.4
> measurement refuses without an approved sign-off for that checksum. Any change to the dataset is a
> new version with a new sign-off; earlier sign-offs are never edited or deleted. No automated test
> replaces this review.

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
| **MRR@5** | mean of 1 / (rank of the first grade-2 chunk), 0 if none in the top 5 (decision A) |
| **nDCG@5** | graded discounted cumulative gain over the top 5, gains (0, 1, 3) for grades 0, 1, 2 (decision B), normalised by the ideal ordering of the judged chunks |
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

> **Resolved (R2.10C.3a, decisions A and B).** MRR@5 counts grade 2 only: it is the
> rank-sensitive companion of Success@5 and Recall@5, non-zero exactly when Success@5 holds, and
> partial evidence is credited by nDCG@5 alone. nDCG@5 gains are (0, 1, 3), that is 2^grade − 1:
> ranking partial evidence above sufficient evidence is penalised more than linear gains would, and
> a grade-1 chunk lexical search cannot reach keeps nDCG below 1, which is the cross-language gap
> made visible. Both are `V2_METRIC_CONFIG` in `retrieval_metrics.py`, recorded in every report.

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
| the fingerprint of §12 | that every input that can change a result is recorded and compared |

Reports are written twice: as **canonical JSON** (sorted keys, pinned in the repository, compared
exactly) and as a short **Markdown summary**. Each query row holds family, origin, the top 5 results
with rank and grade, hit, rank of the first relevant chunk, empty flag and forbidden flag. Each
report carries a fixed statement of what it is not evidence of.

> **Resolved (R2.10C.3a, decision F):** §12.

## 7. CI and database safety

- **Layer 1** runs offline on every commit, in the normal suite, with no database.
- **Layer 2** runs through the existing PostgreSQL integration-test infrastructure: CI's ephemeral
  `postgres:18.6` container pinned by digest, the integration `conftest` (schema rebuilt per
  session, `TRUNCATE` after every test) and the `tests/db_safety.py` guard (a `_test` database name
  **and** a database that is empty or explicitly marked disposable). It adds no new database code
  path. A new file under `tests/integration/` lands in CI shard 1 by construction, so it needs
  no new shard. Measuring and pinning do need `ci.yml` and `.gitattributes` changes (§12.6),
  committed before the first baseline, because both files are fingerprinted.
- **Layer 3** must never run automatically in CI. It stays the operator command
  `python -m tests.evaluation.real_retrieval`, with documents outside the repository.
- **Locally,** every database test skips here, so Layer 2 can be verified only in CI. The pinned v2
  report therefore has to be produced from a CI run.

> **Resolved (R2.10C.3a, decision F):** §12.6.

## 8. Implementation sequence

Each step is its own reviewed stage. None starts without authorization.

| Step | Content | Database |
|---|---|---|
| **C.1** | this decision record | none |
| **C.2** | the pure metrics scorer and dataset schema, offline unit tests, a mutation stage; settles the unresolved metric details in §4 | none |
| **C.3a** | schema v2 (document `source` and `supersedes`, query `limit`, occurrence provenance), `V2_METRIC_CONFIG`, and this amendment | none |
| **C.3b** | dataset v2 authored: the corpus copied, the 11 R2.9Y queries extracted and checked against the capture, the v1 queries re-judged, the new families written; checksum pinned; offline tests that every reference resolves, that the real chunker produces the declared ordinals, and that every copy matches its source | none |
| **C.3c** | the review sheet; your review; revisions | none |
| **C.3d** | **your sign-off** (§3.6) and its binding test | none |
| **C.4** | the first real PostgreSQL measurement in CI, **pinned exactly as measured, with no tuning**; replaces §1.3's predictions with measurements. It comes after the numeric contract (§12.5) and the fingerprint machinery are built and after the `ci.yml` and `.gitattributes` changes are committed (§12.6) | CI |
| **C.5** | the pinned snapshot of real results for the copilot corpus, an opt-in `SnapshotKnowledge`, and the live-versus-snapshot drift test | CI for the drift test |
| **C.6** | opt-in diagnostics: `retrieval_failure` computed from the snapshot, beside the stand-in's | none |
| **C.7** | final documentation and README updates, including links to this record | none |

Throughout: `FixtureKnowledge` stays the default for R2.9Y replays, and the R2.9Y reference report
must remain byte-identical. Switching the default is a separate decision. The stage that
implements the numeric contract (§12.5) is not yet scheduled; it must land before C.4.

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
| **PostgreSQL configuration drift.** A server or dictionary upgrade can change stemming and stop words, and so results | the fingerprint of §12: declared digest and local image identity, catalogs, stop-word files, document and chunk state; the pinned-report gate surfaces any change |
| **CI-only database tests.** Layer 2 cannot be run on this machine | keep as much as possible in Layer 1; budget extra CI rounds |
| **Snapshot versus live.** A snapshot records one commit's results; live search can move on | the drift gate (§5.1.3); explicit source labels in every report, so the stand-in, the snapshot and live search are never confused |
| **Real documents in the repository** | Layer 3 keeps them outside it, as `real_retrieval.py` already enforces |

## 11. Amendments

| Stage | Date | Change |
|---|---|---|
| R2.10C.2.1 | 2026-11-08 | **Intervals (§4):** Wilson 95% for rates only; a query-level percentile bootstrap (10,000 resamples, seed 20261108, nearest-rank percentiles, none below n = 10, `zero_width` flagged) for Recall@5, MRR@5 and nDCG@5. Replaces "a Wilson 95% interval" on every figure. **Leakage (§3.7, §4, §5.1):** derived from document `hotel` and `status` on everything returned; `forbidden` becomes an annotation. Replaces "a query that returns any forbidden chunk". **Lenient no-answer (§3.7):** grade 1 only; resolves the open item. |
| R2.10C.3a | 2026-11-09 | **Decisions A–F approved and recorded.** A: MRR@5 at grade 2 (§4). B: nDCG@5 gains (0, 1, 3) (§4). C: the corpus copied into an immutable JSON snapshot, with per-document `source` and `supersedes` (§3.2). D: the 11 R2.9Y queries with every occurrence in their provenance, and a per-query `limit` (§3.3). E: a sign-off file bound to the dataset checksum (§3.6). F: the fingerprint and measurement integrity (§12). Schema v2 (`knowledge_retrieval_schema_v2`) implements C and D; `V2_METRIC_CONFIG` implements A and B. **Still open:** family sizes (§3.3); whether the C.5 snapshot uses the copilot corpus on its own and is keyed by query and limit; whether reports also break figures down by origin; the stage that implements §12.5. |
| R2.10C.3a (F4) | 2026-11-09 | **Occurrence identity: Option A reviewed and approved (§3.3).** An occurrence stays `(run, attempt)`; the exact call order comes from the checksum-pinned capture file; schema v2 adds no `call_index`. A repeat of the same search within one run and attempt cannot be recorded separately (the R2.9Y capture has none); a future capture that needs it gets `call_index` in a new schema version. Option B (a `call_index` in v2 now) was considered and not adopted. |

No measurement has used these definitions. Nothing in this record's measured results changes:
`knowledge_retrieval_v1`'s 16 of 23 and its Wilson interval, and every R2.9Y result, were produced
before them and stay exactly as recorded. At R2.10C.2.1 the MRR@5 grade and the nDCG@5 gains were
still open; R2.10C.3a resolved them (§4). Neither amendment changes a measured result.

## 12. Retrieval fingerprint and measurement integrity (decision F)

Approved 2026-11-09 and recorded here in R2.10C.3a. **Nothing in this section is implemented
yet.** The numeric contract (§12.5) and the fingerprint machinery are built before the first C.4
measurement.

### 12.1 What is compared

A measurement writes a manifest:
- its **fingerprint** is the SHA-256 of the canonical form (§12.2) of its `inputs`;
- each input group also gets its own SHA-256 under `components`, so a mismatch can be traced to one
  group;
- the dataset sign-off is a precondition recorded beside the fingerprint, not part of it.

CI compares the fingerprint first, then the results:
- **different fingerprint:** fails as "inputs changed". That is expected after a deliberate change,
  which is re-measured and re-pinned;
- **same fingerprint, different results:** fails as "results changed with unchanged inputs". That is
  an undeclared input or nondeterminism, investigated and never re-pinned automatically.

Execution metadata goes to a separate environment record that is never compared: commit and
`$GITHUB_SHA`, CI run and attempt, runner image, CPU, C library, `PYTHONHASHSEED`, libpq, pip,
wheel tags, raw `search_path` sources, other PostgreSQL settings, the platform manifest digest if it
can be obtained, and times.

### 12.2 Canonical form

- JSON, UTF-8, sorted keys, separators `(",", ":")`, `ensure_ascii=False`, `allow_nan=False`.
- Every field has one declared type: integer, string, boolean, binary64, list or object.
- **Binary64 values** (Wilson's z, the nDCG gains, the discount table) are written as
  `float.hex(float(v))` strings and read with `float.fromhex`. They are accepted only if finite and
  if they round-trip exactly. Integers and binary64 are never interchangeable: a gain of 3 is always
  `"0x1.8000000000000p+1"`. The writer refuses any float object.
- `null` appears only in the document state's `supersedes`.
- Unordered lists are sorted; ordered lists keep their order.

### 12.3 Inputs

| Group | Contents |
|---|---|
| engine | the declared image reference and digest, read from the `ci.yml` blob at the measured commit and matched by a workflow variable; the local image id, with the Docker image-store type that gives it meaning; os, architecture and variant; `server_version_num` and `version()`; extensions |
| database | server and client encoding, locale provider, collate, ctype, locale, collation version |
| text search | the effective `search_path` (`current_schemas(true)`) and each configuration's schema; configurations, parsers, token types, mappings, dictionaries, templates, init options; the SHA-256 of every referenced stop-word file (read as a superuser) |
| document state, chunk state | §12.4 |
| query parsing | `(query_id, configuration, limit, websearch_to_tsquery(configuration, query)::text)` for every query and every language of the evaluated hotel's active documents |
| schema | the Alembic head; a canonical digest of the live definitions of `hotel_documents` and `hotel_document_chunks` |
| sources | Git blob content (§12.6) of `.github/workflows/ci.yml`, `.gitattributes`, `pyproject.toml`, `alembic.ini`; `backend/app/main.py`, `api/deps.py`, `api/v1/endpoints/knowledge.py`, `services/knowledge.py`, `services/scope.py`, `repositories/hotel.py`, `repositories/knowledge.py`, `knowledge/chunking.py`, `schemas/knowledge.py`, `models/knowledge.py`, `models/enums.py`; `database/migrations/env.py` and every file under `database/migrations/versions/`; `tests/conftest.py`, `tests/integration/conftest.py`, `tests/evaluation/retrieval.py`, `retrieval_schema.py`, `retrieval_metrics.py`, the C.4 harness and integration test, the dataset JSON; `backend/requirements-lock.txt` and `backend/requirements-dev-lock.txt` |
| python | CPython 3.14.7 and its build string; the float format (53-bit mantissa, radix 2, maximum exponent 1024, short repr); the `_decimal` and libmpdec versions; the SHA-256 of the canonical installed set |
| dataset | identity, schema version, canonical SHA-256 |
| metrics | the metrics version; `V2_METRIC_CONFIG` in full, bootstrap settings included; the discount table and the numeric contract's identifier |

**Excluded, each by reason:**
- `database/migrations/script.py.mako`: never executed.
- `core/config.py`, the scope policy and `tests/db_safety.py`: they can only refuse.
- `models/base.py`: identity generation is defined in the fingerprinted migration DDL.

**Installed set.** It must equal exactly the pins of `requirements-dev-lock.txt` whose markers hold
on the interpreter:
- transitive dependencies included, with markers evaluated by `packaging`;
- every runtime-lock pin must equal its dev-lock pin;
- every installed distribution's `RECORD` must match its files on disk;
- the only exception is `pip`, recorded in the environment record.

**Bootstrap.** It stays `random.Random(seed).randrange(n)`. That is exactly uniform, and
fingerprinting CPython's exact version keeps its sequence.

**Image identity.**
- The container's image must list `postgres@<declared digest>` in its `RepoDigests`.
- Its architecture must agree with the build target in `version()`.
- The platform-specific manifest digest is not observed, and not claimed.

### 12.4 Database state

Both digests are read from the whole tables after ingestion; after the suite's `TRUNCATE`, only
this run's rows exist.

**Document state:** one row per document, chunks or not, sorted by (`hotel`, `doc_id`).
- `hotel`: the role, mapped through the two harness hotels' rows; any other owner refuses the run.
- `doc_id`: from `public_id`, through the upload record.
- `status`, `language`, `title`, `version`, `content_checksum`.
- `document_order`: the 0-based rank of `id` among all documents.
- `supersedes`: the predecessor's `doc_id`, or `null`. It is the only nullable field.
- `chunk_count`.

**Chunk state:** one row per chunk, sorted by (`doc_id`, `ordinal`).
- `doc_id`, `ordinal`;
- the SHA-256 of `text`;
- `search_vector::text`.

**Excluded:** raw ids and public ids (random each run, so verified by a bijection instead),
`source`, `token_count`, timestamps.

**Comparisons.** Both are compared exactly with the expected state built from the dataset and the
harness's plan: uploads in dataset order, new versions through the supersede route, then
withdrawals.
- Chunk rows are compared without `search_vector`.
- Every chunk must satisfy `search_vector = to_tsvector(language::regconfig, text)`.
- A document with zero chunks is always a mismatch.

**Served results:**
- every search result's chunk and document public ids must belong to the bijection;
- its `title`, `version`, `language`, `ordinal` and `text` must equal the database row and the
  dataset;
- the echoed query and limit must equal what was sent;
- a result list may not be longer than the limit.

Reports compare each query's ordered `(doc_id, ordinal)` list, plus each result's
`served_sha256` = SHA-256(canonical {doc_id, ordinal, title, version, language, text_sha256}).

### 12.5 Numeric contract (`retrieval_numerics_v1`)

- **The nDCG discounts.** Ranks 1 and 3 are exactly 1.0 and 2.0. Ranks 2, 4 and 5 are binary64 hex
  literals for log₂3, log₂5 and log₂6, each certified. They replace `math.log2`.
- **Wilson's square root.** `Decimal(x).sqrt()`, certified. It replaces `math.sqrt`.
- **Decimal.**
  - CPython's `_decimal` is required, used only in local contexts, with `ROUND_HALF_EVEN`, `Emin`
    −999999, `Emax` 999999 and `clamp` 0.
  - Traps are set on `InvalidOperation`, `DivisionByZero`, `Overflow`, `Underflow`, `Subnormal` and
    `Clamped`; only `Inexact` and `Rounded` may be set.
- **Precisions P₁/P₂.** 80/160 for ln(k)/ln(2) with k = 3, 5, 6; 60/120 for square roots.
- **Certificate.**
  - r = `float(d₂)` must equal `float(d₁)` exactly, compared as `float.hex` strings.
  - The neighbours come from `math.nextafter`, and the exact midpoints are `Fraction`s built from
    `as_integer_ratio`. The outer midpoint of the largest double is the overflow threshold
    2¹⁰²⁴ − 2⁹⁷⁰.
  - D = `Fraction(d₂)`. The error bound E is an exact `Fraction`:
    - for the square root, ½ × 10^(adjusted(d₂) − P₂ + 1), or 0 if the result was exact;
    - for the logarithm, 2 × 10^(1 − P₂) × |D|. That covers two correctly rounded `ln` and one
      division, whose worst case is about 1.5 × 10^(1 − P₂) × |D| for any P ≥ 3.
  - The result is accepted only if m₋ < D − E and D + E < m₊. An exact tie (E = 0) is accepted only
    for the even significand.
  - Anything else raises `NumericVerificationError`: a measurement refuses, an offline test fails,
    and nothing is guessed.
  - Domains: square-root inputs must be positive, normal and finite; discounts must lie in (1, 4).
- **Tests.**
  - An exact `Fraction` check, independent of Decimal, of every Wilson radicand for n ≤ 200.
  - Certificates at both precisions; context isolation; every refusal path.
  - The existing Wilson and nDCG pins are re-checked: any difference is reported, never quietly
    re-pinned. The bootstrap pins stay unchanged.
  - The hash-seed test.
- **The claim.** Results are correctly rounded and reproducible within the fingerprinted runtime.
  They rely on the documented correct rounding of `_decimal`. There is no cross-platform proof.

### 12.6 Source hashing, CI artifacts and the baseline

**Source hashing.**
- The measured commit is `git rev-parse --verify HEAD^{commit}`, and the tracked tree must be
  clean.
- Every source must be a blob at that commit. Its hash is the SHA-256 of
  `git cat-file blob <commit>:<path>`, recorded with the blob id. No line-ending conversion is
  involved.
- Every fingerprinted Python module must be imported from its repository path.

**Artifacts.**
- Before the tests, a workflow step writes the Docker metadata file (from `docker inspect`,
  `docker image inspect` and `docker info`).
- The C.4 integration test writes `report.json`, `environment.json` and `report.json.sha256`
  (written last) to `$RETRIEVAL_EVAL_OUT`, atomically and before any assertion. On refusal it
  writes `refusal.json` instead.
- A shard-1 step with `if: always()` prints every file's SHA-256 to the log and uploads the files
  with `actions/upload-artifact`, pinned by SHA, with `if-no-files-found: error`, named by run id
  and attempt.

**Pinning.**
1. Download with `gh run download`. That uses the network and needs your authorisation each time.
2. Check that:
   - the report's SHA-256 equals both its `.sha256` file and the digest in the job log;
   - the fingerprint equals SHA-256(canonical(`inputs`));
   - the repository-derived components recompute identically offline at the run's commit.
3. Copy the bytes unchanged and commit them alone (the report and its environment record), with the
   report SHA-256, the fingerprint, the CI run and the artifact digest in the commit message.
4. The report is marked `-text` in `.gitattributes`.

**Order.** The `ci.yml` steps and the `.gitattributes` entry are committed before the baseline
measurement, which runs on a commit that already contains them. The pin commit changes no
fingerprinted file. Artifacts and logs expire; the committed report and its digests are the
permanent record.

### 12.7 Failing closed

A measurement refuses (writing `refusal.json` and no report) on any of:
- a required input that can't be measured, or a probe that fails;
- a NULL, an expected-versus-actual mismatch, a vector-binding failure, or a `content_checksum` or
  `supersedes` mismatch;
- a hotel-scope violation, a broken bijection, a served field that differs, a wrong echo, or more
  results than the limit;
- an image-identity failure;
- a stop-word file that can't be read;
- an installed-set, constraint, `RECORD` or marker failure;
- a numeric certificate failure;
- no Git, a dirty tracked tree, or a source that is missing, untracked or imported from elsewhere;
- a sign-off that is missing or doesn't match.

No `null`, "unknown" or placeholder value ever enters `inputs`.

### 12.8 Limits

- **Unverified until C.4 runs**, each refusing the run if absent:
  - the runner's Docker image store and inspect fields;
  - superuser access to the stop-word files;
  - `packaging` in the dev lock;
  - the `_decimal` and libmpdec versions.
- The platform manifest digest is implied, not observed.
- Reports are exactly comparable only within the fingerprinted environment.
- Any `ci.yml` edit forces a re-measurement and re-pin.
