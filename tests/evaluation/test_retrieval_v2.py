"""R2.10C.3b -- `knowledge_retrieval_v2`: the snapshot, its sources and the R2.9Y capture.

Offline: no database, no search, no model. The judgments in the snapshot are the developer's,
unreviewed until C.3c and unsigned until C.3d; these tests check integrity, not the judgments.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.knowledge.chunking import chunk_text
from tests.evaluation import fixture_documents
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval import QUERIES as V1_QUERIES
from tests.evaluation.retrieval_schema import (
    SCHEMA_VERSION,
    DatasetError,
    DocumentSource,
    RetrievalDataset,
    content_sha256,
    load_dataset,
)

#: Pinned here as well as in the module, so that changing the snapshot and the module's constant
#: together still fails a test.
EXPECTED_SHA256 = "35a968055c790628c2bd9c20ee48913eca0dcd9b0929df30f140c81370a6d5ed"


@pytest.fixture(scope="module")
def dataset() -> RetrievalDataset:
    return v2.load()


@pytest.fixture(scope="module")
def capture() -> dict[str, Any]:
    return dict(v2.read_capture())


@pytest.fixture(scope="module")
def captured(capture: dict[str, Any]) -> tuple[v2.CapturedQuery, ...]:
    return v2.captured_queries(capture)


# --- the snapshot ---------------------------------------------------------------------------------


def test_the_snapshot_loads_under_schema_v2(dataset: RetrievalDataset) -> None:
    assert dataset.identity == "knowledge_retrieval_v2"
    assert dataset.as_mapping()["schema"] == SCHEMA_VERSION == "knowledge_retrieval_schema_v2"
    assert (len(dataset.documents), len(dataset.chunks()), len(dataset.queries)) == (17, 26, 57)


def test_the_snapshot_checksum_is_pinned(dataset: RetrievalDataset) -> None:
    assert dataset.checksum() == v2.DATASET_SHA256 == EXPECTED_SHA256


def test_the_file_is_exactly_its_canonical_rendering(dataset: RetrievalDataset) -> None:
    # A Windows checkout may turn LF into CRLF (`text=auto`); the content is compared without it.
    text = v2.DATASET_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text == v2.render(dataset)


def test_a_changed_snapshot_is_refused(dataset: RetrievalDataset, tmp_path: Path) -> None:
    mapping = dataset.as_mapping()
    mapping["queries"][0]["rationale"] += " Edited."
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(mapping), encoding="utf-8")
    with pytest.raises(v2.SnapshotError, match="is not the pinned"):
        v2.load(edited)
    mapping["documents"][0]["paragraphs"][0] += " Edited."  # without its source digest
    edited.write_text(json.dumps(mapping), encoding="utf-8")
    with pytest.raises(DatasetError, match="does not match its paragraphs"):
        v2.load(edited)


def test_every_chunk_is_what_the_real_chunker_produces(dataset: RetrievalDataset) -> None:
    for document in dataset.documents:
        drafts = chunk_text(document.content)
        assert [draft.text for draft in drafts] == list(document.paragraphs), document.doc_id
        assert [draft.ordinal for draft in drafts] == list(range(len(document.paragraphs)))


# --- copied documents and drift -------------------------------------------------------------------


def test_every_document_matches_its_source(dataset: RetrievalDataset) -> None:
    assert v2.source_drift(dataset.documents) == []
    modules = Counter(document.source.module for document in dataset.documents)
    assert modules == {
        "tests/evaluation/retrieval.py": 10,
        "tests/evaluation/fixture_documents.py": 7,
    }


def _rooftop(dataset: RetrievalDataset) -> Any:
    return next(d for d in dataset.documents if d.doc_id == "neighbour.rooftop-bar")


def _repaint(document: Any, paragraphs: tuple[str, ...]) -> Any:
    source = DocumentSource(
        document.source.module, document.source.symbol, content_sha256(paragraphs)
    )
    return dataclasses.replace(document, paragraphs=paragraphs, source=source)


DRIFTS: list[tuple[str, Callable[[Any], Any], str]] = [
    ("title", lambda d: dataclasses.replace(d, title="Roof terrace"), "title differs"),
    ("language", lambda d: dataclasses.replace(d, language="simple"), "language differs"),
    ("status", lambda d: dataclasses.replace(d, status="withdrawn"), "status differs"),
    ("hotel", lambda d: dataclasses.replace(d, hotel="evaluated"), "hotel differs"),
    ("paragraphs", lambda d: _repaint(d, ("The rooftop bar is closed.",)), "paragraphs differs"),
    (
        "another-source",
        lambda d: dataclasses.replace(
            d, source=DocumentSource(d.source.module, "CORPUS[5]", d.source.text_sha256)
        ),
        "title differs",
    ),
    (
        "unknown-source",
        lambda d: dataclasses.replace(
            d, source=DocumentSource("tests/evaluation/other.py", "X[0]", d.source.text_sha256)
        ),
        "source cannot be read",
    ),
]


@pytest.mark.parametrize(
    ("change", "expected"), [d[1:] for d in DRIFTS], ids=[d[0] for d in DRIFTS]
)
def test_a_copy_that_differs_from_its_source_is_reported(
    dataset: RetrievalDataset, change: Callable[[Any], Any], expected: str
) -> None:
    problems = v2.source_drift([change(_rooftop(dataset))])
    assert any(expected in problem for problem in problems), problems


def test_a_source_that_changes_after_the_copy_is_reported(
    dataset: RetrievalDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = list(fixture_documents.CORPUS)
    corpus[2] = dataclasses.replace(corpus[2], text="Parking costs 18.00 EUR per night.")
    monkeypatch.setattr(fixture_documents, "CORPUS", tuple(corpus))
    where = "copilot.parking (tests/evaluation/fixture_documents.py:CORPUS[2])"
    assert v2.source_drift(dataset.documents) == [
        f"{where}: paragraphs differs from its source",
        f"{where}: text_sha256 does not match the source text",
    ]


# --- the R2.9Y capture ----------------------------------------------------------------------------

#: In capture order: the capture lists cases alphabetically, and ids count each case's distinct
#: queries by first occurrence, so `pool closing time hours` (v2 attempt 1) is .1.
CAPTURED_IDS = [
    ("r29y.casino.1", "casino", 5, 6),
    ("r29y.dog_fee.1", "dog pet cost", 5, 6),
    ("r29y.guest_faq_injected.1", "FAQ", 20, 6),
    ("r29y.neighbour_rooftop_bar.1", "rooftop bar open hours", 5, 6),
    ("r29y.occupancy_and_parking.1", "parking cost", 5, 6),
    ("r29y.parking_price.1", "parking cost per night", 5, 6),
    ("r29y.pool_closing_changed.1", "pool closing time hours", 5, 1),
    ("r29y.pool_hours.1", "pool open hours", 5, 6),
    ("r29y.pool_towels_injected.1", "towels provided at the pool", 5, 6),
    ("r29y.spa_hours_withdrawn.1", "spa opening hours", 5, 6),
    ("r29y.pool_closing_changed.2", "pool closing time", 5, 5),
]


def test_the_capture_is_the_pinned_one(tmp_path: Path) -> None:
    v2.read_capture()  # refuses unless the bytes hash to the pinned SHA-256
    original = (v2.REPOSITORY_ROOT / v2.CAPTURE_FILE).read_bytes()
    altered = tmp_path / "capture.json"
    altered.write_bytes(original + b" ")
    with pytest.raises(v2.SnapshotError, match="is not the pinned"):
        v2.read_capture(altered)


def test_a_crlf_checkout_of_the_capture_is_the_same_capture(tmp_path: Path) -> None:
    lf = (v2.REPOSITORY_ROOT / v2.CAPTURE_FILE).read_bytes().replace(b"\r\n", b"\n")
    crlf = tmp_path / "capture-crlf.json"
    crlf.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert v2.captured_queries(v2.read_capture(crlf)) == v2.captured_queries(v2.read_capture())


def test_the_capture_holds_11_queries_and_60_occurrences(
    captured: tuple[v2.CapturedQuery, ...],
) -> None:
    assert len(captured) == 11
    assert sum(len(item.occurrences) for item in captured) == 60
    assert len({item.case for item in captured}) == 10
    assert "occupied_nights_may" not in {item.case for item in captured}  # it made no search


def test_the_captured_ids_follow_first_occurrence(captured: tuple[v2.CapturedQuery, ...]) -> None:
    assert [
        (v2.captured_query_id(captured, item), item.query, item.limit, len(item.occurrences))
        for item in captured
    ] == CAPTURED_IDS


def test_the_dataset_holds_exactly_the_captured_queries(
    dataset: RetrievalDataset, captured: tuple[v2.CapturedQuery, ...]
) -> None:
    assert v2.capture_mismatches(dataset, captured, v2.CAPTURE_SHA256) == []


def test_run_keys_attempts_and_limits_are_preserved(dataset: RetrievalDataset) -> None:
    captured_queries = [q for q in dataset.queries if q.origin == "model_captured"]
    assert len(captured_queries) == 11
    for query in captured_queries:
        assert query.provenance is not None
        assert query.provenance.limit == query.limit
        assert {o.run for o in query.provenance.occurrences} <= {
            "copilot_answer@v2",
            "copilot_answer@v8",
        }
        assert {o.attempt for o in query.provenance.occurrences} <= {1, 2, 3}
    faq = next(q for q in captured_queries if q.query == "FAQ")
    assert faq.limit == 20
    hours = next(q for q in captured_queries if q.query == "pool closing time hours")
    assert hours.provenance is not None
    assert [(o.run, o.attempt) for o in hours.provenance.occurrences] == [("copilot_answer@v2", 1)]


Edit = Callable[[dict[str, Any]], None]


def _captured(mapping: dict[str, Any], query_id: str) -> dict[str, Any]:
    return dict(next(q for q in mapping["queries"] if q["query_id"] == query_id))


def _on(query_id: str, change: Callable[[dict[str, Any]], None]) -> Edit:
    def edit(mapping: dict[str, Any]) -> None:
        change(next(q for q in mapping["queries"] if q["query_id"] == query_id))

    return edit


def _set_limit(query: dict[str, Any], limit: int) -> None:
    query["limit"] = query["provenance"]["limit"] = limit


def _drop(query_id: str) -> Edit:
    def edit(mapping: dict[str, Any]) -> None:
        mapping["queries"] = [q for q in mapping["queries"] if q["query_id"] != query_id]

    return edit


def _duplicate(query_id: str, new_id: str) -> Edit:
    def edit(mapping: dict[str, Any]) -> None:
        mapping["queries"].append(
            {**copy.deepcopy(_captured(mapping, query_id)), "query_id": new_id}
        )

    return edit


DATASET_EDITS: list[tuple[str, Edit, str]] = [
    (
        "dropped-occurrence",
        _on("r29y.dog_fee.1", lambda q: q["provenance"]["occurrences"].pop()),
        "occurrences",
    ),
    (
        "changed-attempt",
        _on("r29y.casino.1", lambda q: q["provenance"]["occurrences"][0].update(attempt=9)),
        "occurrences",
    ),
    (
        "changed-run",
        _on(
            "r29y.casino.1",
            lambda q: q["provenance"]["occurrences"][0].update(run="copilot_answer@v9"),
        ),
        "occurrences",
    ),
    ("changed-limit", _on("r29y.guest_faq_injected.1", lambda q: _set_limit(q, 5)), "limit 5"),
    ("changed-text", _on("r29y.casino.1", lambda q: q.update(query="casinos")), "query 'casinos'"),
    (
        "changed-case",
        _on("r29y.casino.1", lambda q: q["provenance"].update(case="gym")),
        "case 'gym'",
    ),
    (
        "wrong-capture-sha",
        _on("r29y.casino.1", lambda q: q["provenance"].update(capture_sha256="0" * 64)),
        "capture SHA-256",
    ),
    (
        "wrong-capture-file",
        _on("r29y.casino.1", lambda q: q["provenance"].update(capture_file="tests/other.json")),
        "capture file",
    ),
    ("missing-query", _drop("r29y.pool_closing_changed.1"), "missing from the dataset"),
    ("extra-query", _duplicate("r29y.casino.1", "r29y.casino.2"), "not in the capture"),
    (
        "duplicated-query",
        _duplicate("r29y.casino.1", "r29y.casino.2"),
        "more than one dataset query",
    ),
]


@pytest.mark.parametrize(
    ("edit", "expected"), [e[1:] for e in DATASET_EDITS], ids=[e[0] for e in DATASET_EDITS]
)
def test_a_dataset_that_differs_from_the_capture_is_reported(
    dataset: RetrievalDataset, captured: tuple[v2.CapturedQuery, ...], edit: Edit, expected: str
) -> None:
    mapping = dataset.as_mapping()
    edit(mapping)
    problems = v2.capture_mismatches(load_dataset(mapping), captured, v2.CAPTURE_SHA256)
    assert any(expected in problem for problem in problems), problems


def _first_turns(capture: dict[str, Any], case: str) -> list[Any]:
    return list(capture["runs"]["copilot_answer@v2"]["attempts"]["1"]["turns"][case])


def test_a_repeat_within_one_attempt_is_refused_not_merged(capture: dict[str, Any]) -> None:
    altered = copy.deepcopy(capture)
    turns = altered["runs"]["copilot_answer@v2"]["attempts"]["1"]["turns"]["casino"]
    turns.append({"name": v2.SEARCH_TOOL, "arguments": {"query": "casino", "limit": 5}})
    with pytest.raises(v2.SnapshotError, match="cannot record a repeat without a call_index"):
        v2.captured_queries(altered)


def test_a_search_missing_from_the_capture_is_reported(
    dataset: RetrievalDataset, capture: dict[str, Any]
) -> None:
    altered = copy.deepcopy(capture)
    altered["runs"]["copilot_answer@v2"]["attempts"]["1"]["turns"]["casino"] = []
    problems = v2.capture_mismatches(dataset, v2.captured_queries(altered), v2.CAPTURE_SHA256)
    assert problems == [
        "r29y.casino.1: occurrences "
        "[('copilot_answer@v2', 1), ('copilot_answer@v2', 2), ('copilot_answer@v2', 3), "
        "('copilot_answer@v8', 1), ('copilot_answer@v8', 2), ('copilot_answer@v8', 3)], "
        "captured [('copilot_answer@v2', 2), ('copilot_answer@v2', 3), ('copilot_answer@v8', 1), "
        "('copilot_answer@v8', 2), ('copilot_answer@v8', 3)]"
    ]


def test_a_search_the_dataset_lacks_is_reported(
    dataset: RetrievalDataset, capture: dict[str, Any]
) -> None:
    altered = copy.deepcopy(capture)
    turns = altered["runs"]["copilot_answer@v2"]["attempts"]["1"]["turns"]["casino"]
    turns.append({"name": v2.SEARCH_TOOL, "arguments": {"query": "card room", "limit": 5}})
    problems = v2.capture_mismatches(dataset, v2.captured_queries(altered), v2.CAPTURE_SHA256)
    assert problems == ["r29y.casino.2: in the capture, missing from the dataset"]


@pytest.mark.parametrize("limit", [True, "5", None], ids=["boolean", "string", "missing"])
def test_a_malformed_search_call_is_refused(capture: dict[str, Any], limit: Any) -> None:
    altered = copy.deepcopy(capture)
    turns = altered["runs"]["copilot_answer@v2"]["attempts"]["1"]["turns"]["casino"]
    turns.append({"name": v2.SEARCH_TOOL, "arguments": {"query": "card room", "limit": limit}})
    with pytest.raises(v2.SnapshotError):
        v2.captured_queries(altered)


# --- families, scope traps and no-answer queries --------------------------------------------------


def test_every_family_is_covered_as_authored(dataset: RetrievalDataset) -> None:
    assert Counter(q.family for q in dataset.queries) == {
        "shared_words": 17,
        "paraphrase": 10,
        "multi_idea": 6,
        "cross_language": 6,
        "title_only": 6,
        "no_answer": 6,
        "scope_trap": 6,
    }
    assert sum(q.answerable for q in dataset.queries) == 46
    assert Counter(q.origin for q in dataset.queries) == {"developer": 46, "model_captured": 11}


def test_the_v1_queries_are_all_carried_over_verbatim(dataset: RetrievalDataset) -> None:
    carried = [q for q in dataset.queries if q.query_id.startswith("v1.")]
    assert [(q.query_id, q.query) for q in carried] == [
        (f"v1.{q.query_id}", q.query) for q in V1_QUERIES
    ]
    assert all(q.limit == 5 for q in carried)


def test_no_answer_queries_have_no_sufficient_chunk(dataset: RetrievalDataset) -> None:
    unanswerable = [q for q in dataset.queries if not q.answerable]
    assert len(unanswerable) == 11
    assert all(not q.sufficient for q in unanswerable)
    assert all(not q.answerable for q in dataset.queries if q.family == "no_answer")


def test_scope_traps_cover_every_kind_of_out_of_scope_document(dataset: RetrievalDataset) -> None:
    documents = {d.doc_id: d for d in dataset.documents}
    traps = [q for q in dataset.queries if q.family == "scope_trap"]
    assert all(q.forbidden for q in traps)
    kinds = {
        (documents[chunk.doc_id].hotel, documents[chunk.doc_id].status)
        for query in dataset.queries
        for chunk in query.forbidden
    }
    assert kinds == {
        ("evaluated", "superseded"),
        ("evaluated", "withdrawn"),
        ("neighbour", "active"),
    }
    assert all(not documents[c.doc_id].in_scope for q in dataset.queries for c in q.forbidden)


def test_the_version_chain_matches_the_copilot_corpus(dataset: RetrievalDataset) -> None:
    by_id = {d.doc_id: d for d in dataset.documents}
    order = [d.doc_id for d in dataset.documents]
    assert by_id["copilot.pool-policy-v1"].status == "superseded"
    assert by_id["copilot.pool-policy-v2"].supersedes == "copilot.pool-policy-v1"
    assert order.index("copilot.pool-policy-v1") < order.index("copilot.pool-policy-v2")
    assert (by_id["copilot.spa"].status, by_id["copilot.spa"].supersedes) == ("withdrawn", None)
    assert (by_id["neighbour.rooftop-bar"].hotel, by_id["neighbour.rooftop-bar"].status) == (
        "neighbour",
        "active",
    )
