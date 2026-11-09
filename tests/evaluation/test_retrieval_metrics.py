"""R2.10C.2 and .1 -- retrieval metrics over supplied rankings. Offline: nothing is searched.

Each expected value is written out from the definitions in `retrieval_metrics`, not computed by it.
The dataset is `test_retrieval_schema`'s invented sample, not Dataset v2. The configurations here
exist to exercise each choice; none of them is a decision of the open items.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

import pytest

from tests.evaluation import retrieval_metrics as metrics
from tests.evaluation.retrieval import canonical
from tests.evaluation.retrieval_metrics import ConfigurationError, MetricConfig, ResultsError
from tests.evaluation.retrieval_schema import ChunkRef, DatasetQuery, load_dataset
from tests.evaluation.test_retrieval_schema import sample, sample_mapping

POOL, TOWELS = ChunkRef("pool", 0), ChunkRef("pool", 1)
PARKING, PETS, FAQ = ChunkRef("parking", 0), ChunkRef("pets", 0), ChunkRef("faq", 0)
PISINA = ChunkRef("pisina", 0)
OLD, SPA, ROOF = ChunkRef("pool-old", 0), ChunkRef("spa", 0), ChunkRef("rooftop", 0)

#: The test configuration: MRR counts grade 2 only, gains (0, 1, 3), lenient counts grade 1 up.
CONFIG = MetricConfig(mrr_min_grade=2, ndcg_gains=(0, 1, 3), no_answer_lenient_min_grade=1)

L3 = math.log2(3)  # the discount at rank 2

DATASET = sample()


def query(query_id: str) -> DatasetQuery:
    return next(q for q in sample().queries if q.query_id == query_id)


HOURS, DOG, BOTH = query("q-hours"), query("q-dog"), query("q-both")
CASINO, ROOFTOP = query("q-casino"), query("q-rooftop")

#: q-hours is judged pool#0 = 2, pisina#0 = 1, pool#1 = 0, so its ideal DCG with (0, 1, 3) is:
HOURS_IDEAL = 3 / 1 + 1 / L3


def pairs(*chunks: ChunkRef) -> list[tuple[str, int]]:
    return [(c.doc_id, c.ordinal) for c in chunks]


FULL: dict[str, list[tuple[str, int]]] = {
    "q-hours": pairs(PISINA, POOL),
    "q-dog": [],
    "q-both": pairs(PARKING, TOWELS, ROOF),
    "q-casino": pairs(FAQ),
    "q-rooftop": [],
}


# --- Success@5, Recall@5, MRR@5 and nDCG@5, per query --------------------------------------------


def test_perfect_retrieval_scores_one_everywhere() -> None:
    ranked = (POOL, PISINA, TOWELS)
    assert metrics.success_at(HOURS, ranked, 5)
    assert metrics.recall_at(HOURS, ranked, 5) == 1
    assert metrics.reciprocal_rank_at(HOURS, ranked, 5, 2) == 1
    assert metrics.ndcg_at(HOURS, ranked, 5, CONFIG.ndcg_gains) == pytest.approx(1.0)


def test_partial_retrieval_of_two_sufficient_chunks() -> None:
    ranked = (PARKING, TOWELS, ROOF)
    assert metrics.success_at(BOTH, ranked, 5)
    assert metrics.recall_at(BOTH, ranked, 5) == Fraction(1, 2)
    assert metrics.reciprocal_rank_at(BOTH, ranked, 5, 2) == 1
    # Actual gains 3, 0, 0; ideal 3, 3 (both chunks are grade 2).
    expected = 3 / (3 + 3 / L3)
    assert metrics.ndcg_at(BOTH, ranked, 5, CONFIG.ndcg_gains) == pytest.approx(expected)


def test_no_relevant_result_scores_zero_without_being_empty() -> None:
    ranked = (TOWELS, PARKING, FAQ)
    assert not metrics.success_at(HOURS, ranked, 5)
    assert metrics.recall_at(HOURS, ranked, 5) == 0
    assert metrics.reciprocal_rank_at(HOURS, ranked, 5, 1) == 0
    assert metrics.ndcg_at(HOURS, ranked, 5, CONFIG.ndcg_gains) == 0
    assert not metrics.outcome(DATASET, HOURS, ranked, CONFIG).empty


def test_an_empty_result_scores_zero_and_is_empty() -> None:
    assert not metrics.success_at(HOURS, (), 5)
    assert metrics.recall_at(HOURS, (), 5) == 0
    assert metrics.reciprocal_rank_at(HOURS, (), 5, 2) == 0
    assert metrics.ndcg_at(HOURS, (), 5, CONFIG.ndcg_gains) == 0
    assert metrics.outcome(DATASET, HOURS, (), CONFIG).empty


def test_fewer_than_five_results_are_scored_as_they_are() -> None:
    ranked = (POOL,)
    assert metrics.success_at(HOURS, ranked, 5)
    assert metrics.recall_at(HOURS, ranked, 5) == 1
    assert metrics.ndcg_at(HOURS, ranked, 5, CONFIG.ndcg_gains) == pytest.approx(3 / HOURS_IDEAL)


FILLER = (TOWELS, PARKING, PETS, FAQ, ROOF)


@pytest.mark.parametrize("rank", [1, 2, 3, 4, 5])
def test_a_sufficient_chunk_at_each_rank_inside_the_cutoff(rank: int) -> None:
    ranked = (*FILLER[: rank - 1], POOL, *FILLER[rank - 1 :])
    assert ranked.index(POOL) + 1 == rank
    assert metrics.success_at(HOURS, ranked, 5)
    assert metrics.first_rank_at(HOURS, ranked, 5, 2) == rank
    assert metrics.reciprocal_rank_at(HOURS, ranked, 5, 2) == Fraction(1, rank)
    expected = (3 / math.log2(rank + 1)) / HOURS_IDEAL
    assert metrics.ndcg_at(HOURS, ranked, 5, CONFIG.ndcg_gains) == pytest.approx(expected)


def test_a_sufficient_chunk_at_rank_six_is_a_miss() -> None:
    ranked = (*FILLER, POOL)
    assert not metrics.success_at(HOURS, ranked, 5)
    assert metrics.recall_at(HOURS, ranked, 5) == 0
    assert metrics.first_rank_at(HOURS, ranked, 5, 2) is None
    assert metrics.reciprocal_rank_at(HOURS, ranked, 5, 2) == 0
    assert metrics.ndcg_at(HOURS, ranked, 5, CONFIG.ndcg_gains) == 0


@pytest.mark.parametrize(
    ("ranked", "success", "mrr_2", "mrr_1", "gain"),
    [
        ((TOWELS,), False, 0, 0, 0),  # grade 0, judged
        ((PARKING,), False, 0, 0, 0),  # grade 0, unjudged
        ((PISINA,), False, 0, 1, 1),  # grade 1
        ((POOL,), True, 1, 1, 3),  # grade 2
    ],
    ids=["grade-0-judged", "grade-0-unjudged", "grade-1", "grade-2"],
)
def test_each_grade_counts_as_defined(
    ranked: tuple[ChunkRef, ...], success: bool, mrr_2: int, mrr_1: int, gain: int
) -> None:
    assert metrics.success_at(HOURS, ranked, 5) is success
    assert metrics.reciprocal_rank_at(HOURS, ranked, 5, 2) == mrr_2
    assert metrics.reciprocal_rank_at(HOURS, ranked, 5, 1) == mrr_1
    assert metrics.ndcg_at(HOURS, ranked, 5, CONFIG.ndcg_gains) == pytest.approx(gain / HOURS_IDEAL)


def test_grade_1_and_grade_2_are_not_the_same_to_success_or_recall() -> None:
    assert not metrics.success_at(HOURS, (PISINA, TOWELS), 5)
    assert metrics.recall_at(HOURS, (PISINA, TOWELS), 5) == 0


# --- the configuration decides, and nothing else ------------------------------------------------


def test_the_mrr_threshold_is_the_configured_one() -> None:
    ranked = (PISINA, POOL)
    assert metrics.reciprocal_rank_at(HOURS, ranked, 5, 2) == Fraction(1, 2)
    assert metrics.reciprocal_rank_at(HOURS, ranked, 5, 1) == 1
    one = metrics.outcome(DATASET, HOURS, ranked, CONFIG)
    other = metrics.outcome(
        DATASET,
        HOURS,
        ranked,
        MetricConfig(mrr_min_grade=1, ndcg_gains=(0, 1, 3), no_answer_lenient_min_grade=1),
    )
    assert (one.reciprocal_rank, other.reciprocal_rank) == (Fraction(1, 2), 1)


def test_the_gains_are_the_configured_ones() -> None:
    ranked = (PISINA, POOL)
    exponential = metrics.ndcg_at(HOURS, ranked, 5, (0, 1, 3))
    linear = metrics.ndcg_at(HOURS, ranked, 5, (0, 1, 2))
    assert exponential == pytest.approx((1 + 3 / L3) / (3 + 1 / L3))
    assert linear == pytest.approx((1 + 2 / L3) / (2 + 1 / L3))
    assert exponential != pytest.approx(linear)


def test_the_ideal_ordering_puts_the_highest_grade_first() -> None:
    # The best possible ranking scores exactly 1; the reverse of it scores less.
    assert metrics.ndcg_at(HOURS, (POOL, PISINA), 5, (0, 1, 3)) == pytest.approx(1.0)
    assert metrics.ndcg_at(HOURS, (PISINA, POOL), 5, (0, 1, 3)) < 1


def test_no_configuration_means_no_report() -> None:
    with pytest.raises(TypeError):
        MetricConfig()  # type: ignore[call-arg]
    with pytest.raises(ConfigurationError, match="no defaults"):
        metrics.evaluate(sample(), FULL, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fields",
    [
        {"mrr_min_grade": 0},
        {"mrr_min_grade": 3},
        {"mrr_min_grade": True},
        {"mrr_min_grade": "2"},
        {"no_answer_lenient_min_grade": 0},
        {"no_answer_lenient_min_grade": 2},  # a no-answer query has no grade 2
        {"no_answer_lenient_min_grade": True},
        {"ndcg_gains": (1, 1, 3)},  # grade 0 must gain nothing
        {"ndcg_gains": (0, 3, 1)},  # grade 1 above grade 2
        {"ndcg_gains": (0, 0, 0)},  # nothing gains
        {"ndcg_gains": (0, -1, 3)},
        {"ndcg_gains": (0, float("nan"), 3)},
        {"ndcg_gains": (0, 1)},
        {"ndcg_gains": [0, 1, 3]},
        {"ndcg_gains": (0, True, 3)},
    ],
    ids=[
        "mrr-0",
        "mrr-3",
        "mrr-boolean",
        "mrr-string",
        "lenient-0",
        "lenient-2",
        "lenient-boolean",
        "grade-0-gains",
        "grade-1-above-grade-2",
        "nothing-gains",
        "negative-gain",
        "nan-gain",
        "two-gains",
        "gains-as-list",
        "boolean-gain",
    ],
)
def test_a_malformed_configuration_is_refused(fields: dict[str, Any]) -> None:
    given: dict[str, Any] = {
        "mrr_min_grade": 2,
        "ndcg_gains": (0, 1, 3),
        "no_answer_lenient_min_grade": 1,
        **fields,
    }
    with pytest.raises(ConfigurationError):
        MetricConfig(**given)


def test_a_threshold_passed_directly_is_checked_too() -> None:
    with pytest.raises(ConfigurationError):
        metrics.reciprocal_rank_at(HOURS, (POOL,), 5, 0)
    with pytest.raises(ConfigurationError):
        metrics.no_answer_lenient(CASINO, (), 3)
    with pytest.raises(ConfigurationError, match="no grade-2 chunk"):
        metrics.no_answer_lenient(CASINO, (), 2)


def test_the_report_records_its_configuration() -> None:
    report = metrics.evaluate(sample(), FULL, CONFIG)
    assert report["configuration"] == {
        "mrr_min_grade": 2,
        "ndcg_gains": {"0": 0, "1": 1, "2": 3},
        "no_answer_lenient_min_grade": 1,
        "fixed": {
            "success_min_grade": 2,
            "recall_min_grade": 2,
            "discount": "log2(rank + 1)",
            "rate_interval": "wilson",
            "z": 1.959963984540054,
            "mean_interval": {
                "method": "percentile bootstrap, queries resampled with replacement",
                "resamples": 10_000,
                "seed": 20261108,
                "min_n": 10,
                "percentiles": "2.5 and 97.5, nearest rank",
            },
            "leakage": "any returned chunk of a document not active at the evaluated hotel",
        },
    }
    assert report["metrics_version"] == "knowledge_retrieval_metrics_v1"
    assert report["dataset"] == {
        "identity": "knowledge_retrieval_sample_v1",
        "sha256": sample().checksum(),
    }


# --- scope leakage, from the corpus; forbidden as an annotation (R2.10C.2.1) ---------------------


def leakage_of(results: dict[str, list[tuple[str, int]]]) -> dict[str, Any]:
    return dict(
        metrics.evaluate(DATASET, dict(FULL, **results), CONFIG)["overall"]["scope_leakage"]
    )


def row_of(query_id: str, results: dict[str, list[tuple[str, int]]]) -> dict[str, Any]:
    rows = metrics.evaluate(DATASET, dict(FULL, **results), CONFIG)["queries"]
    return dict(next(row for row in rows if row["query_id"] == query_id))


def test_a_neighbour_chunk_leaks_even_when_no_forbidden_list_names_it() -> None:
    assert ROOF not in DOG.forbidden
    assert metrics.leaked(DATASET, (PETS, ROOF)) == (ROOF,)
    row = row_of("q-dog", {"q-dog": pairs(PETS, ROOF)})
    assert row["leaked"] == [{"doc_id": "rooftop", "ordinal": 0}]
    assert row["trap_hits"] == []
    assert leakage_of({"q-dog": pairs(PETS, ROOF)})["count"] == 2  # q-dog and q-both


def test_a_superseded_version_leaks() -> None:
    assert metrics.leaked(DATASET, (POOL, OLD)) == (OLD,)
    assert row_of("q-dog", {"q-dog": pairs(OLD)})["leaked"] == [
        {"doc_id": "pool-old", "ordinal": 0}
    ]


def test_a_withdrawn_document_leaks() -> None:
    assert metrics.leaked(DATASET, (SPA,)) == (SPA,)
    assert row_of("q-hours", {"q-hours": pairs(POOL, SPA)})["leaked"] == [
        {"doc_id": "spa", "ordinal": 0}
    ]


def test_leakage_is_found_past_the_cutoff() -> None:
    ranked = (TOWELS, PARKING, PETS, FAQ, PISINA, SPA)
    assert metrics.leaked(DATASET, ranked) == (SPA,)
    assert metrics.outcome(DATASET, HOURS, ranked, CONFIG).leaked == (SPA,)


def test_the_evaluated_hotels_active_documents_never_leak() -> None:
    in_scope = (POOL, TOWELS, PARKING, PETS, FAQ, PISINA)
    assert metrics.leaked(DATASET, in_scope) == ()
    assert leakage_of({"q-both": pairs(PARKING, POOL), "q-hours": pairs(*in_scope)})["count"] == 0


def test_a_query_with_several_leaking_chunks_counts_once() -> None:
    assert row_of("q-dog", {"q-dog": pairs(ROOF, OLD, SPA)})["leaked"] == [
        {"doc_id": "rooftop", "ordinal": 0},
        {"doc_id": "pool-old", "ordinal": 0},
        {"doc_id": "spa", "ordinal": 0},
    ]
    leakage = leakage_of({"q-dog": pairs(ROOF, OLD, SPA)})
    assert (leakage["count"], leakage["n"]) == (2, 5)  # q-dog once, and q-both


def test_no_answer_queries_are_in_the_leakage_denominator() -> None:
    leakage = leakage_of(
        {"q-both": pairs(PARKING), "q-casino": pairs(SPA), "q-rooftop": pairs(ROOF)}
    )
    assert (leakage["count"], leakage["n"]) == (2, 5)
    assert metrics.outcome(DATASET, ROOFTOP, (ROOF,), CONFIG).leaked == (ROOF,)


def test_unknown_chunks_are_still_refused() -> None:
    for unknown in (("rooftop", 1), ("bar", 0)):
        with pytest.raises(ResultsError, match="is not a chunk of the corpus"):
            metrics.evaluate(DATASET, dict(FULL, **{"q-dog": [unknown]}), CONFIG)
    with pytest.raises(ResultsError, match="not a document of the corpus"):
        metrics.leaked(DATASET, (ChunkRef("bar", 0),))


def test_forbidden_is_an_annotation_reported_as_trap_hits() -> None:
    both = row_of("q-both", {})
    assert both["trap_hits"] == both["leaked"] == [{"doc_id": "rooftop", "ordinal": 0}]
    assert metrics.trap_hits(BOTH, (PARKING, ROOF, OLD)) == (ROOF,)
    assert metrics.leaked(DATASET, (PARKING, ROOF, OLD)) == (ROOF, OLD)


def test_emptying_every_forbidden_list_cannot_pass_the_leakage_gate() -> None:
    mapping = sample_mapping()
    for entry in mapping["queries"]:
        entry["forbidden"] = []
    unannotated = load_dataset(mapping)
    leakage = metrics.evaluate(unannotated, FULL, CONFIG)["overall"]["scope_leakage"]
    assert (leakage["count"], leakage["n"]) == (1, 5)  # q-both's rooftop chunk still leaks


# --- no-answer queries ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ranked", "strict", "lenient"),
    [
        ((), True, True),
        ((PARKING,), False, True),  # grade 0 for q-casino
        ((FAQ,), False, False),  # grade 1 for q-casino
        ((TOWELS, PARKING, PETS, POOL, PISINA, FAQ), False, False),  # grade 1 at rank 6
    ],
    ids=["empty", "grade-0", "grade-1", "grade-1-past-the-cutoff"],
)
def test_strict_and_lenient_no_answer_checks_are_different_checks(
    ranked: tuple[ChunkRef, ...], strict: bool, lenient: bool
) -> None:
    assert metrics.no_answer_strict(CASINO, ranked) is strict
    assert metrics.no_answer_lenient(CASINO, ranked, 1) is lenient


def test_answerable_and_no_answer_functions_refuse_each_others_queries() -> None:
    for function in (metrics.success_at, metrics.recall_at):
        with pytest.raises(ValueError, match="no-answer query"):
            function(CASINO, (), 5)
    with pytest.raises(ValueError, match="no-answer query"):
        metrics.reciprocal_rank_at(CASINO, (), 5, 2)
    with pytest.raises(ValueError, match="no-answer query"):
        metrics.ndcg_at(CASINO, (), 5, (0, 1, 3))
    with pytest.raises(ValueError, match="is answerable"):
        metrics.no_answer_strict(HOURS, ())
    with pytest.raises(ValueError, match="is answerable"):
        metrics.no_answer_lenient(HOURS, (), 1)


# --- the report: denominators, families, rows ----------------------------------------------------


def test_each_metric_has_its_own_denominator() -> None:
    overall = metrics.evaluate(sample(), FULL, CONFIG)["overall"]
    assert (overall["queries"], overall["answerable"], overall["no_answer"]) == (5, 3, 2)
    # Answerable: q-hours hit at rank 2, q-dog empty, q-both hit at rank 1 with half its chunks.
    assert overall["success_at_5"]["count"] == 2
    assert overall["success_at_5"]["n"] == 3
    assert overall["success_at_5"]["value"] == 0.6667
    recall, mrr, ndcg = overall["recall_at_5"], overall["mrr_at_5"], overall["ndcg_at_5"]
    assert (recall["n"], recall["value"]) == (3, 0.5)  # (1 + 0 + 1/2) / 3
    assert (mrr["n"], mrr["value"]) == (3, 0.5)  # (1/2 + 0 + 1) / 3
    expected = ((1 + 3 / L3) / HOURS_IDEAL + 0 + 3 / (3 + 3 / L3)) / 3
    assert (ndcg["n"], ndcg["value"]) == (3, round(expected, 4))
    assert (overall["empty_result_rate"]["count"], overall["empty_result_rate"]["n"]) == (1, 3)
    # No-answer: q-casino returned its grade-1 FAQ; q-rooftop returned nothing.
    assert (overall["no_answer_strict"]["count"], overall["no_answer_strict"]["n"]) == (1, 2)
    assert (overall["no_answer_lenient"]["count"], overall["no_answer_lenient"]["n"]) == (1, 2)
    # Leakage: q-both returned the neighbour's rooftop chunk.
    assert (overall["scope_leakage"]["count"], overall["scope_leakage"]["n"]) == (1, 5)


def test_rates_carry_a_wilson_interval_and_means_a_bootstrap_block() -> None:
    overall = metrics.evaluate(sample(), FULL, CONFIG)["overall"]
    assert overall["success_at_5"]["wilson_95"] == list(metrics.wilson_interval(2, 3) or ())
    assert overall["scope_leakage"]["wilson_95"] == list(metrics.wilson_interval(1, 5) or ())
    for rate in ("empty_result_rate", "no_answer_strict", "no_answer_lenient"):
        assert "wilson_95" in overall[rate] and "bootstrap_95" not in overall[rate]
    for mean in ("recall_at_5", "mrr_at_5", "ndcg_at_5"):
        assert "wilson_95" not in overall[mean]
        block = overall[mean]["bootstrap_95"]
        assert (block["n"], block["interval"], block["reason"]) == (3, None, "n < 10: no interval")


def test_metrics_are_reported_per_family() -> None:
    families = metrics.evaluate(sample(), FULL, CONFIG)["families"]
    assert list(families) == ["multi_idea", "no_answer", "paraphrase", "scope_trap"]
    multi = families["multi_idea"]  # q-dog (empty) and q-both (hit)
    assert (multi["queries"], multi["answerable"], multi["no_answer"]) == (2, 2, 0)
    assert (multi["success_at_5"]["count"], multi["success_at_5"]["n"]) == (1, 2)
    assert multi["no_answer_strict"] == {"count": 0, "n": 0, "value": None, "wilson_95": None}
    assert families["paraphrase"]["success_at_5"]["count"] == 1
    assert families["no_answer"]["answerable"] == 0
    assert families["no_answer"]["success_at_5"]["value"] is None
    assert families["scope_trap"]["no_answer_strict"]["count"] == 1


def test_each_query_has_a_row_with_its_ranks_and_grades() -> None:
    rows = {row["query_id"]: row for row in metrics.evaluate(sample(), FULL, CONFIG)["queries"]}
    assert list(rows) == sorted(rows)
    hours = rows["q-hours"]
    assert hours["top"] == [
        {"rank": 1, "doc_id": "pisina", "ordinal": 0, "grade": 1},
        {"rank": 2, "doc_id": "pool", "ordinal": 0, "grade": 2},
    ]
    assert (hours["first_sufficient_rank"], hours["reciprocal_rank"]) == (2, 0.5)
    assert rows["q-both"]["leaked"] == [{"doc_id": "rooftop", "ordinal": 0}]
    assert rows["q-both"]["trap_hits"] == [{"doc_id": "rooftop", "ordinal": 0}]
    assert rows["q-dog"]["empty"] is True
    assert rows["q-casino"]["success"] is None
    assert (rows["q-casino"]["no_answer_strict"], rows["q-casino"]["no_answer_lenient"]) == (
        False,
        False,
    )


def test_only_the_top_five_are_scored_but_everything_returned_is_checked() -> None:
    results = dict(FULL, **{"q-hours": pairs(TOWELS, PARKING, PETS, FAQ, PISINA, POOL, OLD)})
    row = next(
        r
        for r in metrics.evaluate(sample(), results, CONFIG)["queries"]
        if r["query_id"] == "q-hours"
    )
    assert (row["returned"], len(row["top"])) == (7, 5)
    assert row["success"] is False
    assert row["leaked"] == [{"doc_id": "pool-old", "ordinal": 0}]


# --- results are validated, never repaired -------------------------------------------------------


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ({k: v for k, v in FULL.items() if k != "q-dog"}, "no results for"),
        (dict(FULL, **{"q-gym": []}), "does not have"),
        (dict(FULL, **{"q-hours": "pool"}), "must be a ranked list"),
        (dict(FULL, **{"q-hours": [("pool",)]}), "not a (doc_id, ordinal) pair"),
        (dict(FULL, **{"q-hours": [("pool", True)]}), "not a (doc_id, ordinal) pair"),
        (dict(FULL, **{"q-hours": [("pool", "0")]}), "not a (doc_id, ordinal) pair"),
        (dict(FULL, **{"q-hours": [("Pool", 0)]}), "not a valid identifier"),
        (dict(FULL, **{"q-hours": [("pool", -1)]}), "must not be negative"),
        (dict(FULL, **{"q-hours": [("pool", 9)]}), "is not a chunk of the corpus"),
        (dict(FULL, **{"q-hours": [("gym", 0)]}), "is not a chunk of the corpus"),
        (dict(FULL, **{"q-hours": [("pool", 0), ("pool", 0)]}), "ranked twice"),
        (dict(FULL, **{"q-hours": pairs(*FILLER, POOL, POOL)}), "ranked twice"),
    ],
    ids=[
        "missing-query",
        "unknown-query",
        "not-a-list",
        "not-a-pair",
        "boolean-ordinal",
        "string-ordinal",
        "malformed-doc-id",
        "negative-ordinal",
        "unknown-ordinal",
        "unknown-document",
        "duplicate",
        "duplicate-past-the-cutoff",
    ],
)
def test_invalid_results_are_refused(results: dict[str, Any], expected: str) -> None:
    with pytest.raises(ResultsError) as refused:
        metrics.evaluate(sample(), results, CONFIG)
    assert expected in str(refused.value)


def test_results_given_as_json_lists_are_accepted() -> None:
    as_lists = {key: [list(item) for item in value] for key, value in FULL.items()}
    assert metrics.evaluate(sample(), as_lists, CONFIG) == metrics.evaluate(sample(), FULL, CONFIG)


# --- determinism ----------------------------------------------------------------------------------


def test_the_report_is_the_same_on_every_run_and_in_any_input_order() -> None:
    first = canonical(metrics.evaluate(sample(), FULL, CONFIG))
    reversed_input = dict(reversed(list(FULL.items())))
    again = {canonical(metrics.evaluate(sample(), reversed_input, CONFIG)) for _ in range(3)}
    assert again == {first}


# --- Wilson intervals -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("successes", "n", "expected"),
    [
        (0, 10, (0.0, 0.2775)),
        (10, 10, (0.7225, 1.0)),
        (5, 10, (0.2366, 0.7634)),
        (16, 23, (0.4913, 0.844)),  # knowledge_retrieval_v1's measured recall@5
        (0, 1, (0.0, 0.7935)),
        (1, 1, (0.2065, 1.0)),
    ],
)
def test_wilson_intervals_match_their_known_values(
    successes: int, n: int, expected: tuple[float, float]
) -> None:
    assert metrics.wilson_interval(successes, n) == expected


def test_a_wilson_interval_of_nothing_is_none() -> None:
    assert metrics.wilson_interval(0, 0) is None


@pytest.mark.parametrize(("successes", "n"), [(-1, 5), (6, 5), (True, 5), (1, 5.0), (1.0, 5)])
def test_a_wilson_interval_refuses_impossible_counts(successes: Any, n: Any) -> None:
    with pytest.raises(ValueError):
        metrics.wilson_interval(successes, n)


def test_a_wilson_interval_stays_inside_zero_and_one() -> None:
    for n in range(1, 40):
        for successes in range(n + 1):
            low, high = metrics.wilson_interval(successes, n) or (None, None)
            assert low is not None and high is not None
            assert 0.0 <= low <= successes / n <= high <= 1.0


# --- query-level bootstrap intervals for the means (R2.10C.2.1) -----------------------------------

TENTHS = [k / 10 for k in range(10)]  # ten query-level values: 0.0, 0.1, ..., 0.9

#: Pinned: the interval for TENTHS under the documented seed and 10,000 resamples. It changes only
#: if the method, the seed, the count or Python's `random.Random` does, and then deliberately.
TENTHS_INTERVAL = [0.27, 0.63]

#: Twelve irregular values. TENTHS' resample means move in steps of 0.01, so every seed rounds to
#: the same interval there; these show what the seed and the resample count change.
UNEVEN = [0.0, 0.0371, 0.1234, 0.2468, 0.3333, 0.5101, 0.6789, 0.7777, 0.9012, 1.0, 0.0625, 0.4444]


def test_the_same_seed_gives_the_same_interval() -> None:
    first = metrics.bootstrap_interval(TENTHS)
    assert metrics.bootstrap_interval(list(TENTHS)) == first
    assert first["method"] == "percentile bootstrap, queries resampled with replacement"
    assert (first["resamples"], first["seed"], first["n"]) == (10_000, 20261108, 10)
    assert first["interval"] == TENTHS_INTERVAL
    assert first["zero_width"] is False
    # TENTHS cannot tell seeds apart; UNEVEN can, so determinism is checked where it shows.
    uneven = metrics.bootstrap_interval(UNEVEN)
    assert metrics.bootstrap_interval(list(UNEVEN)) == uneven
    assert uneven["interval"] == [0.24, 0.6207]


def test_another_seed_is_supported_and_recorded() -> None:
    default = metrics.bootstrap_interval(UNEVEN)
    assert default["interval"] == [0.24, 0.6207]  # pinned, as TENTHS_INTERVAL is
    other = metrics.bootstrap_interval(UNEVEN, seed=7)
    assert (other["seed"], other["interval"]) == (7, [0.2405, 0.6216])
    assert metrics.bootstrap_interval(UNEVEN, seed=7) == other
    fewer = metrics.bootstrap_interval(UNEVEN, resamples=2_000)
    assert (fewer["resamples"], fewer["interval"]) == (2_000, [0.239, 0.6203])


def test_the_interval_lies_around_the_mean_and_inside_the_values() -> None:
    low, high = metrics.bootstrap_interval(TENTHS)["interval"]
    assert 0.0 <= low < sum(TENTHS) / len(TENTHS) < high <= 0.9


def test_the_percentiles_are_the_nearest_ranks() -> None:
    means = [i / 10_000 for i in range(10_000)]  # rank r (from 1) holds (r - 1) / 10,000
    assert metrics.percentile_interval(means) == (0.0249, 0.9749)  # ranks 250 and 9,750
    assert metrics.percentile_interval(list(reversed(means))) == (0.0249, 0.9749)
    assert metrics.percentile_interval(list(range(1, 41))) == (1, 39)  # ranks 1 and 39 of 40
    assert metrics.percentile_interval(list(range(1, 42))) == (2, 40)  # ranks 2 and 40 of 41
    assert metrics.percentile_interval([0.3]) == (0.3, 0.3)
    with pytest.raises(ValueError):
        metrics.percentile_interval([])


def test_below_ten_queries_there_is_no_interval() -> None:
    for values in (TENTHS[:9], [], [1.0]):
        block = metrics.bootstrap_interval(values)
        assert (block["interval"], block["zero_width"]) == (None, None)
        assert block["reason"] == "n < 10: no interval"
        assert block["n"] == len(values)
    assert metrics.bootstrap_interval(TENTHS)["reason"] is None


def test_a_zero_width_interval_is_flagged() -> None:
    block = metrics.bootstrap_interval([0.5] * 12)
    assert (block["interval"], block["zero_width"]) == ([0.5, 0.5], True)
    assert metrics.bootstrap_interval([0.0] * 10)["zero_width"] is True


def test_fractions_are_accepted_as_query_level_values() -> None:
    assert metrics.bootstrap_interval([Fraction(k, 10) for k in range(10)]) == (
        metrics.bootstrap_interval(TENTHS)
    )


@pytest.mark.parametrize(
    "values",
    [[1.5] * 10, [-0.1] * 10, [float("nan")] * 10, [True] * 10, ["0.5"] * 10, "0.5", None],
    ids=["above-one", "below-zero", "nan", "boolean", "string-values", "string", "none"],
)
def test_invalid_query_level_values_are_refused(values: Any) -> None:
    with pytest.raises(ValueError):
        metrics.bootstrap_interval(values)


@pytest.mark.parametrize(
    "settings",
    [{"resamples": 0}, {"resamples": True}, {"seed": 1.5}, {"min_n": 0}],
    ids=["no-resamples", "boolean-resamples", "float-seed", "min-n-0"],
)
def test_invalid_bootstrap_settings_are_refused(settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        metrics.bootstrap_interval(TENTHS, **settings)


def twelve_queries() -> tuple[Any, dict[str, list[tuple[str, int]]]]:
    """Eleven two-chunk queries and one one-chunk query: 12 queries, 23 grade-2 chunks."""
    mapping = sample_mapping()
    two = next(q for q in mapping["queries"] if q["query_id"] == "q-both")
    one = next(q for q in mapping["queries"] if q["query_id"] == "q-hours")
    mapping["queries"] = [dict(two, query_id=f"q-both-{i:02d}") for i in range(11)] + [one]
    results = {
        f"q-both-{i:02d}": pairs(PARKING) if i % 3 else pairs(PARKING, POOL) for i in range(11)
    }
    results["q-hours"] = pairs(POOL)
    return load_dataset(mapping), results


def test_the_resampling_unit_is_the_query_not_the_chunk() -> None:
    dataset, results = twelve_queries()
    report = metrics.evaluate(dataset, results, CONFIG)
    recalls = [row["recall"] for row in report["queries"]]  # one per query, in query_id order
    assert len(recalls) == 12
    block = report["overall"]["recall_at_5"]["bootstrap_95"]
    assert block == metrics.bootstrap_interval(recalls)
    assert block["n"] == 12  # queries; a chunk-level resampling would have n = 23
    assert block["interval"] is not None
    for mean in ("mrr_at_5", "ndcg_at_5"):
        assert report["overall"][mean]["bootstrap_95"]["n"] == 12
