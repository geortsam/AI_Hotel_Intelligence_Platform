"""Retrieval metrics for a `knowledge_retrieval` dataset (R2.10C.2). Pure: no search, no database.

`docs/retrieval-evaluation-decision.md` §4 defines the metrics. This module computes them from
**ranked results supplied by the caller** -- the chunks a search returned for each query, in its
order -- and the dataset's judgments (`retrieval_schema`). It never runs a search.

## What the decision record left open, and how this module keeps it open

Two semantic choices are unresolved (decision record §4). Neither has a default here:
`MetricConfig` must be given each one explicitly, and every report records the configuration it
was computed under.

    mrr_min_grade                 the lowest grade MRR@5 counts as relevant: 1 or 2
    ndcg_gains                    the gain of grades 0, 1 and 2 for nDCG@5, e.g. (0, 1, 2) for "the
                                  grade itself" or (0, 1, 3) for 2^grade - 1; grade 0 gains 0
    no_answer_lenient_min_grade   must be given, and must be 1 (R2.10C.2.1): a no-answer query has
                                  no grade-2 chunk, so a threshold of 2 could never fail

Fixed by the decision record, and so not configurable: Success@5 and Recall@5 count grade 2 only,
the cut-off is the dataset's (5), rates carry Wilson 95% intervals and means carry query-level
bootstrap 95% intervals (below).

## Definitions

Let *top* be the first `cutoff` results of a query, in the order supplied.

Answerable queries are the denominator of the first five; the mean is over them for Recall, MRR
and nDCG.

- **Success@5:** a grade-2 chunk is in *top*.
- **Recall@5:** the grade-2 chunks in *top*, over the query's grade-2 chunks.
- **MRR@5:** 1 / the rank of the first chunk in *top* graded at least `mrr_min_grade`, else 0.
- **nDCG@5:** DCG / ideal DCG. DCG sums gain(grade) / log2(rank + 1) over *top*; the ideal DCG
  is the same sum over the query's judged grades, highest first, cut at `cutoff`.
- **Empty-result rate:** the search returned nothing at all.
- **No-answer, strict** (over no-answer queries): the search returned nothing at all.
- **No-answer, lenient** (over no-answer queries): nothing returned is graded at least
  `no_answer_lenient_min_grade`.
- **Scope leakage** (over all queries): something returned, at any rank, is a chunk of a
  document that is not an active document of the evaluated hotel. It is derived from the
  corpus's own document metadata, never from a query's `forbidden` list.

Ranks start at 1. The discount `log2(rank + 1)` is the one `insight_ranking.py` uses.

**Answerable and no-answer queries never share a denominator.** The answerable-only functions
refuse a no-answer query rather than score it 0, so no caller can dilute a rate by accident.

**Fewer than five results** are scored as they are: nothing is padded, and a missing rank is a
miss. **No results** score 0 on every answerable metric and count as empty. **More than five**
(the model may ask for up to 20): Success, Recall, MRR and nDCG read *top* only; emptiness, both
no-answer checks and scope leakage read everything returned, since that is what the model was shown.

**Order and ties.** The supplied order is the ranking: production's is total (relevance, then
document, then ordinal), and nothing here reorders it. Ties in the ideal ordering of nDCG cannot
change its value. Queries are reported by `query_id`, families by name.

**Invalid results are refused, never repaired** (`ResultsError`): a query of the dataset with no
results entry, an entry for a query the dataset does not have, an item that is not a
`(doc_id, ordinal)` pair, a chunk the corpus does not hold, the same chunk twice in one ranking,
or more results than the query's `limit` (R2.10C.3a). A duplicate or an overlong ranking is
refused rather than trimmed because production search cannot return one, so it can only mean the
harness is wrong.

**The approved configuration** for `knowledge_retrieval_v2` is `V2_METRIC_CONFIG`: MRR@5 at grade
2, nDCG@5 gains (0, 1, 3), the lenient check at grade 1 (decision record §4, decisions A and B).

**`forbidden` is an annotation.** It names the scope traps a query was written to spring. Each
report row lists the ones returned (`trap_hits`). Leaving a chunk out of it cannot hide leakage:
leakage is decided by the corpus, so every out-of-scope chunk counts for every query.

## Intervals

**Rates** -- Success@5, the empty-result rate, both no-answer checks and scope leakage -- carry a
**Wilson 95%** interval with their count and n.

**Means** -- Recall@5, MRR@5 and nDCG@5 -- are averages of per-query values in [0, 1], not counts of
successes, so Wilson does not apply. They carry a **percentile bootstrap 95%** interval: the
query-level values are resampled with replacement, `BOOTSTRAP_RESAMPLES` (10,000) times, from a
`random.Random(BOOTSTRAP_SEED)` generator (seed 20261108). Each resample's mean is the metric's own
macro average; the interval is the 2.5th and 97.5th percentiles of those means by nearest rank. The
resampling unit is the query, never a chunk. Each interval starts a fresh generator from the seed,
so the three means of one group are resampled on the same draws.

- Below **n = 10** no interval is reported, and the block says why.
- An interval whose ends are equal is reported and flagged `zero_width`: it is a sign that every
  query scored alike, not that the value is certain.
- The interval is **descriptive**. It assumes the queries are exchangeable; queries that share a
  document, or that were captured from the same case, weaken that assumption.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final

from tests.evaluation.retrieval_schema import (
    GRADES,
    SCHEMA_VERSION,
    SUFFICIENT,
    ChunkRef,
    DatasetError,
    DatasetQuery,
    RetrievalDataset,
)

METRICS_VERSION: Final = "knowledge_retrieval_metrics_v1"

#: The two-sided 95% normal quantile.
WILSON_Z_95: Final = 1.959963984540054
DISCOUNT: Final = "log2(rank + 1)"
DIGITS: Final = 4

#: The query-level bootstrap for the means (Recall@5, MRR@5, nDCG@5). See the docstring.
BOOTSTRAP_RESAMPLES: Final = 10_000
BOOTSTRAP_SEED: Final = 20261108
BOOTSTRAP_MIN_N: Final = 10
BOOTSTRAP_METHOD: Final = "percentile bootstrap, queries resampled with replacement"

#: The only lenient no-answer threshold that can fail: a no-answer query has no grade 2.
LENIENT_MIN_GRADE: Final = 1


class ConfigurationError(ValueError):
    """A metric configuration is missing, malformed or contradicts the grades' meaning."""


class ResultsError(ValueError):
    """Supplied results do not match the dataset. Nothing is repaired or dropped."""


def _threshold(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2):
        raise ConfigurationError(f"{name} must be 1 or 2, given explicitly; got {value!r}")
    return value


def _lenient_threshold(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != LENIENT_MIN_GRADE:
        raise ConfigurationError(
            f"{name} must be {LENIENT_MIN_GRADE}: a no-answer query has no grade-2 chunk, "
            f"so a higher threshold could never fail; got {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class MetricConfig:
    """The choices the decision record leaves open. Every field is required; none has a default."""

    mrr_min_grade: int
    ndcg_gains: tuple[float, float, float]
    no_answer_lenient_min_grade: int

    def __post_init__(self) -> None:
        _threshold(self.mrr_min_grade, "mrr_min_grade")
        _lenient_threshold(self.no_answer_lenient_min_grade, "no_answer_lenient_min_grade")
        gains = self.ndcg_gains
        if not isinstance(gains, tuple) or len(gains) != len(GRADES):
            raise ConfigurationError("ndcg_gains must give one gain per grade: (g0, g1, g2)")
        for gain in gains:
            if (
                isinstance(gain, bool)
                or not isinstance(gain, int | float)
                or not math.isfinite(gain)
            ):
                raise ConfigurationError(f"every gain must be a finite number; got {gain!r}")
        if gains[0] != 0:
            raise ConfigurationError("grade 0 is not relevant, so its gain must be 0")
        if not 0 <= gains[1] <= gains[2] or gains[2] <= 0:
            raise ConfigurationError("gains must satisfy 0 <= g1 <= g2 and g2 > 0")

    def as_dict(self) -> dict[str, Any]:
        return {
            "mrr_min_grade": self.mrr_min_grade,
            "ndcg_gains": {
                str(grade): gain for grade, gain in zip(GRADES, self.ndcg_gains, strict=True)
            },
            "no_answer_lenient_min_grade": self.no_answer_lenient_min_grade,
            "fixed": {
                "success_min_grade": SUFFICIENT,
                "recall_min_grade": SUFFICIENT,
                "discount": DISCOUNT,
                "rate_interval": "wilson",
                "z": WILSON_Z_95,
                "mean_interval": {
                    "method": BOOTSTRAP_METHOD,
                    "resamples": BOOTSTRAP_RESAMPLES,
                    "seed": BOOTSTRAP_SEED,
                    "min_n": BOOTSTRAP_MIN_N,
                    "percentiles": "2.5 and 97.5, nearest rank",
                },
                "leakage": "any returned chunk of a document not active at the evaluated hotel",
            },
        }


#: The configuration approved for `knowledge_retrieval_v2` (decision record §4, decisions A and
#: B): MRR@5 counts grade 2 only, so it is non-zero exactly when Success@5 holds; nDCG@5 gains
#: 2^grade - 1, so (0, 1, 3); the lenient no-answer check counts grade 1 and above.
V2_METRIC_CONFIG: Final = MetricConfig(
    mrr_min_grade=2, ndcg_gains=(0, 1, 3), no_answer_lenient_min_grade=1
)


# --- validating supplied results ------------------------------------------------------------------


def located(
    dataset: RetrievalDataset, results: Mapping[str, Sequence[Any]]
) -> dict[str, tuple[ChunkRef, ...]]:
    """Each query's ranking as chunk references, checked against the dataset. See `ResultsError`."""
    if not isinstance(results, Mapping):
        raise ResultsError("results must map each query_id to its ranked results")
    expected = {query.query_id for query in dataset.queries}
    missing = sorted(expected - set(results))
    if missing:
        raise ResultsError(f"no results for {missing}")
    extra = sorted(str(key) for key in results if key not in expected)
    if extra:
        raise ResultsError(f"results for queries the dataset does not have: {extra}")
    corpus = dataset.chunks()
    limits = {query.query_id: query.limit for query in dataset.queries}
    ranked: dict[str, tuple[ChunkRef, ...]] = {}
    for query_id in sorted(expected):
        items = results[query_id]
        if not isinstance(items, list | tuple):
            raise ResultsError(f"{query_id}: results must be a ranked list")
        if len(items) > limits[query_id]:
            raise ResultsError(
                f"{query_id}: {len(items)} results exceed its limit of {limits[query_id]}"
            )
        chunks: list[ChunkRef] = []
        for rank, item in enumerate(items, start=1):
            if (
                not isinstance(item, list | tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
            ):
                raise ResultsError(
                    f"{query_id} rank {rank}: not a (doc_id, ordinal) pair: {item!r}"
                )
            try:
                chunk = ChunkRef(item[0], item[1])
            except DatasetError as malformed:
                raise ResultsError(f"{query_id} rank {rank}: {malformed}") from malformed
            if chunk not in corpus:
                raise ResultsError(f"{query_id} rank {rank}: {item!r} is not a chunk of the corpus")
            chunks.append(chunk)
        if len(set(chunks)) != len(chunks):
            raise ResultsError(f"{query_id}: the same chunk is ranked twice")
        ranked[query_id] = tuple(chunks)
    return ranked


# --- per query -----------------------------------------------------------------------------------


def _top(ranked: Sequence[ChunkRef], cutoff: int) -> Sequence[ChunkRef]:
    return ranked[:cutoff]


def _answerable(query: DatasetQuery) -> None:
    if not query.answerable:
        raise ValueError(f"{query.query_id} is a no-answer query; it has no score here")


def success_at(query: DatasetQuery, ranked: Sequence[ChunkRef], cutoff: int) -> bool:
    _answerable(query)
    return any(query.grade_of(chunk) >= SUFFICIENT for chunk in _top(ranked, cutoff))


def recall_at(query: DatasetQuery, ranked: Sequence[ChunkRef], cutoff: int) -> Fraction:
    _answerable(query)
    required = query.sufficient
    found = required & set(_top(ranked, cutoff))
    return Fraction(len(found), len(required))


def first_rank_at(
    query: DatasetQuery, ranked: Sequence[ChunkRef], cutoff: int, min_grade: int
) -> int | None:
    """The rank (from 1) of the first chunk in the top *cutoff* graded at least *min_grade*."""
    for rank, chunk in enumerate(_top(ranked, cutoff), start=1):
        if query.grade_of(chunk) >= min_grade:
            return rank
    return None


def reciprocal_rank_at(
    query: DatasetQuery, ranked: Sequence[ChunkRef], cutoff: int, min_grade: int
) -> Fraction:
    _answerable(query)
    rank = first_rank_at(query, ranked, cutoff, _threshold(min_grade, "min_grade"))
    return Fraction(0) if rank is None else Fraction(1, rank)


def _dcg(gains: Sequence[float]) -> float:
    return math.fsum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))


def ndcg_at(
    query: DatasetQuery,
    ranked: Sequence[ChunkRef],
    cutoff: int,
    gains: tuple[float, float, float],
) -> float:
    _answerable(query)
    actual = [gains[query.grade_of(chunk)] for chunk in _top(ranked, cutoff)]
    ideal = sorted((gains[item.grade] for item in query.relevant), reverse=True)[:cutoff]
    return _dcg(actual) / _dcg(ideal)


def leaked(dataset: RetrievalDataset, ranked: Sequence[ChunkRef]) -> tuple[ChunkRef, ...]:
    """Every returned chunk, at any rank, whose document is not an active document of the
    evaluated hotel -- by the corpus's own metadata, whatever any query's `forbidden` list says."""
    documents = {document.doc_id: document for document in dataset.documents}
    found: list[ChunkRef] = []
    for chunk in ranked:
        document = documents.get(chunk.doc_id)
        if document is None:
            raise ResultsError(f"{chunk.doc_id!r} is not a document of the corpus")
        if document.hotel != "evaluated" or document.status != "active":
            found.append(chunk)
    return tuple(found)


def trap_hits(query: DatasetQuery, ranked: Sequence[ChunkRef]) -> tuple[ChunkRef, ...]:
    """The query's own `forbidden` scope traps among everything returned: an annotation only."""
    forbidden = set(query.forbidden)
    return tuple(chunk for chunk in ranked if chunk in forbidden)


def _no_answer(query: DatasetQuery) -> None:
    if query.answerable:
        raise ValueError(f"{query.query_id} is answerable; the no-answer checks do not apply")


def no_answer_strict(query: DatasetQuery, ranked: Sequence[ChunkRef]) -> bool:
    """Strict: the search returned nothing at all."""
    _no_answer(query)
    return not ranked


def no_answer_lenient(query: DatasetQuery, ranked: Sequence[ChunkRef], min_grade: int) -> bool:
    """Lenient: nothing returned is graded at or above *min_grade* for this query."""
    _no_answer(query)
    threshold = _lenient_threshold(min_grade, "min_grade")
    return not any(query.grade_of(chunk) >= threshold for chunk in ranked)


# --- intervals -----------------------------------------------------------------------------------


def wilson_interval(successes: int, n: int, z: float = WILSON_Z_95) -> tuple[float, float] | None:
    """Wilson's score interval for *successes* of *n*, rounded; `None` when n is 0."""
    for value, name in ((successes, "successes"), (n, "n")):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    if not (math.isfinite(z) and z > 0):
        raise ValueError("z must be a positive finite number")
    if n == 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return round(low, DIGITS), round(high, DIGITS)


def percentile_interval(means: Sequence[float]) -> tuple[float, float]:
    """The 2.5th and 97.5th percentiles of *means* by nearest rank: the values at ranks
    ceil(0.025 B) and ceil(0.975 B) of B sorted means, counted from 1. Integer arithmetic."""
    if not means:
        raise ValueError("a percentile interval needs at least one mean")
    ordered = sorted(means)
    count = len(ordered)
    low_rank = -(-25 * count // 1000)
    high_rank = -(-975 * count // 1000)
    return ordered[low_rank - 1], ordered[high_rank - 1]


def bootstrap_interval(
    values: Sequence[float | Fraction],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    min_n: int = BOOTSTRAP_MIN_N,
) -> dict[str, Any]:
    """A percentile bootstrap 95% interval for the mean of query-level *values*, each in [0, 1].
    Queries are resampled with replacement; below *min_n* queries there is no interval."""
    for setting, name in ((resamples, "resamples"), (seed, "seed"), (min_n, "min_n")):
        if isinstance(setting, bool) or not isinstance(setting, int):
            raise ValueError(f"{name} must be an integer")
    if resamples < 1 or min_n < 1:
        raise ValueError("resamples and min_n must be at least 1")
    if not isinstance(values, list | tuple):
        raise ValueError("values must be a list of query-level values")
    observations: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float | Fraction):
            raise ValueError(f"a query-level value must be a number; got {value!r}")
        number = float(value)
        if not (math.isfinite(number) and 0.0 <= number <= 1.0):
            raise ValueError(f"a query-level value must lie in [0, 1]; got {value!r}")
        observations.append(number)
    n = len(observations)
    block: dict[str, Any] = {
        "method": BOOTSTRAP_METHOD,
        "resamples": resamples,
        "seed": seed,
        "n": n,
        "interval": None,
        "zero_width": None,
        "reason": None,
    }
    if n < min_n:
        block["reason"] = f"n < {min_n}: no interval"
        return block
    generator = random.Random(seed)
    means = [
        math.fsum(observations[generator.randrange(n)] for _ in range(n)) / n
        for _ in range(resamples)
    ]
    low, high = percentile_interval(means)
    block["interval"] = [round(low, DIGITS), round(high, DIGITS)]
    block["zero_width"] = low == high
    return block


# --- aggregation ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    query_id: str
    family: str
    origin: str
    answerable: bool
    returned: tuple[ChunkRef, ...]
    top: tuple[tuple[int, ChunkRef, int], ...]
    empty: bool
    leaked: tuple[ChunkRef, ...]
    trap_hits: tuple[ChunkRef, ...]
    success: bool | None
    recall: Fraction | None
    reciprocal_rank: Fraction | None
    ndcg: float | None
    first_sufficient_rank: int | None
    no_answer_strict: bool | None
    no_answer_lenient: bool | None


def outcome(
    dataset: RetrievalDataset,
    query: DatasetQuery,
    ranked: Sequence[ChunkRef],
    config: MetricConfig,
) -> QueryOutcome:
    cutoff = dataset.cutoff
    top = tuple(
        (rank, chunk, query.grade_of(chunk))
        for rank, chunk in enumerate(_top(ranked, cutoff), start=1)
    )
    answerable = query.answerable
    return QueryOutcome(
        query_id=query.query_id,
        family=query.family,
        origin=query.origin,
        answerable=answerable,
        returned=tuple(ranked),
        top=top,
        empty=not ranked,
        leaked=leaked(dataset, ranked),
        trap_hits=trap_hits(query, ranked),
        success=success_at(query, ranked, cutoff) if answerable else None,
        recall=recall_at(query, ranked, cutoff) if answerable else None,
        reciprocal_rank=(
            reciprocal_rank_at(query, ranked, cutoff, config.mrr_min_grade) if answerable else None
        ),
        ndcg=ndcg_at(query, ranked, cutoff, config.ndcg_gains) if answerable else None,
        first_sufficient_rank=first_rank_at(query, ranked, cutoff, SUFFICIENT),
        no_answer_strict=None if answerable else no_answer_strict(query, ranked),
        no_answer_lenient=(
            None
            if answerable
            else no_answer_lenient(query, ranked, config.no_answer_lenient_min_grade)
        ),
    )


def _proportion(count: int, n: int) -> dict[str, Any]:
    return {
        "count": count,
        "n": n,
        "value": round(count / n, DIGITS) if n else None,
        "wilson_95": list(interval) if (interval := wilson_interval(count, n)) else None,
    }


def _mean_exact(values: Sequence[Fraction]) -> dict[str, Any]:
    return {
        "n": len(values),
        "value": round(float(sum(values) / len(values)), DIGITS) if values else None,
        "bootstrap_95": bootstrap_interval(list(values)),
    }


def _mean_float(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "value": round(math.fsum(values) / len(values), DIGITS) if values else None,
        "bootstrap_95": bootstrap_interval(list(values)),
    }


def aggregate(outcomes: Sequence[QueryOutcome]) -> dict[str, Any]:
    """The metrics over *outcomes*, each with its own denominator (see the module docstring)."""
    answerable = [item for item in outcomes if item.answerable]
    no_answer = [item for item in outcomes if not item.answerable]
    return {
        "queries": len(outcomes),
        "answerable": len(answerable),
        "no_answer": len(no_answer),
        "success_at_5": _proportion(
            sum(bool(item.success) for item in answerable), len(answerable)
        ),
        "recall_at_5": _mean_exact([item.recall for item in answerable if item.recall is not None]),
        "mrr_at_5": _mean_exact(
            [item.reciprocal_rank for item in answerable if item.reciprocal_rank is not None]
        ),
        "ndcg_at_5": _mean_float([item.ndcg for item in answerable if item.ndcg is not None]),
        "empty_result_rate": _proportion(sum(item.empty for item in answerable), len(answerable)),
        "no_answer_strict": _proportion(
            sum(bool(item.no_answer_strict) for item in no_answer), len(no_answer)
        ),
        "no_answer_lenient": _proportion(
            sum(bool(item.no_answer_lenient) for item in no_answer), len(no_answer)
        ),
        "scope_leakage": _proportion(sum(bool(item.leaked) for item in outcomes), len(outcomes)),
    }


def _row(item: QueryOutcome) -> dict[str, Any]:
    return {
        "query_id": item.query_id,
        "family": item.family,
        "origin": item.origin,
        "answerable": item.answerable,
        "returned": len(item.returned),
        "empty": item.empty,
        "top": [
            {"rank": rank, "doc_id": chunk.doc_id, "ordinal": chunk.ordinal, "grade": grade}
            for rank, chunk, grade in item.top
        ],
        "first_sufficient_rank": item.first_sufficient_rank,
        "leaked": [chunk.as_dict() for chunk in item.leaked],
        "trap_hits": [chunk.as_dict() for chunk in item.trap_hits],
        "success": item.success,
        "recall": None if item.recall is None else round(float(item.recall), DIGITS),
        "reciprocal_rank": (
            None if item.reciprocal_rank is None else round(float(item.reciprocal_rank), DIGITS)
        ),
        "ndcg": None if item.ndcg is None else round(item.ndcg, DIGITS),
        "no_answer_strict": item.no_answer_strict,
        "no_answer_lenient": item.no_answer_lenient,
    }


def evaluate(
    dataset: RetrievalDataset, results: Mapping[str, Sequence[Any]], config: MetricConfig
) -> dict[str, Any]:
    """The full report: configuration, overall and per-family metrics, and one row per query."""
    if not isinstance(config, MetricConfig):
        raise ConfigurationError("a MetricConfig is required; the open choices have no defaults")
    ranked = located(dataset, results)
    by_id = {query.query_id: query for query in dataset.queries}
    outcomes = [
        outcome(dataset, by_id[query_id], ranked[query_id], config) for query_id in sorted(by_id)
    ]
    families = sorted({item.family for item in outcomes})
    return {
        "metrics_version": METRICS_VERSION,
        "schema_version": SCHEMA_VERSION,
        "dataset": {"identity": dataset.identity, "sha256": dataset.checksum()},
        "cutoff": dataset.cutoff,
        "configuration": config.as_dict(),
        "overall": aggregate(outcomes),
        "families": {
            family: aggregate([item for item in outcomes if item.family == family])
            for family in families
        },
        "queries": [_row(item) for item in outcomes],
    }


__all__ = [
    "BOOTSTRAP_METHOD",
    "BOOTSTRAP_MIN_N",
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "DIGITS",
    "DISCOUNT",
    "LENIENT_MIN_GRADE",
    "METRICS_VERSION",
    "V2_METRIC_CONFIG",
    "WILSON_Z_95",
    "ConfigurationError",
    "MetricConfig",
    "QueryOutcome",
    "ResultsError",
    "aggregate",
    "bootstrap_interval",
    "evaluate",
    "first_rank_at",
    "leaked",
    "located",
    "ndcg_at",
    "no_answer_lenient",
    "no_answer_strict",
    "outcome",
    "percentile_interval",
    "recall_at",
    "reciprocal_rank_at",
    "success_at",
    "trap_hits",
    "wilson_interval",
]
