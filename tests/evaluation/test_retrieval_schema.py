"""R2.10C.2 -- the retrieval dataset schema: strict loading, scope, provenance, canonical checksum.

The sample below is a test fixture invented for these tests. It is **not** Dataset v2, and no
judgment in it is a judgment of any real or Dataset v2 query. Offline: no database, no search.
"""

# The Greek paragraph is deliberate: it exercises a second language in the canonical form.
# ruff: noqa: RUF001

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from tests.evaluation import retrieval_schema as schema
from tests.evaluation.retrieval_schema import (
    ChunkRef,
    DatasetError,
    DatasetQuery,
    Judgment,
    load_dataset,
    parse_dataset_json,
)

CAPTURE_SHA = "a" * 64


def sample_mapping() -> dict[str, Any]:
    """A small, valid dataset touching every rule: scope traps, grades 0-2, every origin."""
    return {
        "schema": schema.SCHEMA_VERSION,
        "set_id": "knowledge_retrieval_sample",
        "version": "v1",
        "cutoff": 5,
        "documents": [
            {
                "doc_id": "pool",
                "hotel": "evaluated",
                "status": "active",
                "language": "english",
                "title": "Pool policy",
                "paragraphs": [
                    "The outdoor pool is open from 07:30 to 22:00 daily.",
                    "Towels are provided at the pool bar.",
                ],
            },
            {
                "doc_id": "parking",
                "hotel": "evaluated",
                "status": "active",
                "language": "english",
                "title": "Parking",
                "paragraphs": ["Parking costs 15.00 EUR per night."],
            },
            {
                "doc_id": "pets",
                "hotel": "evaluated",
                "status": "active",
                "language": "english",
                "title": "Pets",
                "paragraphs": ["Dogs up to 10 kg are welcome for a fee of 20.00 EUR per stay."],
            },
            {
                "doc_id": "faq",
                "hotel": "evaluated",
                "status": "active",
                "language": "english",
                "title": "Guest FAQ",
                "paragraphs": ["Ask reception about anything not covered here."],
            },
            {
                "doc_id": "pisina",
                "hotel": "evaluated",
                "status": "active",
                "language": "greek",
                "title": "Πισίνα",
                "paragraphs": ["Η πισίνα είναι ανοιχτή από τις 07:30 έως τις 22:00."],
            },
            {
                "doc_id": "pool-old",
                "hotel": "evaluated",
                "status": "superseded",
                "language": "english",
                "title": "Pool policy",
                "paragraphs": ["The outdoor pool is open from 08:00 to 20:00 daily."],
            },
            {
                "doc_id": "spa",
                "hotel": "evaluated",
                "status": "withdrawn",
                "language": "english",
                "title": "Spa",
                "paragraphs": ["The spa is open from 10:00 to 18:00."],
            },
            {
                "doc_id": "rooftop",
                "hotel": "neighbour",
                "status": "active",
                "language": "english",
                "title": "Rooftop bar",
                "paragraphs": ["The rooftop bar is open from 18:00 to 01:00."],
            },
        ],
        "queries": [
            {
                "query_id": "q-hours",
                "query": "pool opening hours",
                "language": "english",
                "family": "paraphrase",
                "origin": "developer",
                "provenance": None,
                "answerable": True,
                "relevant": [
                    {"doc_id": "pool", "ordinal": 0, "grade": 2},
                    {"doc_id": "pisina", "ordinal": 0, "grade": 1},
                    {"doc_id": "pool", "ordinal": 1, "grade": 0},
                ],
                "forbidden": [{"doc_id": "pool-old", "ordinal": 0}],
                "rationale": "pool#0 states the hours; the Greek page is the same fact in Greek.",
            },
            {
                "query_id": "q-dog",
                "query": "dog pet cost",
                "language": "english",
                "family": "multi_idea",
                "origin": "model_captured",
                "provenance": {
                    "run": "sample-run",
                    "case": "dog_fee",
                    "prompt": "copilot_answer_v2",
                    "capture_sha256": CAPTURE_SHA,
                },
                "answerable": True,
                "relevant": [{"doc_id": "pets", "ordinal": 0, "grade": 2}],
                "forbidden": [],
                "rationale": "The pets chunk states the fee.",
            },
            {
                "query_id": "q-both",
                "query": "parking and pool",
                "language": "english",
                "family": "multi_idea",
                "origin": "developer",
                "provenance": None,
                "answerable": True,
                "relevant": [
                    {"doc_id": "parking", "ordinal": 0, "grade": 2},
                    {"doc_id": "pool", "ordinal": 0, "grade": 2},
                ],
                "forbidden": [{"doc_id": "rooftop", "ordinal": 0}],
                "rationale": "Two facts, each in its own chunk.",
            },
            {
                "query_id": "q-casino",
                "query": "casino",
                "language": "english",
                "family": "no_answer",
                "origin": "developer",
                "provenance": None,
                "answerable": False,
                "relevant": [{"doc_id": "faq", "ordinal": 0, "grade": 1}],
                "forbidden": [{"doc_id": "spa", "ordinal": 0}],
                "rationale": "No casino; the FAQ only points to reception.",
            },
            {
                "query_id": "q-rooftop",
                "query": "rooftop bar open hours",
                "language": "english",
                "family": "scope_trap",
                "origin": "operator",
                "provenance": None,
                "answerable": False,
                "relevant": [],
                "forbidden": [{"doc_id": "rooftop", "ordinal": 0}],
                "rationale": "Only the neighbour hotel has a rooftop bar.",
            },
        ],
    }


def sample() -> schema.RetrievalDataset:
    return load_dataset(sample_mapping())


#: Pinned: the canonical form of `sample_mapping()`. It changes only when the schema or the sample
#: does, and then deliberately.
SAMPLE_SHA256 = "9f1166c7211aba1998d5035bd3c1c75528de6898ccd27213382dd8ec59a7b6f9"


# --- loading and round trips ---------------------------------------------------------------------


def test_a_valid_dataset_loads_with_everything_it_declared() -> None:
    dataset = sample()
    assert dataset.identity == "knowledge_retrieval_sample_v1"
    assert [d.doc_id for d in dataset.documents][:2] == ["pool", "parking"]
    hours = dataset.queries[0]
    assert hours.grade_of(ChunkRef("pool", 0)) == 2
    assert hours.grade_of(ChunkRef("pisina", 0)) == 1
    assert hours.grade_of(ChunkRef("pool", 1)) == 0
    assert hours.grade_of(ChunkRef("parking", 0)) == 0  # unjudged
    assert hours.sufficient == frozenset({ChunkRef("pool", 0)})
    dog = dataset.queries[1]
    assert dog.provenance == schema.Provenance(
        "sample-run", "dog_fee", "copilot_answer_v2", CAPTURE_SHA
    )


def test_the_canonical_form_loads_back_to_the_same_dataset() -> None:
    dataset = sample()
    assert load_dataset(dataset.as_mapping()) == dataset
    assert parse_dataset_json(dataset.canonical_json()) == dataset


def test_every_chunk_of_the_corpus_is_known_in_scope_or_not() -> None:
    chunks = sample().chunks()
    assert ChunkRef("pool", 1) in chunks
    assert ChunkRef("rooftop", 0) in chunks
    assert ChunkRef("pool", 2) not in chunks
    assert len(chunks) == 9


def test_a_no_answer_query_may_hold_a_partial_chunk_but_not_a_sufficient_one() -> None:
    casino = sample().queries[3]
    assert not casino.answerable
    assert casino.sufficient == frozenset()
    assert casino.grade_of(ChunkRef("faq", 0)) == 1


# --- the canonical form and its checksum ---------------------------------------------------------


def test_the_checksum_is_pinned() -> None:
    assert sample().checksum() == SAMPLE_SHA256


def test_the_canonical_json_has_sorted_keys_and_compact_separators() -> None:
    text = sample().canonical_json()
    assert text.startswith('{"cutoff":5,"documents":[{"doc_id":"pool","hotel":"evaluated",')
    assert '": ' not in text
    assert '", "' not in text
    assert "Πισίνα" in text  # UTF-8 text, not \\u escapes


def test_the_checksum_ignores_the_order_of_keys_and_of_judgments() -> None:
    reordered = sample_mapping()
    reordered = {key: reordered[key] for key in reversed(list(reordered))}
    reordered["queries"][0]["relevant"].reverse()
    reordered["queries"][0] = dict(reversed(list(reordered["queries"][0].items())))
    assert load_dataset(reordered).checksum() == sample().checksum()


def test_the_judgment_lists_are_stored_sorted() -> None:
    hours = sample().queries[0]
    assert [item.ref for item in hours.relevant] == sorted(item.ref for item in hours.relevant)


def test_the_checksum_follows_document_order_and_every_word() -> None:
    base = sample().checksum()
    moved = sample_mapping()
    moved["documents"].reverse()  # upload order decides the search's tie-break
    reworded = sample_mapping()
    reworded["queries"][0]["rationale"] += " "
    regraded = sample_mapping()
    regraded["queries"][0]["relevant"][1]["grade"] = 0
    checksums = {load_dataset(m).checksum() for m in (moved, reworded, regraded)}
    assert base not in checksums
    assert len(checksums) == 3


def test_the_checksum_is_the_same_on_every_run() -> None:
    assert len({sample().checksum() for _ in range(5)}) == 1


# --- what is refused -----------------------------------------------------------------------------

Change = Callable[[dict[str, Any]], None]


def _set(path: tuple[Any, ...], value: Any) -> Change:
    def change(block: dict[str, Any]) -> None:
        target: Any = block
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value

    return change


def _delete(path: tuple[Any, ...]) -> Change:
    def change(block: dict[str, Any]) -> None:
        target: Any = block
        for step in path[:-1]:
            target = target[step]
        del target[path[-1]]

    return change


def _append(path: tuple[Any, ...], value: Any) -> Change:
    def change(block: dict[str, Any]) -> None:
        target: Any = block
        for step in path:
            target = target[step]
        target.append(value)

    return change


Q0, Q1, Q3 = ("queries", 0), ("queries", 1), ("queries", 3)
D0 = ("documents", 0)

REFUSED: list[tuple[str, Change, str]] = [
    ("unknown top-level field", _set(("extra",), 1), "unknown field"),
    ("unknown query field", _set((*Q0, "notes"), "x"), "unknown field"),
    ("unknown document field", _set((*D0, "source"), "x"), "unknown field"),
    ("unknown judgment field", _set((*Q0, "relevant", 0, "why"), "x"), "unknown field"),
    ("unknown provenance field", _set((*Q1, "provenance", "model"), "x"), "unknown field"),
    ("missing query field", _delete((*Q0, "rationale")), "missing field"),
    ("missing provenance field", _delete((*Q1, "provenance", "run")), "missing field"),
    ("another schema", _set(("schema",), "knowledge_retrieval_schema_v0"), "schema must be"),
    ("another cutoff", _set(("cutoff",), 10), "cutoff must be 5"),
    ("a boolean cutoff", _set(("cutoff",), True), "must be an integer"),
    ("a malformed set_id", _set(("set_id",), "Knowledge Retrieval"), "not a valid identifier"),
    ("a malformed version", _set(("version",), "2"), "not a valid identifier"),
    ("a malformed doc_id", _set((*D0, "doc_id"), "Pool Policy"), "not a valid identifier"),
    ("a malformed query_id", _set((*Q0, "query_id"), " q-hours"), "not a valid identifier"),
    ("a duplicate doc_id", _set(("documents", 1, "doc_id"), "pool"), "doc_id is used twice"),
    ("a duplicate query_id", _set((*Q1, "query_id"), "q-hours"), "query_id is used twice"),
    ("grade 3", _set((*Q0, "relevant", 1, "grade"), 3), "must be one of [0, 1, 2]"),
    ("grade -1", _set((*Q0, "relevant", 1, "grade"), -1), "must be one of [0, 1, 2]"),
    ("a boolean grade", _set((*Q0, "relevant", 1, "grade"), True), "must be an integer"),
    ("a string grade", _set((*Q0, "relevant", 1, "grade"), "2"), "must be an integer"),
    ("a negative ordinal", _set((*Q0, "relevant", 0, "ordinal"), -1), "must not be negative"),
    ("a float ordinal", _set((*Q0, "relevant", 0, "ordinal"), 0.0), "must be an integer"),
    ("an ordinal past the end", _set((*Q0, "relevant", 0, "ordinal"), 2), "has no chunk 2"),
    ("an unknown document", _set((*Q0, "relevant", 0, "doc_id"), "gym"), "names no document"),
    (
        "a judged chunk out of scope",
        _append((*Q1, "relevant"), {"doc_id": "rooftop", "ordinal": 0, "grade": 1}),
        "is not in scope",
    ),
    (
        "a judged inactive version",
        _append((*Q1, "relevant"), {"doc_id": "pool-old", "ordinal": 0, "grade": 0}),
        "is not in scope",
    ),
    (
        "a forbidden chunk in scope",
        _append((*Q1, "forbidden"), {"doc_id": "pool", "ordinal": 0}),
        "is in scope",
    ),
    (
        "a chunk judged twice",
        _append((*Q0, "relevant"), {"doc_id": "pool", "ordinal": 0, "grade": 1}),
        "judged more than once",
    ),
    (
        "a chunk forbidden twice",
        _append((*Q0, "forbidden"), {"doc_id": "pool-old", "ordinal": 0}),
        "forbidden more than once",
    ),
    (
        "an answerable query with no grade-2 chunk",
        _set((*Q0, "relevant", 0, "grade"), 1),
        "needs a grade-2 chunk",
    ),
    (
        "a no-answer query with a grade-2 chunk",
        _set((*Q3, "relevant", 0, "grade"), 2),
        "cannot have a grade-2 chunk",
    ),
    ("a non-boolean answerable", _set((*Q0, "answerable"), "yes"), "must be true or false"),
    (
        "model_captured with no provenance",
        _set((*Q1, "provenance"), None),
        "provenance is required",
    ),
    (
        "a developer query with provenance",
        _set(
            (*Q0, "provenance"),
            {"run": "r", "case": "c", "prompt": "p", "capture_sha256": CAPTURE_SHA},
        ),
        "provenance is required",
    ),
    (
        "a malformed capture digest",
        _set((*Q1, "provenance", "capture_sha256"), "A" * 64),
        "capture_sha256",
    ),
    ("an unknown language", _set((*Q0, "language"), "klingon"), "language must be one of"),
    ("an unknown family", _set((*Q0, "family"), "other"), "family must be one of"),
    ("an unknown origin", _set((*Q0, "origin"), "llm"), "origin must be one of"),
    ("an unknown hotel", _set((*D0, "hotel"), "elsewhere"), "hotel must be one of"),
    ("an unknown status", _set((*D0, "status"), "draft"), "status must be one of"),
    ("a document without paragraphs", _set((*D0, "paragraphs"), []), "at least one paragraph"),
    ("a blank paragraph", _set((*D0, "paragraphs", 1), "  "), "non-empty string"),
    ("a blank title", _set((*D0, "title"), ""), "non-empty string"),
    ("a blank query", _set((*Q0, "query"), "   "), "non-empty string"),
    ("a blank rationale", _set((*Q0, "rationale"), ""), "non-empty string"),
    ("no documents", _set(("documents",), []), "at least one document"),
    ("no queries", _set(("queries",), []), "at least one query"),
    ("relevant not an array", _set((*Q0, "relevant"), {"doc_id": "pool"}), "must be an array"),
    ("a query that is not an object", _set(("queries", 0), "q-hours"), "must be an object"),
]


@pytest.mark.parametrize(
    ("change", "expected"),
    [r[1:] for r in REFUSED],
    ids=[r[0].replace(" ", "-") for r in REFUSED],
)
def test_a_malformed_or_inconsistent_dataset_is_refused(change: Change, expected: str) -> None:
    block = copy.deepcopy(sample_mapping())
    change(block)
    with pytest.raises(DatasetError, match=None) as refused:
        load_dataset(block)
    assert expected in str(refused.value)


def test_a_no_answer_family_query_is_never_answerable() -> None:
    block = sample_mapping()
    block["queries"][3]["answerable"] = True
    block["queries"][3]["relevant"][0]["grade"] = 2
    with pytest.raises(DatasetError, match="no_answer query cannot be answerable"):
        load_dataset(block)


def test_another_family_may_be_unanswerable() -> None:
    """v1's r22 ("gym opening times") was a paraphrase with no answer: that stays possible."""
    block = sample_mapping()
    block["queries"][4]["family"] = "paraphrase"
    assert not load_dataset(block).queries[4].answerable


def test_construction_is_validated_like_loading() -> None:
    with pytest.raises(DatasetError, match="must be one of"):
        Judgment("pool", 0, 3)
    with pytest.raises(DatasetError, match="needs a grade-2 chunk"):
        DatasetQuery(
            query_id="q",
            query="pool",
            language="english",
            family="shared_words",
            origin="developer",
            provenance=None,
            answerable=True,
            relevant=(Judgment("pool", 0, 1),),
            forbidden=(),
            rationale="partial only",
        )


def test_duplicate_json_keys_are_refused_not_resolved() -> None:
    text = sample().canonical_json().replace('"cutoff":5', '"cutoff":5,"cutoff":5', 1)
    with pytest.raises(DatasetError, match="duplicate JSON key"):
        parse_dataset_json(text)


def test_nan_and_invalid_json_are_refused() -> None:
    with pytest.raises(DatasetError, match="not a JSON number"):
        parse_dataset_json('{"cutoff": NaN}')
    with pytest.raises(DatasetError, match="not valid JSON"):
        parse_dataset_json("{")


def test_the_languages_are_the_upload_routes_own() -> None:
    assert {"english", "greek", "simple"} <= schema.LANGUAGES
    assert json.dumps(sorted(schema.LANGUAGES))  # a closed, serialisable set
