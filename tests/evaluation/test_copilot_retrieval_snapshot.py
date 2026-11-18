"""C.5 offline: the copilot retrieval snapshot, `SnapshotKnowledge` and the harness's opt-in.

Gate 3 itself, the snapshot against live search, is
`tests/integration/test_copilot_retrieval_snapshot.py`.
Offline: no database, no network.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.copilot.tools.knowledge_search import KnowledgeSearchArguments
from tests.evaluation import copilot_retrieval_snapshot as snap
from tests.evaluation import fixture_documents as fixtures
from tests.evaluation import knowledge_harness as kh
from tests.evaluation import retrieval_canonical as canon
from tests.evaluation import retrieval_probes as rp
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.fixture_hotel import EVAL_HOTEL
from tests.evaluation.knowledge_questions import COPILOT_KNOWLEDGE_EVAL_V1
from tests.evaluation.replay import load_replays


@pytest.fixture(scope="module")
def corpus() -> Any:
    return snap.copilot_corpus(v2.load())


@pytest.fixture
def pinned() -> dict[str, Any]:
    return copy.deepcopy(canon.loads(snap.pinned_bytes()))


def search(document: dict[str, Any], query: str, limit: int) -> dict[str, Any]:
    (entry,) = [s for s in document["searches"] if (s["query"], s["limit"]) == (query, limit)]
    return entry


def row(corpus: Any, doc_id: str) -> dict[str, Any]:
    """A result row that names *doc_id*'s fixture chunk correctly, served hash included."""
    chunk = snap.fixture_chunks(corpus)[doc_id]
    return {
        "doc_id": doc_id,
        "ordinal": chunk.ordinal,
        "served_sha256": rp.served_sha256(
            doc_id, chunk.ordinal, chunk.title, chunk.version, "english", chunk.text
        ),
    }


def parse(document: dict[str, Any], corpus: Any) -> snap.Snapshot:
    return snap.parse(canon.dumps(document), corpus)


def replays() -> Any:
    return load_replays(kh.REFERENCE_REPLAYS, COPILOT_KNOWLEDGE_EVAL_V1, prompt=kh.REFERENCE_PROMPT)


# --- the pinned file -----------------------------------------------------------------------------


def test_the_snapshot_is_pinned_by_its_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = snap.pinned_bytes()
    assert hashlib.sha256(data).hexdigest() == snap.SNAPSHOT_SHA256
    assert b"\n" not in data and b"\r" not in data  # canonical: no line break to convert
    changed = tmp_path / "snapshot.json"
    changed.write_bytes(data.replace(b'"limit":5', b'"limit":6', 1))
    monkeypatch.setattr(snap, "SNAPSHOT_PATH", changed)
    with pytest.raises(snap.RetrievalSnapshotError, match="is not the pinned"):
        snap.pinned_bytes()


def test_the_pinned_snapshot_loads_and_holds_every_copilot_search() -> None:
    loaded = snap.load()
    assert sorted(loaded.searches) == list(snap.copilot_searches())
    assert loaded.sha256 == snap.SNAPSHOT_SHA256


def test_the_snapshot_records_what_real_search_returned(corpus: Any) -> None:
    """The pinned record, read back: §1.3's predictions, now as recorded."""
    served = {key: [c.title for c in chunks] for key, chunks in snap.load().searches.items()}
    assert served[("dog fee", 5)] == ["Pets"]
    assert served[("parking cost", 5)] == ["Parking"]
    assert served[("towels provided at the pool", 5)] == ["Pool policy"]
    assert served[("pool open hours", 5)] == []  # the all-terms rule: "hour" is not in the text
    assert served[("FAQ", 20)] == []  # "FAQ" is only in the title, which is not indexed
    assert served[("dog pet cost", 5)] == []


# --- the corpus ----------------------------------------------------------------------------------


def test_the_corpus_is_the_copilot_corpus_alone(corpus: Any) -> None:
    assert rp.plan(corpus) == [
        "copilot.pool-policy-v1",
        "copilot.pool-policy-v2",
        "copilot.parking",
        "copilot.pets",
        "copilot.spa",
        "copilot.guest-faq",
        "neighbour.rooftop-bar",
    ]
    chunks = snap.fixture_chunks(corpus)
    assert sorted(chunks.values(), key=fixtures.CORPUS.index) == list(fixtures.CORPUS)


def test_a_copy_that_is_not_its_fixture_chunk_refuses(corpus: Any) -> None:
    documents = list(corpus.documents)
    documents[2] = dataclasses.replace(documents[2], title="Car park")
    changed = dataclasses.replace(corpus, documents=tuple(documents))
    with pytest.raises(snap.RetrievalSnapshotError, match="is not the fixture chunk"):
        snap.fixture_chunks(changed)


def test_a_corpus_missing_a_fixture_chunk_refuses() -> None:
    """Two copies of one fixture chunk and none of another: the copies are not one per chunk."""
    dataset = v2.load()
    documents = [
        dataclasses.replace(d, source=dataclasses.replace(d.source, symbol="CORPUS[2]"))
        if d.doc_id == "copilot.pets"
        else d
        for d in dataset.documents
    ]
    with pytest.raises(snap.RetrievalSnapshotError, match="each fixture chunk once"):
        snap.copilot_corpus(dataclasses.replace(dataset, documents=tuple(documents)))


# --- the searches --------------------------------------------------------------------------------


def test_the_searches_are_the_replays_and_the_capture_exactly() -> None:
    assert len(snap.replay_searches()) == 10
    assert len(snap.capture_searches()) == 11
    assert len(snap.replay_searches() & snap.capture_searches()) == 4
    assert len(snap.copilot_searches()) == 17
    assert ("FAQ", 20) in snap.copilot_searches()  # sent with the limit as captured


def test_a_replay_without_a_limit_gets_the_tools_default() -> None:
    default = KnowledgeSearchArguments.model_fields["limit"].default
    assert ("casino", default) in snap.replay_searches()
    assert {limit for _, limit in snap.replay_searches()} == {default}


def test_replays_that_are_not_the_pinned_file_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / snap.REPLAYS_FILE
    target.parent.mkdir(parents=True)
    data = (snap.REPOSITORY_ROOT / snap.REPLAYS_FILE).read_bytes()
    target.write_bytes(data.replace(b'"casino"', b'"casinos"', 1))
    monkeypatch.setattr(snap, "REPOSITORY_ROOT", tmp_path)
    with pytest.raises(snap.RetrievalSnapshotError, match="replays SHA-256"):
        snap.replay_searches()


def test_search_arguments_the_tool_refuses_refuse() -> None:
    with pytest.raises(snap.RetrievalSnapshotError, match="arguments the search tool refuses"):
        snap._effective({"query": "pool", "limit": 21}, "here")


# --- parsing refuses everything else -------------------------------------------------------------


def test_bytes_that_are_not_canonical_refuse(corpus: Any) -> None:
    pretty = snap.pinned_bytes().replace(b",", b", ", 1)
    with pytest.raises(snap.RetrievalSnapshotError, match="not canonical"):
        snap.parse(pretty, corpus)


def test_a_snapshot_of_another_corpus_refuses(corpus: Any, pinned: dict[str, Any]) -> None:
    pinned["corpus"]["documents"].reverse()
    with pytest.raises(snap.RetrievalSnapshotError, match="another corpus"):
        parse(pinned, corpus)


def test_another_snapshot_or_source_refuses(corpus: Any, pinned: dict[str, Any]) -> None:
    pinned["source"] = "fixture"
    with pytest.raises(snap.RetrievalSnapshotError, match="not the copilot retrieval snapshot"):
        parse(pinned, corpus)


def test_more_results_than_the_limit_refuse(corpus: Any, pinned: dict[str, Any]) -> None:
    entry = search(pinned, "parking cost", 5)
    entry["results"] = [row(corpus, "copilot.parking")] + [row(corpus, "copilot.pets")] * 5
    with pytest.raises(snap.RetrievalSnapshotError, match="more results than the limit"):
        parse(pinned, corpus)


@pytest.mark.parametrize(
    "doc_id", ["copilot.pool-policy-v1", "copilot.spa", "neighbour.rooftop-bar"]
)
def test_a_result_outside_the_evaluated_hotels_active_documents_refuses(
    corpus: Any, pinned: dict[str, Any], doc_id: str
) -> None:
    search(pinned, "casino", 5)["results"] = [row(corpus, doc_id)]
    with pytest.raises(snap.RetrievalSnapshotError, match=f"leaks {doc_id}"):
        parse(pinned, corpus)


def test_a_served_hash_that_is_not_the_fixture_chunk_refuses(
    corpus: Any, pinned: dict[str, Any]
) -> None:
    wrong = row(corpus, "copilot.parking") | {"served_sha256": "0" * 64}
    search(pinned, "parking cost", 5)["results"] = [wrong]
    with pytest.raises(snap.RetrievalSnapshotError, match="is not the fixture chunk"):
        parse(pinned, corpus)


def test_a_chunk_the_document_does_not_have_refuses(corpus: Any, pinned: dict[str, Any]) -> None:
    search(pinned, "parking cost", 5)["results"] = [row(corpus, "copilot.parking") | {"ordinal": 1}]
    with pytest.raises(snap.RetrievalSnapshotError, match="has no such chunk"):
        parse(pinned, corpus)


def test_a_chunk_served_twice_refuses(corpus: Any, pinned: dict[str, Any]) -> None:
    search(pinned, "parking cost", 5)["results"] = [row(corpus, "copilot.parking")] * 2
    with pytest.raises(snap.RetrievalSnapshotError, match="twice"):
        parse(pinned, corpus)


def test_a_search_recorded_twice_refuses(corpus: Any, pinned: dict[str, Any]) -> None:
    entry = search(pinned, "parking cost", 5)
    pinned["searches"].append(entry | {"results": []})
    pinned["searches"] = canon.sorted_unique(pinned["searches"])
    with pytest.raises(snap.RetrievalSnapshotError, match="recorded twice"):
        parse(pinned, corpus)


def test_a_missing_or_an_extra_search_refuses(corpus: Any, pinned: dict[str, Any]) -> None:
    fewer = copy.deepcopy(pinned)
    fewer["searches"] = [s for s in fewer["searches"] if s["query"] != "casino"]
    with pytest.raises(snap.RetrievalSnapshotError, match="exactly the copilot's searches"):
        parse(fewer, corpus)
    more = copy.deepcopy(pinned)
    more["searches"] = canon.sorted_unique(
        [*more["searches"], {"query": "casino", "limit": 6, "results": []}]
    )
    with pytest.raises(snap.RetrievalSnapshotError, match="exactly the copilot's searches"):
        parse(more, corpus)


# --- building -------------------------------------------------------------------------------------


def rebuilt(corpus: Any, pinned: dict[str, Any]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    return {(s["query"], s["limit"]): s["results"] for s in pinned["searches"]}


def test_building_from_the_recorded_results_writes_the_pinned_bytes(
    corpus: Any, pinned: dict[str, Any]
) -> None:
    built = snap.build(corpus, pinned["engine"]["server_version_num"], rebuilt(corpus, pinned))
    assert canon.dumps(built) == snap.pinned_bytes()


def test_building_refuses_a_leak_a_long_list_or_other_searches(
    corpus: Any, pinned: dict[str, Any]
) -> None:
    version = pinned["engine"]["server_version_num"]
    leaking = rebuilt(corpus, pinned) | {("casino", 5): [row(corpus, "copilot.spa")]}
    with pytest.raises(snap.RetrievalSnapshotError, match=r"leaks copilot\.spa"):
        snap.build(corpus, version, leaking)
    long = rebuilt(corpus, pinned) | {("casino", 5): [row(corpus, "copilot.parking")] * 6}
    with pytest.raises(snap.RetrievalSnapshotError, match="more results than the limit"):
        snap.build(corpus, version, long)
    other = {**rebuilt(corpus, pinned), ("casino", 6): []}
    with pytest.raises(snap.RetrievalSnapshotError, match="exactly the copilot evaluation's"):
        snap.build(corpus, version, other)


def test_differences_name_what_changed(pinned: dict[str, Any]) -> None:
    live = copy.deepcopy(pinned)
    search(live, "parking cost", 5)["results"] = []
    live["engine"]["server_version_num"] += 1
    assert snap.differences(pinned, live) == ["engine", "search 'parking cost' (limit 5)"]
    assert snap.differences(pinned, pinned) == []


# --- SnapshotKnowledge ----------------------------------------------------------------------------


def test_snapshot_knowledge_serves_the_recorded_fixture_chunks() -> None:
    knowledge = snap.SnapshotKnowledge()
    response = knowledge.search(EVAL_HOTEL, "parking cost", 5)
    parking = fixtures.CORPUS[2]
    assert (response.query, response.limit) == ("parking cost", 5)
    assert [
        (r.chunk_public_id, r.document_public_id, r.title, r.version, r.ordinal, r.text)
        for r in response.results
    ] == [
        (
            parking.chunk_public_id,
            parking.document_public_id,
            parking.title,
            parking.version,
            parking.ordinal,
            parking.text,
        )
    ]
    assert knowledge.search(EVAL_HOTEL, "pool open hours", 5).results == []
    assert knowledge.misses == []


def test_a_search_the_snapshot_does_not_hold_refuses_and_is_recorded() -> None:
    knowledge = snap.SnapshotKnowledge()
    with pytest.raises(snap.SnapshotMissError):
        knowledge.search(EVAL_HOTEL, "breakfast hours", 5)
    assert knowledge.misses == [(str(EVAL_HOTEL), "breakfast hours", 5)]


def test_the_limit_is_part_of_the_key() -> None:
    knowledge = snap.SnapshotKnowledge()
    assert knowledge.search(EVAL_HOTEL, "FAQ", 20).limit == 20
    with pytest.raises(snap.SnapshotMissError):
        knowledge.search(EVAL_HOTEL, "FAQ", 5)
    with pytest.raises(snap.SnapshotMissError):
        knowledge.search(EVAL_HOTEL, "parking cost", 4)
    assert knowledge.misses == [(str(EVAL_HOTEL), "FAQ", 5), (str(EVAL_HOTEL), "parking cost", 4)]


def test_another_hotel_refuses_and_is_recorded() -> None:
    knowledge = snap.SnapshotKnowledge()
    with pytest.raises(snap.SnapshotMissError):
        knowledge.search(fixtures.NEIGHBOUR_HOTEL, "rooftop bar", 5)
    assert knowledge.misses == [(str(fixtures.NEIGHBOUR_HOTEL), "rooftop bar", 5)]


# --- the harness's opt-in -------------------------------------------------------------------------


def test_the_stand_in_stays_the_default() -> None:
    pinned = kh.REFERENCE_REPORT.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    loaded = replays()
    # Both entry points, each left at its own default: run_replays passes its default on to
    # run, so run's default is only seen when run is called without one.
    direct = kh.run(
        COPILOT_KNOWLEDGE_EVAL_V1,
        lambda case: kh.ReplayModel(loaded.cases[case.case_id]),
        mode="replay",
        provider=loaded.provider,
        model=loaded.model,
        recorded_on=loaded.recorded_on,
        prompt=kh.REFERENCE_PROMPT,
    )
    for report in (kh.run_replays(loaded, prompt=kh.REFERENCE_PROMPT), direct):
        assert "knowledge_source" not in report
        assert report["statement"] == kh.REFERENCE_STATEMENT
        assert kh.canonical(report) == pinned


def test_a_snapshot_run_is_labelled_and_says_what_its_search_was() -> None:
    report = kh.run_replays(replays(), prompt=kh.REFERENCE_PROMPT, knowledge_source=kh.SNAPSHOT)
    assert report["knowledge_source"] == {
        "source": snap.SOURCE_LABEL,
        "snapshot": snap.SNAPSHOT_ID,
        "sha256": snap.SNAPSHOT_SHA256,
    }
    assert report["statement"] == kh.SNAPSHOT_REFERENCE_STATEMENT
    assert report["measures"]["tool_selection"] == {"passed": 11, "scored": 11}


def test_a_snapshot_miss_refuses_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    full = snap.load()
    fewer = {key: chunks for key, chunks in full.searches.items() if key != ("casino", 5)}
    monkeypatch.setattr(snap, "load", lambda: snap.Snapshot(sha256=full.sha256, searches=fewer))
    with pytest.raises(snap.RetrievalSnapshotError, match="casino"):
        kh.run_replays(replays(), prompt=kh.REFERENCE_PROMPT, knowledge_source=kh.SNAPSHOT)


def test_an_unknown_knowledge_source_refuses() -> None:
    with pytest.raises(ValueError, match="knowledge_source"):
        kh.run_replays(replays(), prompt=kh.REFERENCE_PROMPT, knowledge_source="live")


def test_the_live_statement_names_its_document_search() -> None:
    fixture = kh.live_statement("ollama", "m", "2026-11-18", 11, prompt=kh.REFERENCE_PROMPT)
    assert "with fixed tool data and a fixed stand-in for document search. It describes" in fixture
    snapshot = kh.live_statement(
        "ollama", "m", "2026-11-18", 11, prompt=kh.REFERENCE_PROMPT, knowledge_source=kh.SNAPSHOT
    )
    assert f"pinned PostgreSQL snapshot {snap.SNAPSHOT_ID}. It describes" in snapshot
    assert "stand-in" not in snapshot


# --- regeneration ---------------------------------------------------------------------------------


def test_the_snapshot_is_regenerated_only_on_an_explicit_request_outside_ci() -> None:
    assert snap.regeneration_requested({}) is False
    assert snap.regeneration_requested({snap.REGENERATE_VARIABLE: "true"}) is False
    assert snap.regeneration_requested({snap.REGENERATE_VARIABLE: "1"}) is True
    with pytest.raises(snap.RetrievalSnapshotError, match="never regenerated in CI"):
        snap.regeneration_requested({snap.REGENERATE_VARIABLE: "1", "CI": "true"})
