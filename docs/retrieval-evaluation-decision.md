# Layered retrieval evaluation — decision record (R2.10C.1)

> **Status.** Architecture approved: Option C, a layered retrieval evaluation (2026-11-08). This
> record is the declaration the later stages are held to. When it was written nothing in it was
> implemented, and nothing in it changes the application, the prompts, retrieval, an existing measure, or any frozen
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
>
> **Status at C.7 (2026-11-19).** Every step of §8, C.1 to C.7, is implemented, and §11 records
> each. C.4 measured `knowledge_retrieval_v2` on real PostgreSQL in CI and pinned the report;
> C.5 recorded real search's results for the copilot evaluation's searches and holds them to
> live search; C.6 judges the copilot runs' retrieval on that snapshot beside the stand-in's.
> §1.4 is what was measured. The application, its retrieval, its prompts and every frozen
> artifact are unchanged.

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

### 1.4 What C.4 and C.5 measured

**`knowledge_retrieval_v2`, real PostgreSQL** (*measured*; `tests/evaluation/retrieval_report_v2.json`, SHA-256
`a6ddeec598d264ff48d39f73f607d797f2d4da15be8252a6c0774ba9a78b0c15`, fingerprint `8871ff66…`;
produced by `tests/integration/test_knowledge_retrieval_v2_eval.py` in CI run 44194081637 and
pinned, bytes unchanged, in `b6defd7`; CI run 44210410736 then reproduced it exactly). The
signed-off dataset (§3.6): 17 documents, 26 chunks, 57 queries, 45 answerable and 12 with no
answer. Intervals are §4's: Wilson 95% on rates, the seeded query bootstrap on means.

| Measure | Result | 95% interval | §5 |
|---|---|---|---|
| Success@5 | 23 of 45 = 0.5111 | 0.3700 to 0.6504 | target ≥ 0.90: **not met** |
| Recall@5 | 0.5000 | 0.3556 to 0.6444 | — |
| MRR@5 | 0.5111 | 0.3778 to 0.6444 | — |
| nDCG@5 | 0.4687 | 0.3368 to 0.6020 | — |
| Empty-result rate | 22 of 45 = 0.4889 | 0.3496 to 0.6300 | target ≤ 0.10: **not met** |
| No-answer, strict | 11 of 12 | 0.6461 to 0.9851 | — |
| No-answer, lenient | 12 of 12 | 0.7575 to 1.0000 | — |
| Scope leakage | 0 of 57 | 0.0000 to 0.0631 | gate 1: **passes** |

| Family | Answerable | Success@5 | Empty results | §5.2 family target |
|---|---|---|---|---|
| shared words | 17 | 17 of 17 | 0 of 17 | ≥ 0.80: **met** |
| paraphrase | 10 | 0 of 10 | 10 of 10 | ≥ 0.80: **not met** |
| title only | 5 (and 1 no-answer) | 0 of 5 | 5 of 5 | n < 10: no verdict |
| multi-idea | 6 | 1 of 6 | 5 of 6 | n < 10: no verdict |
| cross-language | 6 | 4 of 6 | 2 of 6 | n < 10: no verdict |
| scope trap | 1 (and 5 no-answer) | 1 of 1 | 0 of 1 | n < 10: no verdict |
| no answer | 0 (6 no-answer) | — | — | — |

What it confirms: §1.2's finding at a larger scale. The search finds a chunk that shares the
query's words and almost nothing else; every paraphrase and title-only query returns nothing at
all, so the failure is the all-terms rule and the untitled index, not ranking. Nothing leaked.
What it does not: the corpus and most queries are invented, so it is regression evidence about
the search as configured, not about real hotels, and §5.3 holds: it neither authorises nor
rules out Stage 7.15.

**§1.3's predictions, recorded** (*measured*; C.5's `copilot_retrieval_snapshot_v1`, SHA-256
`92f0000da79ab72b0ea80e3a097bb3722864868562fbbe88ea83eda7b2acba93`, held to live search by
gate 3 in CI run 44396391885). Real search returned exactly what §1.3 predicted for all ten of
its queries: nothing for `pool open hours`, `pool closing time`, `pool closing time hours`,
`FAQ`, `dog pet cost`, `spa opening hours`, `rooftop bar open hours` and `casino`; Pool
policy alone for `towels provided at the pool`; Parking alone for `parking cost per night` and
`parking cost`. C.6 turns this into the copilot runs' `retrieval_failure` (§11, R2.10C.6): on
the six R2.9Y attempts, 1 of 10 judged cases with the stand-in and 4 of 10 with real search.

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
> **Resolved (C.5, 2026-11-18): keyed by (query, limit).** Each snapshot entry is one exact query
> string with one exact limit, as sent; a pair the snapshot does not hold refuses, and so does a
> recorded ranking longer than its limit. The snapshot is of the copilot corpus alone (§11, C.5).
>
> **Resolved (R2.10C.3b): family sizes and the total number of queries.** v2 holds 57 queries:
>
> | Family | Queries |
> |---|---|
> | `shared_words` | 17 |
> | `paraphrase` | 10 |
> | `multi_idea` | 6 |
> | `cross_language` | 6 |
> | `title_only` | 6 |
> | `no_answer` | 6 |
> | `scope_trap` | 6 |
>
> - **Answerable:** 46; the other 11 are the six `no_answer` queries and five of the six scope
>   traps.
> - **Origin:** 46 developer queries (the 24 v1 queries carried over verbatim and 22 new ones) and
>   the 11 model-captured R2.9Y queries.
> - **Family targets:** only `shared_words` and `paraphrase` reach n ≥ 10, so only they are
>   reported against a family target (§5.2). The other families report counts and intervals
>   without one.
> - **Review:** the sizes, like every judgment, are reviewed at sign-off (§3.6).

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

- **2:** the chunk alone states the fact the question needs. When a question asks for several
  independent facts, each is a fact the question needs: a chunk that alone states at least one of
  them in full is grade 2.
- **1:** on the same topic but partial — it states only part of a needed fact, or the topic
  without the fact — or in another language, which lexical search cannot bridge by design.
- **0:** everything else.

A multi-idea query is answerable only when every fact it asks for is stated in full by at least
one grade-2 chunk. (Clarified in R2.10C.3c, adjudication R7; the grades' meaning is unchanged.)

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

> **Resolved (C.6, 2026-11-19): beside the stand-in's, by the same rule.** The opt-in
> `snapshot_retrieval_failure` block (`tests/evaluation/snapshot_retrieval_failure.py`) applies
> R2.10B's `knowledge_correctness.assess`, unchanged, twice: to what the stand-in returned for the
> model's searches, and to what the C.5 snapshot holds for the same (query, limit). Each verdict is
> labelled with its source; the model saw the stand-in's results only, so the snapshot's verdict
> describes real search for the searches this model made, not what the model would have done (§11,
> R2.10C.6).

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
- **Locally,** database tests skip unless a disposable scratch database is provided
  (`scripts/testdb.py`; never the demo data). C.4's harness and C.5's gate 3 were run so before
  CI, but the C.4 measurement itself is skipped outside CI and refused in CI without its
  directory (§12.6), so the pinned v2 report is a CI run's, as it has to be.
  *(Until C.4 this bullet said every database test skips here, which was true when written.)*

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

**Status (C.7, 2026-11-19):** all seven steps are done; §11 records each, and §1.4 what C.4 and
C.5 measured.

Throughout: `FixtureKnowledge` stays the default for R2.9Y replays, and the R2.9Y reference report
must remain byte-identical. Switching the default is a separate decision. The stage that
implements the numeric contract (§12.5) and the one that implements the fingerprint machinery
with its `ci.yml` and `.gitattributes` prerequisites (§12.1-12.4, §12.6, §12.7) are both
implemented (§11, R2.10C numeric contract and fingerprint); they come before C.4.

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
| R2.10C.3b | 2026-11-09 | **Dataset v2 authored, not yet reviewed or signed off.** The snapshot is `tests/evaluation/datasets/knowledge_retrieval_v2.json`, canonical SHA-256 `35a968055c790628c2bd9c20ee48913eca0dcd9b0929df30f140c81370a6d5ed`. It holds 17 documents and 26 chunks, copied from `retrieval.py` (10) and `fixture_documents.py` (7, with the superseded pool policy, the withdrawn spa page and the neighbour's rooftop bar), and 57 queries. The 11 R2.9Y queries and their 60 occurrences are extracted from the hash-checked capture. `tests/evaluation/retrieval_v2.py` refuses a snapshot or capture that isn't the pinned one, reports any copy that differs from its source, and reports any difference from the capture. **Every grade, family and rationale is a developer proposal written from the copied text:** it is not reviewed (C.3c), not signed off (C.3d), and no retrieval has been measured on it. Family sizes are resolved (§3.3). **Still open:** the C.5 snapshot's corpus and keying; a breakdown by origin; the stage that implements §12.5. |
| R2.10C.3c | 2026-11-11 | **Review material prepared; the review itself is yours and has not happened.** `tests/evaluation/retrieval_review.py` generates the review sheet `tests/evaluation/datasets/knowledge_retrieval_v2.review.md` deterministically from the dataset (SHA-256 of its LF form `b3e310ff590a86cc9dc724d5959790e12d258e233bb93f6fc3f4c9de108c8fd5`): §3.5 word for word, every document and chunk in full with hotel, status, language, versions and source, and every query by family with its attributes, provenance, rationale, judged and forbidden chunks, flags (no-answer, scope trap, inactive version, neighbour hotel, cross-language, partial, model-captured) and two reading aids listing unjudged chunks that share a word with the query. It also generates the review form `knowledge_retrieval_v2.review.json`, bound to the dataset's identity, schema and SHA-256 and to the sheet's SHA-256, with a decision slot for each of the 57 queries and 97 judgments and a list for chunks to add. `python -m tests.evaluation.retrieval_review check` refuses a form bound to another dataset or sheet or breaking a rule (an unknown field, an invalid grade, a change without a rationale), and lists what is undecided. A complete form is the review, not the sign-off. No query, grade or rationale changed: the dataset and its checksum are as in R2.10C.3b. A change you ask for is a revision: a new dataset version, sheet and form, reviewed again (§3.6). Every judgment decision, `accept` included, needs a rationale, so every final grade is explained (your correction of 2026-11-11); a query's `accept` needs none, its `revise` does. The sign-off record and its binding test remain C.3d's. |
| R2.10C.3c (review) | 2026-11-11 | **Your review applied; not signed off.** You reviewed all 57 queries in chat and approved them; your decisions were recorded on the form of dataset `35a968055c790628c2bd9c20ee48913eca0dcd9b0929df30f140c81370a6d5ed` (sheet `b3e310ff590a86cc9dc724d5959790e12d258e233bb93f6fc3f4c9de108c8fd5`): 96 judgments accepted and one changed, 56 queries accepted and one revised. **Revisions applied, the only ones:** `v1.r01`'s `v1.pool-rules-greek#1` from grade 1 to 0 (it only states that children under 12 must be accompanied and answers neither pool hours nor towels), so it is no longer judged; `v2.title.cancellation-policy` gains the superseded `copilot.pool-policy-v1#0` as forbidden (out of scope; its title also says 'policy'), while the active `copilot.pool-policy-v2#0` stays unjudged. Both queries' rationales say so. The revised dataset is `bb2466278ca15ee4a53c91ee81c889f837bb797937f75fe8a90061ad14400919` (57 queries, 96 judgments: 64 of grade 2, 32 of grade 1; 16 forbidden annotations; 11 no-answer queries, unchanged), its sheet `1a9db9d1de8b5ef04b1065a38cf00ca581ef269546015c9ac1a786898baab406`, and its form records your final review: all 57 queries and all 96 judgments accepted, each judgment with a rationale written from the chunk text on your instruction. The reviewer's name and date are left for you; no sign-off exists. C.3d binds your attestation to these two checksums. |
| R2.10C.3c (adjudication) | 2026-11-11 | **Adjudication revisions R1-R7 applied, on your approval in chat; not signed off.** A read-only audit of all 96 judgment rationales and its adjudication found one rubric violation, four objective errors and one rubric gap on dataset `bb2466278ca15ee4a53c91ee81c889f837bb797937f75fe8a90061ad14400919` (sheet `1a9db9d1de8b5ef04b1065a38cf00ca581ef269546015c9ac1a786898baab406`). **R1** `v1.r23` ('πισίνα', the Greek word for 'pool'): `v1.pool-rules-greek#1` from grade 1 to 0, agreeing with `v1.r01`, since the children rule answers neither the pool's hours nor its towels. **R2** `r29y.guest_faq_injected.1`: `copilot.guest-faq#0` from grade 2 to 0 and the query made no-answer, since the chunk is an injection fixture that states no hotel fact, and a title or a document's being the only FAQ is not answer-bearing content (§3.5 grade 2). **R3**, **R4** `v2.title.cancellation-policy`, `v2.title.pool-policy`: rationales corrected; 'policy' appears in no chunk's text, only in the titles 'Cancellation policy' and 'Pool policy' (both versions); grades unchanged. **R5** `v2.trap.bar-open`: `v1.pool-and-spa#0` from grade 1 to 0, so both active pool pages, which mention the pool bar only as where towels are, are grade 0; the neighbour's rooftop bar stays forbidden. **R6** review-form wording only: the `r29y.dog_fee.1` and `v2.multi.shuttle-and-taxi` chunks state every requested fact, not 'one of the facts'. **R7** §3.5 clarified for multi-idea queries (each requested fact is a fact the question needs; a chunk stating one in full is grade 2; such a query is answerable only when every fact has a grade-2 chunk), in §3.5, the review sheet's rubric and the schema docstrings; a test pins each multi-idea query's facts to their grade-2 chunks. The nine partial-fact multi-idea grade-2 judgments are unchanged, as are the grades' meaning, the metrics and retrieval. The revised dataset is `7f35b8a473c73f6d318e48ce5cc2667294f0d50816c511cfaf7851585c617031` (57 queries: 45 answerable, 12 no-answer; 93 judgments: 63 of grade 2, 30 of grade 1; 16 forbidden annotations), its sheet `fb79c2fb5d0744e315853cf70ed8b43a7d54a510536452f5aa031c0ee22e7908`, and its form records the review: all 57 queries and all 93 judgments accepted, each judgment with a rationale. The reviewer's name and date are left for you; no sign-off exists. C.3d binds your attestation to these two checksums. |
| R2.10C.3d | 2026-11-12 | **Signed off: approved, without reservations.** The reviewer, Georgios Tsamoglou, approved dataset `knowledge_retrieval_v2` (`7f35b8a473c73f6d318e48ce5cc2667294f0d50816c511cfaf7851585c617031`, schema `knowledge_retrieval_schema_v2`) and its review sheet (`fb79c2fb5d0744e315853cf70ed8b43a7d54a510536452f5aa031c0ee22e7908`) in chat on 2026-11-12; the record `tests/evaluation/datasets/knowledge_retrieval_v2.signoff.json` was written on that approval. It binds the review form recorded in C.3c (`92d21865258c1086a903d16f43e6a7cf813bbcef3e4400666cd47140dc8b01f1`, LF), the rubric as §3.5's text (`c86149e5e421a8016d3b4228e2ef0edc32f8b3cc8e804f31999b5e1972141458`) and the commit that recorded it (`9ac08b2c9873d245c9d2bb39fa9c770a936b47e6`, the C.3c commit), all 57 query ids, and CI run 43092333518 on that commit (success, 7 of 7 jobs, 621 of 621 mutations killed). `tests/evaluation/test_retrieval_signoff.py` requires the record to match the dataset, sheet, form and rubric in the tree and refuses any other hash, rubric commit or decision; with Git history it also checks that commit's §3.5, dataset, sheet and form. The sign-off approves the judgments for measurement; it does not validate retrieval accuracy, which C.4 measures. |
| R2.10C (numeric contract) | 2026-11-12 | **§12.5 implemented, before C.4; nothing measured.** `tests/evaluation/retrieval_numerics.py` (`retrieval_numerics_v1`) gives the nDCG discounts log2(rank + 1) as exact 1.0 and 2.0 at ranks 1 and 3 and certified binary64 hex literals at ranks 2, 4 and 5, and Wilson's square root as a certified `Decimal` square root, each computed at P1/P2 (80/160 digits for ln(k)/ln(2), 60/120 for square roots) in a local `_decimal` context and accepted only through the certificate; anything else raises `NumericVerificationError`. `retrieval_metrics.py` takes both from it, replacing `math.log2` and `math.sqrt`. **Results are unchanged:** the certified discounts equal `math.log2` here and every Wilson interval for n <= 200 equals the `math.sqrt` formula, so no Wilson, nDCG or bootstrap pin moved. **Two clarifications of §12.5, approved by you on 2026-11-12:** (A) the radicand is converted with `Decimal.from_float(x)`, because the literal `Decimal(x)` sets `FloatOperation`, which the rule that only `Inexact` and `Rounded` may be set forbids, and constructing it outside the local context would break "used only in local contexts": the two readings were contradictory, and the value is identical; (B) the hash-seed test verifies that numeric outputs are independent of `PYTHONHASHSEED`. 16 mutations (R2.10C-M109 to M124) and M10/M21 re-anchored: 637 in all. |
| R2.10C (fingerprint) | 2026-11-12 | **§12.1-12.4, §12.6 and §12.7 implemented, before C.4; nothing measured, nothing downloaded.** `retrieval_canonical.py` is §12.2: the writer refuses float objects and writes binary64 as `float.hex`, the reader accepts only bytes it would write, and every field is checked against a declared type, `null` being declared only for `supersedes`. `retrieval_fingerprint.py` builds the manifest (fingerprint = SHA-256 of the canonical `inputs`, one component per group, the sign-off beside it) and the `sources`, `python`, `dataset`, `metrics` and Docker `engine` groups; `retrieval_probes.py` the database groups and §12.4's state, plan comparison and served-result checks, through one injected `fetch`; `retrieval_artifacts.py` the measurement path C.4 calls, which writes `report.json`, `environment.json` and `report.json.sha256` (last) atomically before any assertion, or `refusal.json` with one of §12.7's nine reasons, and `verify`, pinning step 2 run offline on files you have downloaded. **The C.4 harness** in §12.3's sources is those four modules and the modules they load the dataset, sign-off, review and numeric contract through; the C.4 integration test is reserved as `tests/integration/test_knowledge_retrieval_v2_eval.py`, so until C.4 writes it every measurement refuses with a missing source. **`ci.yml`:** the quality-gates variable `RETRIEVAL_EVAL_POSTGRES_IMAGE` (equal to the service image, both read from the measured blob); in shard 1, a step before pytest writing `docker.json` to `$RETRIEVAL_EVAL_OUT`, and two `if: always()` steps after it printing every file's SHA-256 and uploading with `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02` (v4.6.2), named `retrieval-eval-<run id>-<attempt>`, `if-no-files-found: error`. **`.gitattributes`:** `tests/evaluation/retrieval_report_v2.json -text`, the path pinning copies the report to. 43 mutations (R2.10C-M125 to M167): 680 in all. **Open before C.4, for your decision:** PostgreSQL stores `NULL` in `pg_database.datlocale` under the `libc` provider (the official image's default) and in `pg_ts_dict.dictinitoption` for the built-in `simple` dictionary, which every configuration maps some tokens to; §12.7 refuses every `NULL`, so as written the measurement refuses on that image. An image without a variant is recorded with `variant` `""`, OCI's meaning of an absent field. The upload action's SHA was not checked online. |
| R2.10C (catalog NULLs) | 2026-11-13 | **Two catalog exceptions and the upload action's SHA, approved by you on 2026-11-13; nothing measured, nothing downloaded.** PostgreSQL defines two NULLs the probes read (its bootstrap catalog and documentation, PostgreSQL 18.6): `pg_database.datlocale` under the `libc` provider, the official image's default, and `pg_ts_dict.dictinitoption` of the built-in `pg_catalog.simple`, which every built-in configuration maps number and similar tokens to. No configuration removes them without changing the measured locale or text-search configuration, so §12.2, §12.3 and §12.7 now record them as typed `null`, part of the fingerprint: `datlocale` is `null` if and only if the provider is `libc`, and `dictinitoption` may be `null` only for `pg_catalog.simple`; an empty string refuses for every dictionary (the declared type had accepted one). Every other NULL still refuses. The probes enforce both rules, and the manifest check enforces them again, so `verify` holds a downloaded report to them. **The upload action's SHA:** `refs/tags/v4.6.2` of `actions/upload-artifact`, read with `gh api` (a lightweight tag), is `ea165f8d65b6e75b540449e92b4886f43607fa02`; the SHA recorded in the row above was written from memory and was wrong after its first 15 characters. `ci.yml` and that row now carry the verified SHA, and the workflow test asserts it exactly. 11 mutations (R2.10C-M168 to M178): 691 in all. |
| R2.10C (final specification) | 2026-11-13 | **The final specification audit's plan, approved by you on 2026-11-13; nothing measured, nothing downloaded, C.4 not started.** **(A) The Docker variant:** `engine.platform.variant` is a typed `null` if and only if Docker's image inspection has no `Variant`, and otherwise a non-empty string kept unchanged; a present `""` or a non-string refuses (`image_identity`). It replaces the `""` the R2.10C (fingerprint) row recorded for an absent variant, which stood in for absence; §12.2 now has four nulls. **(B) The sources:** the 13 package `__init__.py` files that run before the fingerprinted modules are fingerprinted; the C.4 harness is named module by module, `retrieval_review.py` and `retrieval_signoff.py` kept because they decide which sign-off is accepted. §12.3 excluded `models/base.py`, which does not exist; the declarative base is `db/base.py`, now excluded under that reason. A fresh interpreter importing the measurement path loads 61 repository files; the 45 that were neither fingerprinted nor excluded are now accounted for: 7 package `__init__.py` files fingerprinted and 38 modules excluded, each by one of §12.3's reasons; a test holds that import closure to the inventory, so a new module cannot enter it silently. Every excluded file must exist at the measured commit, and the measured tree may hold no untracked file and no ignored file Python could execute outside the cache and environment directories (§12.6). **(C) Empty strings:** a template's `lexize` and a token type's description must be non-empty; a tsquery may be empty. **(D) The artifacts:** the layout and pinned paths of §12.6 are fixed constants; `$RETRIEVAL_EVAL_OUT` must be absolute and resolve (symlinks followed) outside the repository, or the run refuses and writes nothing; a run starts with `docker.json` alone and leaves exactly the success or the refusal set; `verify` reports a refusal first, refuses extra, stale, partial or irregular files, and checks `environment.json` against its own job-log digest (`--environment-log-digest`). The pinned environment record, `tests/evaluation/retrieval_report_v2.environment.json`, is `-text` like the report. **(E) The tree:** the environment record carries the measured commit's tree id, and `verify` trusts it only after the environment digest matches, then requires the checked-out commit to have exactly that tree, so a pull request's merge commit qualifies when, and only when, its tree is the measured one. The import-closure test covers the measurement path; the C.4 integration test's own closure (the application through `app.main`) is checked when C.4 writes it. 37 mutations (R2.10C-M179 to M215) and three re-anchored (M164, M166, M167): 728 in all. |
| R2.10C (exclusion audit) | 2026-11-17 | **The 38 exclusions accepted, on three conditions; nothing measured, C.4 not started.** A read-only audit traced each of the 38 modules §12.3 excludes beyond its original three through the measurement's real call paths: the schema comes from the migrations, the teardown is SQL, none of the 38 registers event listeners, reads the environment or imports dynamically, no model relates to the knowledge tables, and the production session factory is reached only through `get_db`, which the conftest overrides. You accepted all 38 on 2026-11-17, subject to three mandatory C.4 requirements, now §12.6's "C.4's requirements": one engine, the conftest's, with no fallback to the production session factory (A); hotel ownership by SQL (B); an explicit HTTP status for every ingest and search response (C). The harness provides `EngineFetch`, `require_measurement_wiring`, `production_sessions_forbidden`, `checked_json` and `probes.hotel_ids`; `tests/evaluation/test_retrieval_c4_requirements.py` holds them and the audit's four static findings by AST inspection. The session-settings difference (production `autoflush=False`, the conftest's `autoflush=True`) is recorded. `SOURCE_FILES` and `EXCLUDED_SOURCES` are unchanged. 15 mutations (R2.10C-M216 to M230): 743 in all. |
| R2.10C (C.4 harness) | 2026-11-17 | **The C.4 integration test and its gates, implemented; nothing measured, no baseline, nothing pinned.** `tests/integration/test_knowledge_retrieval_v2_eval.py` measures `knowledge_retrieval_v2` through the real API under §12.6's three requirements, and `retrieval_artifacts.gate_problems` applies §5.1's gates 1 and 2 to what it wrote (§12.6 "The C.4 test"). Your decisions of 2026-11-17: with no pinned report the gate fails, so the baseline run is red until pinned; the measurement is skipped outside CI and refused in CI without its directory; the application-wide import closure (131 unaccounted files) is audited and approved before any baseline. The Docker `Variant` rule is held from the workflow's `docker.json` itself: absent is `null`, a non-empty string is kept, `""` and any other type refuse. Locally the live harness ran against a scratch database (`scripts/testdb.py`), never the demo data. 15 mutations (R2.10C-M231 to M245): 758 in all. |
| R2.10C (application closure) | 2026-11-17 | **The application-wide import-closure audit, approved by you on 2026-11-17; nothing measured, no baseline, nothing pinned.** `app.main`, both conftests and the C.4 test load 209 repository files; 131 were neither fingerprinted nor excluded. A coverage trace of the live harness on a scratch database showed 17 of them run during the live harness and 114 only at import. `api/v1/router.py`, which dispatches every measured request, is now fingerprinted (47 sources); the other 130 are excluded under six reasons (§12.3): 171 exclusions. `test_the_application_import_closure_is_fingerprinted_or_excluded_by_reason` holds the closure to the lists, and the forbidden-use check now covers 168 audited exclusions. 4 mutations (R2.10C-M246 to M249): 762 in all. |
| R2.10C.4 (baseline) | 2026-11-18 | **Measured and pinned; recorded here at C.7.** CI run 44194081637 on `3a5f49a` measured `knowledge_retrieval_v2` and was red only on the no-pin gate, as decided on 2026-11-17; its other six jobs passed. The artifact `retrieval-eval-44194081637-1` was verified with `python -m tests.evaluation.retrieval_artifacts verify` and its two files pinned, bytes unchanged, in `b6defd7` (after `b9c56e2` admitted the pin by exact digest): report SHA-256 `a6ddeec598d264ff48d39f73f607d797f2d4da15be8252a6c0774ba9a78b0c15`, environment `6836b5a378a3022a5b81555cff864ccc80fcb790c679b238cb37e9595468cbf7`, fingerprint `8871ff6632a1fcd280beaf94865e65e7ceecc01948be281492681aec40141204`. CI run 44210410736 on `b6defd7` passed all 7 jobs, its measured report byte-identical to the pin: gate 2 holds. Nothing was tuned. The results, and §5's targets against them, are §1.4: Success@5 23 of 45, every paraphrase and title-only query empty, scope leakage 0 of 57. |
| R2.10C.5 | 2026-11-18 | **Layer 1's snapshot, `SnapshotKnowledge` and gate 3, implemented; verified locally, not yet in CI.** Your decisions of 2026-11-18 settle what §3.3 and the R2.10C.3a row left to C.5: **the copilot corpus alone** -- the seven `knowledge_retrieval_v2` documents copied from `fixture_documents.py` (`CORPUS[0]` to `CORPUS[6]`, the superseded pool policy, the withdrawn spa page and the neighbour's rooftop bar included), each mapped to exactly one fixture chunk, so served results carry the identifiers the copilot evaluation's citations resolve to; **keyed by (query, limit)**, a miss refusing; **every search the copilot evaluation makes**, 17 pairs read from the checksum-pinned reference replays (10, each limit the search tool's argument default) and R2.9Y capture (11), four shared; **produced locally** through the real API on a scratch PostgreSQL 18.6 database (`scripts/testdb.py`), never the demo data, with CI to confirm it against `postgres:18.6`. The snapshot `tests/evaluation/datasets/copilot_retrieval_snapshot_v1.json` (canonical §12.2 bytes, SHA-256 `92f0000da79ab72b0ea80e3a097bb3722864868562fbbe88ea83eda7b2acba93`) records, per search, the ranked `(doc_id, ordinal, served_sha256)` rows of §12.4: 5 of the 17 return a chunk (`dog fee`, `parking cost`, `parking cost per night`, `pool towels`, `towels provided at the pool`), the other 12 nothing -- §1.3's predictions for these queries, now recorded. `tests/evaluation/copilot_retrieval_snapshot.py` loads it only if its bytes hash to the pin, its corpus and searches are exactly these, and every result is an active chunk of the evaluated hotel whose served hash is its fixture chunk's. `SnapshotKnowledge` stands where `KnowledgeService` does, as `FixtureKnowledge` does; because the tool layer turns a service exception into a failed tool result, every miss is also recorded and `knowledge_harness.run(..., knowledge_source="snapshot")` refuses the run. **`FixtureKnowledge` stays the default:** a stand-in run is unchanged field for field and the R2.9Y reference report stays byte-identical; a snapshot run carries a `knowledge_source` block (`postgresql_snapshot`, the snapshot id and SHA-256) and its own statement (§10). **Gate 3** (§5.1.3) is `tests/integration/test_copilot_retrieval_snapshot.py`: C.4's `LiveHarness` under requirements A to C ingests the copilot corpus, the state is compared with the plan, every served result is checked (§12.4), the 17 searches are sent, a leaking result refuses, and the live document must equal the pinned bytes. It lands in CI shard 1 by construction; locally it passed on the scratch database. Regeneration is opt-in (`COPILOT_RETRIEVAL_SNAPSHOT_WRITE=1`) and refused in CI. **No fingerprinted file changed**, so C.4's pinned report stays valid; `ci.yml`'s comment still says 762 mutations, because correcting it would force a re-measurement and re-pin (§12.8). 18 mutations (R2.10C-M250 to M267, M267 a search that loses its language's stemming, which gate 3 alone must catch): 780 in all. |
| R2.10C.6 | 2026-11-19 | **`retrieval_failure` from the snapshot, beside the stand-in's; offline, no database.** C.5 passed CI first: run 44396391885 on `8bb5bff`, all 7 jobs green, gate 3 run in shard 1 against `postgres:18.6` and all 780 mutations killed. `knowledge_harness.run(..., knowledge_diagnostics=True, snapshot_retrieval_failure=True)` adds a `snapshot_retrieval_failure` block (`copilot_snapshot_retrieval_failure_v1`, `tests/evaluation/snapshot_retrieval_failure.py`) beside `knowledge_diagnostics`. **One rule:** both verdicts are R2.10B's `knowledge_correctness.assess`, unchanged, with its checksum: the stand-in's on what the model was shown, the snapshot's on the same searches with each returned result list replaced by what `copilot_retrieval_snapshot_v1` holds for its (query, limit), the limit decided by the search tool's argument model as in C.5. A search that errored or whose result was never shown keeps its outcome. **Labelled (§10):** the block names both sources -- `fixture` (`fixture_documents.py`) and `postgresql_snapshot` with the snapshot id and SHA-256 -- and its statement says what the snapshot's verdict is not: the model was shown the stand-in's results, so it describes real search for the searches this model made, not what the model would have searched for or answered. **It refuses** a returned search the snapshot does not hold, a key that is not the search the stand-in answered (query, limit and titles are checked against the stand-in), a stand-in verdict that is not the `knowledge_diagnostics` row's, the block without `knowledge_diagnostics`, and a run the snapshot rather than the stand-in served. **Opt-in and additive:** a report without the block is unchanged, and the R2.9Y reference report stays byte-identical. **Results, offline:** on the reference replays `retrieval_failure` is true for 0 of 10 judged cases with the stand-in and 4 of 10 with the snapshot (`pool_hours`, `parking_price`, `pool_closing_changed`, `guest_faq_injected`: real search returns nothing for `pool opening hours`, `parking price`, `pool closing time` and `guest FAQ`). On all six R2.9Y attempts it is 1 of 10 with the stand-in (`dog_fee`, `dog pet cost`) and 4 of 10 with the snapshot, adding `pool_hours`, `pool_closing_changed` and `guest_faq_injected` (`FAQ` with limit 20); `parking_price` (`parking cost per night`) is adequate under both. These are §1.3's predictions for these queries, now computed from the recorded snapshot. Neither verdict says whether the query or the search is at fault. No fingerprinted file changed, so C.4's pinned report and C.5's snapshot stay valid; `ci.yml`'s comment still says 762 mutations (§12.8). 15 mutations (R2.10C-M268 to M282): 795 in all. |
| R2.10C.7 | 2026-11-19 | **Final documentation; the last step of §8.** C.6 passed CI first: run 44518385846 on `da258e3`, all 7 jobs green and all 795 mutations killed. This record now says what was built and measured: its status header, a new §1.4 with C.4's pinned results set against §5's targets (Success@5 23 of 45, not the 0.90 target; empty-result rate 22 of 45, not the 0.10 target; shared words 17 of 17, the only family to meet its 0.80 target; paraphrase 0 of 10; scope leakage 0 of 57) and C.5's confirmation of all ten of §1.3's predictions, §7's local-database bullet (a scratch database now exists; the pinned report is still CI's), §8's status and the R2.10C.4 (baseline) row above, which §11 had lacked. `README.md` links this record from a new paragraph on the layered retrieval evaluation; `docs/copilot-evaluation.md` gains §8.5 and points §8.1's production-retrieval caveat at C.6's block; `tests/README.md` lists the R2.10C test modules. §3.5 is unchanged (SHA-256 `c86149e5e421a8016d3b4228e2ef0edc32f8b3cc8e804f31999b5e1972141458`), as is every frozen, pinned and fingerprinted file; no code changed and no mutation was added: 795 in all. |

Until C.4 (§1.4), no measurement had used these definitions. Nothing in this record's earlier
measured results changes:
`knowledge_retrieval_v1`'s 16 of 23 and its Wilson interval, and every R2.9Y result, were produced
before them and stay exactly as recorded. At R2.10C.2.1 the MRR@5 grade and the nDCG@5 gains were
still open; R2.10C.3a resolved them (§4). Neither amendment changes a measured result.

## 12. Retrieval fingerprint and measurement integrity (decision F)

Approved 2026-11-09 and recorded here in R2.10C.3a. **Implemented, nothing measured:** the
numeric contract (§12.5, `tests/evaluation/retrieval_numerics.py`) and the fingerprint
machinery (§12.1-12.4, §12.6, §12.7: `retrieval_canonical.py`, `retrieval_fingerprint.py`,
`retrieval_probes.py`, `retrieval_artifacts.py`, the `ci.yml` steps and the `.gitattributes`
entry). The baseline itself is C.4's (§11, R2.10C fingerprint); it was measured and pinned on
2026-11-18 (§1.4).

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
- `null` appears in exactly four places, each written as a typed JSON `null` that the
  fingerprint covers, never omitted and never written as `""`:
  - the document state's `supersedes`;
  - `database.locale` (`pg_database.datlocale`), if and only if the locale provider is `libc`,
    under which PostgreSQL defines it as NULL;
  - the init options (`pg_ts_dict.dictinitoption`) of the dictionary `pg_catalog.simple` alone,
    which PostgreSQL creates without options;
  - `engine.platform.variant`, if and only if Docker's image inspection has no `Variant` field
    (an image without a variant). A present `Variant` must be a non-empty string.
- `""` never stands for absence. It is a value only where §12.3 says so.
- Unordered lists are sorted; ordered lists keep their order.

### 12.3 Inputs

| Group | Contents |
|---|---|
| engine | the declared image reference and digest, read from the `ci.yml` blob at the measured commit and matched by a workflow variable; the local image id, with the Docker image-store type that gives it meaning; os, architecture and variant (`null` if and only if Docker reports no `Variant`; a present empty or non-string variant refuses); `server_version_num` and `version()`; extensions |
| database | server and client encoding, locale provider, collate, ctype, locale (`null` if and only if the provider is `libc`), collation version |
| text search | the effective `search_path` (`current_schemas(true)`) and each configuration's schema; configurations, parsers, token types (descriptions never empty), mappings, dictionaries, templates (`lexize` functions never empty), init options (`null` only for `pg_catalog.simple`, never empty); the SHA-256 of every referenced stop-word file (read as a superuser) |
| document state, chunk state | §12.4 |
| query parsing | `(query_id, configuration, limit, websearch_to_tsquery(configuration, query)::text)` for every query and every language of the evaluated hotel's active documents; the tsquery may be empty, which PostgreSQL returns for a query of stop words alone |
| schema | the Alembic head; a canonical digest of the live definitions of `hotel_documents` and `hotel_document_chunks` |
| sources | Git blob content (§12.6) of `.github/workflows/ci.yml`, `.gitattributes`, `pyproject.toml`, `alembic.ini`; `backend/app/main.py`, `api/deps.py`, `api/v1/router.py`, `api/v1/endpoints/knowledge.py`, `services/knowledge.py`, `services/scope.py`, `repositories/hotel.py`, `repositories/knowledge.py`, `knowledge/chunking.py`, `schemas/knowledge.py`, `models/knowledge.py`, `models/enums.py`; `database/migrations/env.py` and every file under `database/migrations/versions/`; `tests/conftest.py`, `tests/integration/conftest.py`, `tests/evaluation/retrieval.py`, `retrieval_schema.py`, `retrieval_metrics.py`; the C.4 harness, `tests/evaluation/retrieval_canonical.py`, `retrieval_fingerprint.py`, `retrieval_probes.py`, `retrieval_artifacts.py`, `retrieval_numerics.py`, `retrieval_review.py`, `retrieval_signoff.py` and `retrieval_v2.py`; the C.4 integration test `tests/integration/test_knowledge_retrieval_v2_eval.py`; the dataset JSON; the package `__init__.py` files of `tests/`, `tests/evaluation/`, `tests/integration/`, `backend/app/` and its `api/`, `api/v1/`, `api/v1/endpoints/`, `services/`, `repositories/`, `knowledge/`, `schemas/`, `models/` and `db/`; `backend/requirements-lock.txt` and `backend/requirements-dev-lock.txt` |
| python | CPython 3.14.7 and its build string; the float format (53-bit mantissa, radix 2, maximum exponent 1024, short repr); the `_decimal` and libmpdec versions; the SHA-256 of the canonical installed set |
| dataset | identity, schema version, canonical SHA-256 |
| metrics | the metrics version; `V2_METRIC_CONFIG` in full, bootstrap settings included; the discount table and the numeric contract's identifier |

`retrieval_review.py` and `retrieval_signoff.py` feed no input group; they are fingerprinted
because they decide which sign-off is accepted.

**Excluded, each by reason.** Every excluded file must exist at the measured commit. Every
repository file a fresh interpreter loads to import the measurement path, or to import the
application (`app.main`), both conftests and the C.4 test, is fingerprinted or excluded here,
and two tests hold those import closures to this list.
- `database/migrations/script.py.mako`: never executed.
- `core/config.py`, `core/client_address.py`, `core/errors.py`, `core/request_id.py`,
  `core/security_headers.py`, the scope policy and `tests/db_safety.py`: they can only refuse.
- `core/__init__.py`: a docstring; its package holds no fingerprinted module.
- `db/base.py` (the declarative base, identity key and timestamp mixin): the tables' DDL,
  identity generation included, is defined in the fingerprinted migrations and read from the
  live definitions. (This record once named `models/base.py`, which does not exist.)
- `db/session.py`: the application's engine. The measurement's sessions are the fingerprinted
  integration conftest's, which overrides `get_db`.
- `models/audit.py`, `models/booking.py`, `models/copilot_conversation.py`, `models/demand_observation.py`, `models/finance.py`, `models/guest.py`, `models/hotel.py`, `models/llm_invocation.py`, `models/membership.py`, `models/metrics.py`, `models/payment.py`, `models/platform.py`, `models/prediction.py`, `models/review.py`, `models/room.py` and `models/user.py`: they map tables outside §12.4's measured state, so they can only
  refuse.
- `schemas/common.py`: the error and page envelopes. An error can only refuse, and the
  measurement's responses are knowledge schemas.
- `schemas/amenity.py`, `schemas/analytics.py`, `schemas/auth.py`, `schemas/booking.py`, `schemas/finance.py`, `schemas/guest.py`, `schemas/health.py`, `schemas/hotel.py`, `schemas/intelligence.py`, `schemas/meta.py`, `schemas/payment.py`, `schemas/review.py`, `schemas/room.py` and `schemas/room_type.py`: models of other endpoints, loaded by the package's
  `__init__`. No measured value passes through them.

The application-wide closure (C.4 audit, approved 2026-11-17) adds 130 exclusions. A trace of
the live harness over a scratch database (coverage, per test) showed which files run on the
measured requests; no file outside the import closure ran. `api/v1/router.py`, which builds the
route table every measured request is dispatched through, is fingerprinted instead.
- `api/v1/endpoints/auth.py`, `api/v1/endpoints/hotels.py`, `services/auth.py`,
  `services/hotel.py`, `services/audit.py`, `repositories/user.py`, `repositories/membership.py`
  and `repositories/audit.py`: they register and sign in the harness user and create its two
  hotels. They write only users, memberships, hotels and audit events, outside §12.4's measured
  state, and otherwise can only refuse; the knowledge service uses only the hotel's `id`, which
  the fingerprinted resolver and repository return.
- `core/security.py`, `core/rate_limit.py`, `services/authorization.py` and
  `middleware/request_size.py`: they run on the measured requests but can only refuse.
- `middleware/request_id.py` and `middleware/security_headers.py`: they add response headers,
  never the body.
- `api/openapi.py`: it changes only the OpenAPI document.
- `repositories/room_type.py` and `repositories/ml_prediction_removal.py`: constructed by a
  dependency the measured routes share; none of their methods is called.
- The other 113, every remaining module of `api/` (`preconditions.py`, `routes/`, the 24 other
  endpoint modules), `copilot/`, `llm/`, `ml/`, `core/logging.py`, `middleware/__init__.py`,
  16 repositories, 12 schemas and 28 services: loaded by the application's imports; only their
  import-time definitions run, and nothing in them runs on a measured request. That is what the
  trace observed, not what the code guarantees: if a later change made one of them run and alter
  a result, gate 2 still fails, as "results changed with unchanged inputs".

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
- **Wilson's square root.** `Decimal.from_float(x).sqrt()`, certified. Convert the binary
  floating-point radicand exactly using `Decimal.from_float(x)`, then calculate its square root
  using the certified local Decimal context. This replaces `math.sqrt`. The conversion must not
  set the `FloatOperation` signal. Only `Inexact` and `Rounded` may be set during the certified
  calculation.
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
  - The hash-seed test. It verifies that numeric evaluation outputs are independent of
    `PYTHONHASHSEED`: fresh Python subprocesses with the explicit seeds `0`, `1`, `4294967295`
    and `random` run the identical numeric evaluation, and their canonical JSON outputs are
    compared byte for byte, execution metadata excluded; any differing numeric output fails.
> **Clarified (2026-11-12, approved by you).** Two clarifications, nothing else changed. (A)
> The text read `Decimal(x).sqrt()`. In CPython, `Decimal(x)` sets `FloatOperation`: inside the
> local context that breaks "only `Inexact` and `Rounded` may be set", and outside it breaks
> "used only in local contexts". `Decimal.from_float(x)` gives the identical exact value and
> sets no flag. (B) "The hash-seed test" had no further definition; it is the
> `PYTHONHASHSEED`-independence test above. Every other requirement of this section stands.

- **The claim.** Results are correctly rounded and reproducible within the fingerprinted runtime.
  They rely on the documented correct rounding of `_decimal`. There is no cross-platform proof.

### 12.6 Source hashing, CI artifacts and the baseline

**Source hashing.**
- The measured commit is `git rev-parse --verify HEAD^{commit}`, and the tracked tree must be
  clean.
- Every source must be a blob at that commit. Its hash is the SHA-256 of
  `git cat-file blob <commit>:<path>`, recorded with the blob id. No line-ending conversion is
  involved.
- Every fingerprinted Python module must be imported from its repository path, and every
  excluded source (§12.3) must be a blob at that commit.
- The measured tree holds no untracked file, and no ignored file Python could execute: `.py`
  files (`conftest.py`, `sitecustomize.py` and `usercustomize.py` among them), `.pyc`, `.pyd`,
  `.so` and `.pth`. Only the cache and environment directories `.git`, `.venv`, `venv`,
  `__pycache__`, `node_modules`, `.mypy_cache`, `.pytest_cache` and `.ruff_cache` are exempt.
- The measured commit's tree id is recorded in the environment record.

**Artifacts.**
- Before the tests, a workflow step writes the Docker metadata file (from `docker inspect`,
  `docker image inspect` and `docker info`).
- The C.4 integration test writes to `$RETRIEVAL_EVAL_OUT`, which the workflow sets to
  `$RUNNER_TEMP/retrieval-eval`. It must be an absolute path that resolves, symlinks followed,
  outside the repository; otherwise the run refuses and writes nothing, since no directory is
  safe to write to. When the run starts the directory holds `docker.json` and nothing else.
- On success the test writes `report.json`, `environment.json` and `report.json.sha256`
  (written last), atomically and before any assertion. On refusal it writes `refusal.json`
  instead. A run leaves exactly `docker.json`, `report.json`, `environment.json` and
  `report.json.sha256`, or exactly `docker.json` and `refusal.json`.
- Every file name and pinned path is a constant; no argument or variable overrides one.

| Artifact | CI path | Pinned path |
|---|---|---|
| report | `$RUNNER_TEMP/retrieval-eval/report.json` | `tests/evaluation/retrieval_report_v2.json` |
| environment | `$RUNNER_TEMP/retrieval-eval/environment.json` | `tests/evaluation/retrieval_report_v2.environment.json` |
| report digest | `$RUNNER_TEMP/retrieval-eval/report.json.sha256` | not committed; its value is in the pin commit's message |
| refusal | `$RUNNER_TEMP/retrieval-eval/refusal.json` | never pinned |

- A shard-1 step with `if: always()` prints every file's SHA-256 to the log and uploads the files
  with `actions/upload-artifact`, pinned by SHA, with `if-no-files-found: error`, named by run id
  and attempt.

**Pinning.**
1. Download with `gh run download <run> -n retrieval-eval-<run>-<attempt> -D <directory outside
   the repository>`. That uses the network and needs your authorisation each time.
2. Check, with `python -m tests.evaluation.retrieval_artifacts verify <directory> --log-digest
   <report SHA-256> --environment-log-digest <environment SHA-256>`, both digests read from the
   job log, that:
   - the run did not refuse (reported first), and the directory holds exactly the four files of
     a successful run, as regular files, with nothing extra, stale or partial;
   - the report's SHA-256 equals both its `.sha256` file and the report's digest in the job log;
   - `environment.json`'s SHA-256 equals its digest in the job log. Only then is its tree id
     trusted;
   - the fingerprint equals SHA-256(canonical(`inputs`));
   - the checked-out commit's tree is exactly the recorded tree id. A different commit with that
     tree, such as a pull request's merge commit, qualifies; a branch name or a commit message
     never does;
   - the repository-derived components recompute identically offline at that commit.
3. Copy the bytes unchanged to the two pinned paths and commit them alone (the report and its
   environment record), with the report SHA-256, the fingerprint, the CI run and the artifact
   digest in the commit message.
4. Both pinned files are marked `-text` in `.gitattributes`.

**C.4's requirements** (approved 2026-11-17, after the audit of §12.3's exclusions). The 38
modules §12.3 excludes beyond its original three were audited one by one and are accepted on
three conditions the C.4 integration test must meet. Each is mandatory; the harness provides
the means (`retrieval_artifacts.py`, `retrieval_probes.py`), and offline tests hold them.
- **A. One engine, the conftest's.** The test builds its application with the integration
  conftest's `create_test_app`, so every request reaches the database through the conftest's
  `get_db` override. It reads the database through `EngineFetch` over the same conftest engine,
  and calls `require_measurement_wiring(app, fetch)` before measuring, which refuses a missing
  override or one bound to another engine. It measures inside `production_sessions_forbidden()`,
  under which the production session factory refuses, so a request that bypasses the override
  stops the run instead of falling back to it.
- **B. Hotel ownership by SQL.** `UploadRecord.hotels` is built with `probes.hotel_ids`: each
  harness hotel's internal id read by SQL from the public id the API returned, never through the
  ORM `Hotel` model. §12.4's state comparison then holds every document to its role, so a wrong
  hotel resolution refuses rather than scoring another hotel's documents.
- **C. An explicit status for every response.** Every ingest and search response passes
  `checked_json(response, <expected status>, <what>)`. A different status, a body that is not a
  JSON object, or an exception the client raises stops the run (`unmeasurable`); none becomes a
  result.

**Session settings.** The production session factory (`app.db.session`) uses `autoflush=False`;
the integration conftest's override uses SQLAlchemy's default, `autoflush=True`. C.4 measures the
conftest-controlled environment, whose implementation (`tests/integration/conftest.py`) is
fingerprinted. Neither setting changes.

**C.4 acceptance tests**, which inspect the C.4 test (`tests/evaluation/test_retrieval_c4_requirements.py`,
by AST):
- it calls `create_test_app`, builds `EngineFetch` from the conftest `engine`, calls
  `require_measurement_wiring` before ingesting, and measures inside
  `production_sessions_forbidden()`;
- it builds `UploadRecord.hotels` with `probes.hotel_ids` and nothing else;
- every client request's response is the first argument of a `checked_json` call with a literal
  status;
- it passes what it measured, with the pinned report's bytes, to `gate_problems` and fails on any
  problem;
- the import-closure test extends to the application (`app.main`), both conftests and the C.4
  test (`test_the_application_import_closure_is_fingerprinted_or_excluded_by_reason`): 209
  repository files, each fingerprinted or excluded by reason (§12.3, the application-wide
  closure audit approved 2026-11-17).

**The C.4 test** (`tests/integration/test_knowledge_retrieval_v2_eval.py`, approved 2026-11-17).
Through the real API it registers one user, creates the evaluated and the neighbour hotel
(`POST /api/v1/hotels`, 201), uploads in `probes.plan` order (201), posts the new version through
the supersede route (201) and the withdrawal (200), and searches the evaluated hotel (200), every
response through `checked_json`. `run_measurement` writes the artifacts; then `gate_problems`
applies §5.1's gates 1 and 2: scope leakage = 0, from the overall count and every query's row,
and the report equal to the pinned one, compared as canonical JSON reads both back (fingerprint,
then results, then byte for byte). Gate 3 is C.5's.
- **The first run.** With no report pinned, gate 2 fails after the artifacts are written: that
  run is the baseline, and its CI is red until the report is pinned (§12.6 "Pinning"); the pin
  commit changes no fingerprinted file, so its CI measures the same inputs and compares.
- **Where it measures.** Only in CI shard 1, which provides `$RETRIEVAL_EVAL_OUT` and
  `docker.json`. Outside CI (no `CI=true`) the measurement is skipped with that reason; in CI a
  missing directory refuses, so moving the test out of shard 1 cannot pass unseen.
- **Without measuring**, a second test drives the same live harness through the real API and
  PostgreSQL (state comparison, every served result), wherever a test database is.

**Order.** The `ci.yml` steps and the `.gitattributes` entry are committed before the baseline
measurement, which runs on a commit that already contains them. The pin commit changes no
fingerprinted file. Artifacts and logs expire; the committed report and its digests are the
permanent record.

### 12.7 Failing closed

A measurement refuses (writing `refusal.json` and no report) on any of:
- a required input that can't be measured, or a probe that fails;
- a NULL other than the two catalog exceptions below, an expected-versus-actual mismatch, a
  vector-binding failure, or a `content_checksum` or `supersedes` mismatch;
- a hotel-scope violation, a broken bijection, a served field that differs, a wrong echo, or more
  results than the limit;
- an image-identity failure, a present Docker `Variant` that is empty or not a string included;
- a stop-word file that can't be read;
- an installed-set, constraint, `RECORD` or marker failure;
- a numeric certificate failure;
- no Git, a dirty tracked tree, an untracked file or an ignored file Python could execute (§12.6),
  a source that is missing, untracked or imported from elsewhere, or an excluded source missing
  at the commit;
- an output directory that already holds anything but `docker.json`;
- a sign-off that is missing or doesn't match.

An output directory that is relative or resolves inside the repository refuses as well, but
writes nothing at all, not even `refusal.json`.

**Empty strings.** A token type's description, a template's `lexize` function and every init
options value must be non-empty; an empty one refuses. A tsquery may be empty.

**The two catalog exceptions** (approved 2026-11-13). PostgreSQL defines these NULLs; each is
recorded as a typed `null` (§12.2):
- `pg_database.datlocale` is `null` if and only if the locale provider is `libc`. A value under
  `libc`, or a NULL under `builtin` or `icu`, refuses. `datcollate`, `datctype` and the
  collation version stay required.
- `pg_ts_dict.dictinitoption` may be `null` only for `pg_catalog.simple`. A NULL for any other
  dictionary refuses, and so does an empty string for any dictionary.

The fourth `null` of §12.2, `engine.platform.variant`, is Docker's absent field, not a catalog
NULL.

No "unknown" or placeholder value ever enters `inputs`, and no `null` beyond §12.2's four.

### 12.8 Limits

- **Unverified until C.4 runs**, each refusing the run if absent:
  - the runner's Docker image store and inspect fields;
  - superuser access to the stop-word files;
  - `packaging` in the dev lock;
  - the `_decimal` and libmpdec versions.
- The platform manifest digest is implied, not observed.
- Reports are exactly comparable only within the fingerprinted environment.
- Any `ci.yml` edit forces a re-measurement and re-pin.
