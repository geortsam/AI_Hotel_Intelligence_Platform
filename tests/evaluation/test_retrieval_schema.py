"""R2.10C.2, R2.10C.3a -- the retrieval dataset schema: strict loading, scope, versions, checksum.

The sample below is a test fixture invented for these tests. It is **not** Dataset v2, and no
judgment in it is a judgment of any real or Dataset v2 query. Offline: no database, no search.
"""

# The Greek paragraph is deliberate: it exercises a second language in the canonical form.
# ruff: noqa: RUF001

from __future__ import annotations

import copy
import hashlib
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


def _sample_v1_shape() -> dict[str, Any]:
    """The sample's documents and queries before `sample_mapping` adds the v2 fields."""
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


SAMPLE_MODULE = "tests/evaluation/test_retrieval_schema.py"
SAMPLE_ORDER = ("pool-old", "pool", "parking", "pets", "faq", "pisina", "spa", "rooftop")


def sample_mapping() -> dict[str, Any]:
    """A small, valid v2 dataset touching every rule: scope traps, grades 0-2, every origin, a
    superseded version before its successor, a withdrawn version, and per-query limits."""
    block = _sample_v1_shape()
    documents = {document["doc_id"]: document for document in block["documents"]}
    block["documents"] = [documents[doc_id] for doc_id in SAMPLE_ORDER]
    for index, document in enumerate(block["documents"]):
        document["source"] = {
            "module": SAMPLE_MODULE,
            "symbol": f"SAMPLE_DOCUMENTS[{index}]",
            "text_sha256": schema.content_sha256(document["paragraphs"]),
        }
        document["supersedes"] = "pool-old" if document["doc_id"] == "pool" else None
    for query in block["queries"]:
        query["limit"] = 20 if query["query_id"] == "q-hours" else 5
    dog = next(query for query in block["queries"] if query["query_id"] == "q-dog")
    dog["provenance"] = {
        "capture_file": "tests/evaluation/fixtures/r29y_knowledge_capture.json",
        "capture_sha256": CAPTURE_SHA,
        "case": "dog_fee",
        "limit": 5,
        "occurrences": [
            {"run": "copilot_answer@v2", "attempt": 1},
            {"run": "copilot_answer@v8", "attempt": 2},
        ],
    }
    return block


def sample() -> schema.RetrievalDataset:
    return load_dataset(sample_mapping())


#: Pinned: the canonical form of `sample_mapping()`. It changes only when the schema or the sample
#: does, and then deliberately.
SAMPLE_SHA256 = "482c5f4062faa061ed1c1fab4625c255af33755f9633ae927d797e487b1ef2ce"


# --- loading and round trips ---------------------------------------------------------------------


def test_a_valid_dataset_loads_with_everything_it_declared() -> None:
    dataset = sample()
    assert dataset.identity == "knowledge_retrieval_sample_v1"
    assert [d.doc_id for d in dataset.documents][:2] == ["pool-old", "pool"]
    assert dataset.documents[1].supersedes == "pool-old"
    assert dataset.documents[0].source.module == SAMPLE_MODULE
    hours = dataset.queries[0]
    assert hours.limit == 20
    assert hours.grade_of(ChunkRef("pool", 0)) == 2
    assert hours.grade_of(ChunkRef("pisina", 0)) == 1
    assert hours.grade_of(ChunkRef("pool", 1)) == 0
    assert hours.grade_of(ChunkRef("parking", 0)) == 0  # unjudged
    assert hours.sufficient == frozenset({ChunkRef("pool", 0)})
    dog = dataset.queries[1]
    assert dog.provenance == schema.Provenance(
        capture_file="tests/evaluation/fixtures/r29y_knowledge_capture.json",
        capture_sha256=CAPTURE_SHA,
        case="dog_fee",
        limit=5,
        occurrences=(
            schema.Occurrence("copilot_answer@v2", 1),
            schema.Occurrence("copilot_answer@v8", 2),
        ),
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
    assert text.startswith('{"cutoff":5,"documents":[{"doc_id":"pool-old","hotel":"evaluated",')
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
    # Upload order decides the search's tie-break: swapping two documents is a new dataset.
    moved["documents"][2], moved["documents"][3] = moved["documents"][3], moved["documents"][2]
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


def _both(*changes: Change) -> Change:
    def change(block: dict[str, Any]) -> None:
        for each in changes:
            each(block)

    return change


def _swap(key: str, first: int, second: int) -> Change:
    def change(block: dict[str, Any]) -> None:
        items = block[key]
        items[first], items[second] = items[second], items[first]

    return change


Q0, Q1, Q3 = ("queries", 0), ("queries", 1), ("queries", 3)
D0 = ("documents", 1)  # "pool", the two-paragraph document

REFUSED: list[tuple[str, Change, str]] = [
    ("unknown top-level field", _set(("extra",), 1), "unknown field"),
    ("unknown query field", _set((*Q0, "notes"), "x"), "unknown field"),
    ("unknown document field", _set((*D0, "notes"), "x"), "unknown field"),
    ("unknown judgment field", _set((*Q0, "relevant", 0, "why"), "x"), "unknown field"),
    ("unknown provenance field", _set((*Q1, "provenance", "model"), "x"), "unknown field"),
    ("missing query field", _delete((*Q0, "rationale")), "missing field"),
    ("missing provenance field", _delete((*Q1, "provenance", "case")), "missing field"),
    ("another schema", _set(("schema",), "knowledge_retrieval_schema_v0"), "schema must be"),
    ("another cutoff", _set(("cutoff",), 10), "cutoff must be 5"),
    ("a boolean cutoff", _set(("cutoff",), True), "must be an integer"),
    ("a malformed set_id", _set(("set_id",), "Knowledge Retrieval"), "not a valid identifier"),
    ("a malformed version", _set(("version",), "2"), "not a valid identifier"),
    ("a malformed doc_id", _set((*D0, "doc_id"), "Pool Policy"), "not a valid identifier"),
    ("a malformed query_id", _set((*Q0, "query_id"), " q-hours"), "not a valid identifier"),
    ("a duplicate doc_id", _set(("documents", 2, "doc_id"), "pool"), "doc_id is used twice"),
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
            {
                "capture_file": "tests/evaluation/fixtures/r29y_knowledge_capture.json",
                "capture_sha256": CAPTURE_SHA,
                "case": "pool_hours",
                "limit": 20,
                "occurrences": [{"run": "copilot_answer@v2", "attempt": 1}],
            },
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
    # --- v2: sources, versions, limits and occurrence provenance (R2.10C.3a) ---
    ("a missing source", _delete((*D0, "source")), "missing field"),
    ("an unknown source field", _set((*D0, "source", "note"), "x"), "unknown field"),
    (
        "a source digest that does not match the text",
        _set((*D0, "source", "text_sha256"), "b" * 64),
        "does not match its paragraphs",
    ),
    ("a malformed source module", _set((*D0, "source", "module"), "../x.py"), "not a valid"),
    ("an absolute source module", _set((*D0, "source", "module"), "/x.py"), "not a valid"),
    ("a malformed source symbol", _set((*D0, "source", "symbol"), "x y"), "not a valid"),
    ("a missing supersedes", _delete((*D0, "supersedes")), "missing field"),
    (
        "supersedes an unknown document",
        _set(("documents", 1, "supersedes"), "gym"),
        "supersedes an unknown document",
    ),
    ("supersedes itself", _set(("documents", 1, "supersedes"), "pool"), "cannot supersede itself"),
    (
        "supersedes a version that is not superseded",
        _set(("documents", 2, "supersedes"), "faq"),
        "which is not superseded",
    ),
    (
        "a superseded version without a successor",
        _set(("documents", 1, "supersedes"), None),
        "needs exactly one successor",
    ),
    (
        "a version superseded twice",
        _set(("documents", 2, "supersedes"), "pool-old"),
        "is superseded twice",
    ),
    (
        "a new version before the one it supersedes",
        _swap("documents", 0, 1),
        "a new version is made from it",
    ),
    (
        "supersedes across hotels",
        _set(("documents", 7, "supersedes"), "pool-old"),
        "across hotels",
    ),
    ("a limit of 0", _set((*Q0, "limit"), 0), "must be between 1 and 20"),
    ("a limit above 20", _set((*Q0, "limit"), 21), "must be between 1 and 20"),
    ("a boolean limit", _set((*Q0, "limit"), True), "must be an integer"),
    ("a missing limit", _delete((*Q0, "limit")), "missing field"),
    (
        "a provenance limit that differs",
        _set((*Q1, "provenance", "limit"), 20),
        "differs from the query's limit",
    ),
    ("no occurrences", _set((*Q1, "provenance", "occurrences"), []), "at least one occurrence"),
    (
        "an occurrence recorded twice",
        _append((*Q1, "provenance", "occurrences"), {"run": "copilot_answer@v2", "attempt": 1}),
        "recorded twice",
    ),
    (
        "attempt 0",
        _set((*Q1, "provenance", "occurrences", 0, "attempt"), 0),
        "attempt must be at least 1",
    ),
    (
        "a malformed run",
        _set((*Q1, "provenance", "occurrences", 0, "run"), "Copilot Answer"),
        "not a valid identifier",
    ),
    (
        "an unknown occurrence field",
        _set((*Q1, "provenance", "occurrences", 0, "model"), "x"),
        "unknown field",
    ),
    (
        "a malformed capture file",
        _set((*Q1, "provenance", "capture_file"), "/abs/capture.json"),
        "not a valid",
    ),
    # --- F1: each of these breaks one rule only, so dropping that rule accepts the dataset ---
    (
        # faq comes after parking and nothing else replaces parking: only the status rule objects.
        "a successor of an active version",
        _set(("documents", 4, "supersedes"), "parking"),
        "which is not superseded",
    ),
    (
        # The neighbour's rooftop page is pool-old's only successor: only the hotel rule objects.
        "a sole successor in another hotel",
        _both(
            _set(("documents", 1, "supersedes"), None),
            _set(("documents", 7, "supersedes"), "pool-old"),
        ),
        "across hotels",
    ),
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
            limit=5,
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


# --- v2: what the new rules accept (R2.10C.3a) ----------------------------------------------------


def test_a_run_key_keeps_its_at_sign_as_captured() -> None:
    dog = next(q for q in sample().queries if q.query_id == "q-dog")
    assert dog.provenance is not None
    assert [o.run for o in dog.provenance.occurrences] == ["copilot_answer@v2", "copilot_answer@v8"]
    assert '"run":"copilot_answer@v2"' in sample().canonical_json()


def test_occurrences_are_stored_sorted() -> None:
    reordered = sample_mapping()
    dog = next(q for q in reordered["queries"] if q["query_id"] == "q-dog")
    dog["provenance"]["occurrences"].reverse()
    assert load_dataset(reordered).checksum() == sample().checksum()


def test_a_chain_of_versions_loads() -> None:
    block = sample_mapping()
    first = {**block["documents"][0], "doc_id": "pool-v1", "supersedes": None}
    block["documents"][0]["supersedes"] = "pool-v1"  # pool-old is now itself a successor
    block["documents"].insert(0, first)
    chain = load_dataset(block)
    assert [(d.doc_id, d.status, d.supersedes) for d in chain.documents[:3]] == [
        ("pool-v1", "superseded", None),
        ("pool-old", "superseded", "pool-v1"),
        ("pool", "active", "pool-old"),
    ]


def test_a_withdrawn_version_needs_no_successor() -> None:
    spa = next(d for d in sample().documents if d.doc_id == "spa")
    assert (spa.status, spa.supersedes) == ("withdrawn", None)


def test_the_source_digest_is_of_the_paragraphs_joined_by_a_blank_line() -> None:
    pool = next(d for d in sample().documents if d.doc_id == "pool")
    joined = "\n\n".join(pool.paragraphs)
    assert pool.content == joined
    assert pool.source.text_sha256 == hashlib.sha256(joined.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("limit", [1, 5, 20])
def test_every_limit_from_1_to_20_is_accepted(limit: int) -> None:
    block = sample_mapping()
    block["queries"][0]["limit"] = limit
    assert load_dataset(block).queries[0].limit == limit


# --- F3: version-chain regressions (R2.10C.3a remediation) ----------------------------------------


def _version(
    base: dict[str, Any], doc_id: str, status: str, supersedes: str | None
) -> dict[str, Any]:
    return {**copy.deepcopy(base), "doc_id": doc_id, "status": status, "supersedes": supersedes}


def test_a_cycle_of_versions_is_refused() -> None:
    """pool-old and pool replace each other. A successor must come strictly after what it
    replaces, so one edge of any cycle points backwards and the order rule refuses it."""
    block = sample_mapping()
    block["documents"][0]["supersedes"] = "pool"
    block["documents"][1]["status"] = "superseded"
    with pytest.raises(DatasetError, match="'pool-old' must come after 'pool'"):
        load_dataset(block)


def test_two_independent_version_chains_load() -> None:
    block = sample_mapping()
    spa = block["documents"][6]
    block["documents"] += [
        _version(spa, "spa-v1", "superseded", None),
        _version(spa, "spa-v2", "active", "spa-v1"),
    ]
    dataset = load_dataset(block)
    successors = {d.supersedes: d.doc_id for d in dataset.documents if d.supersedes is not None}
    assert successors == {"pool-old": "pool", "spa-v1": "spa-v2"}


def test_a_withdrawn_version_may_be_a_successor() -> None:
    """v1 replaced by v2, then v2 withdrawn: the real routes allow it. Both are out of scope, so a
    query may name them as scope traps, and every judgment stays valid."""
    block = sample_mapping()
    spa = block["documents"][6]
    block["documents"] += [
        _version(spa, "rules-v1", "superseded", None),
        _version(spa, "rules-v2", "withdrawn", "rules-v1"),
    ]
    block["queries"][4]["forbidden"].append({"doc_id": "rules-v2", "ordinal": 0})
    dataset = load_dataset(block)
    assert [(d.doc_id, d.status, d.supersedes) for d in dataset.documents[-2:]] == [
        ("rules-v1", "superseded", None),
        ("rules-v2", "withdrawn", "rules-v1"),
    ]
    assert ChunkRef("rules-v2", 0) in dataset.queries[4].forbidden
