"""R2.10C, stage C.5 -- Layer 1's pinned snapshot of real PostgreSQL search for the copilot corpus.

`docs/retrieval-evaluation-decision.md` §2 and §8: the answer and citation evaluation can be fed
what **real** search returned instead of `FixtureKnowledge`'s word overlap, and §5.1's gate 3
holds that record to live search. Your decisions of 2026-11-18 settle what §3.3 and §11 left to
C.5:

- **The corpus is the copilot corpus alone**: the seven documents of `knowledge_retrieval_v2`
  copied from `fixture_documents.py` (`CORPUS[0]` to `CORPUS[6]`), the superseded pool policy, the
  withdrawn spa page and the neighbour's rooftop bar included. Each maps to exactly one fixture
  chunk, so a served result carries the identifiers the copilot evaluation's citations resolve to.
- **An entry is keyed by (query, limit)**, exactly as sent. A pair the snapshot does not hold is
  refused, never answered with a nearest match or an empty list.
- **The searches are every one the copilot evaluation makes**: the reference replays' and the
  R2.9Y capture's, both read from their checksum-pinned files, each limit as the search tool's
  argument model gives it.
- **The snapshot was produced** through the real API on a scratch PostgreSQL 18.6 database; gate 3
  (`tests/integration/test_copilot_retrieval_snapshot.py`) compares it with live search in CI.

The snapshot is a record of what search returned, not search itself (§2). `FixtureKnowledge` stays
the default everywhere (§8); `SnapshotKnowledge` is opt-in, and a report that uses it says so
(`knowledge_harness`, §10).

Offline: no database, no network.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.copilot.tools.knowledge_search import NAME as SEARCH_TOOL
from app.copilot.tools.knowledge_search import KnowledgeSearchArguments
from app.schemas.knowledge import KnowledgeSearchResponse, KnowledgeSearchResult
from tests.evaluation import fixture_documents as fixtures
from tests.evaluation import retrieval_canonical as canon
from tests.evaluation import retrieval_probes as rp
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.fixture_hotel import EVAL_HOTEL
from tests.evaluation.retrieval_schema import CorpusDocument, RetrievalDataset

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]

SNAPSHOT_ID: Final = "copilot_retrieval_snapshot_v1"
#: What a report says its document search was, when it was this snapshot (§10).
SOURCE_LABEL: Final = "postgresql_snapshot"
SNAPSHOT_PATH: Final = HERE / "datasets" / f"{SNAPSHOT_ID}.json"
#: SHA-256 of the snapshot file's bytes, which are canonical (§12.2) and hold no line break.
SNAPSHOT_SHA256: Final = "92f0000da79ab72b0ea80e3a097bb3722864868562fbbe88ea83eda7b2acba93"

FIXTURE_MODULE: Final = "tests/evaluation/fixture_documents.py"
REPLAYS_FILE: Final = "tests/evaluation/fixtures/knowledge_reference_replays.json"
#: The LF form's SHA-256, the digest `test_expectations_v3_v2.py` pins the same file by.
REPLAYS_SHA256: Final = "a5e8b0aab53204f6a08901c11bfd861d9d1008971de38a3989923bc45f495f24"
#: Set to "1" to regenerate the snapshot from live search, outside CI only.
REGENERATE_VARIABLE: Final = "COPILOT_RETRIEVAL_SNAPSHOT_WRITE"

_RESULT = canon.OBJ(
    doc_id=canon.STR("nonempty"), ordinal=canon.INT(), served_sha256=canon.STR("sha256")
)
SNAPSHOT_SPEC: Final = canon.OBJ(
    snapshot=canon.STR("nonempty"),
    source=canon.STR("nonempty"),
    corpus=canon.OBJ(
        dataset=canon.OBJ(identity=canon.STR("nonempty"), sha256=canon.STR("sha256")),
        documents=canon.LIST(canon.STR("nonempty"), ordered=True),
    ),
    engine=canon.OBJ(server_version_num=canon.INT()),
    searches=canon.LIST(
        canon.OBJ(
            query=canon.STR("nonempty"),
            limit=canon.INT(),
            results=canon.LIST(_RESULT, ordered=True),
        ),
        ordered=False,
    ),
)


class RetrievalSnapshotError(ValueError):
    """The snapshot, its corpus or its searches are not what C.5 pinned; nothing is guessed."""


class SnapshotMissError(LookupError):
    """A search the snapshot does not hold. Recorded on the `SnapshotKnowledge` that raised it."""


# --- the copilot corpus --------------------------------------------------------------------------


def copilot_corpus(dataset: RetrievalDataset) -> RetrievalDataset:
    """`knowledge_retrieval_v2` restricted to the documents copied from `fixture_documents.py`.

    The documents keep their dataset order, so `probes.plan` uploads the superseded pool policy
    before its successor; the queries kept are those whose judgments all lie in the corpus (the
    dataset type needs one, and none is measured here). The copies must be `CORPUS[0]` to
    `CORPUS[n - 1]`, each once, for every fixture chunk: one document per chunk.
    """
    documents = tuple(d for d in dataset.documents if d.source.module == FIXTURE_MODULE)
    symbols = sorted(document.source.symbol for document in documents)
    expected = sorted(f"CORPUS[{index}]" for index in range(len(fixtures.CORPUS)))
    if symbols != expected:
        raise RetrievalSnapshotError(
            f"the copilot corpus must copy each fixture chunk once ({expected}); got {symbols}"
        )
    inside = {document.doc_id for document in documents}
    queries = tuple(
        query
        for query in dataset.queries
        if {item.ref.doc_id for item in query.relevant} | {ref.doc_id for ref in query.forbidden}
        <= inside
    )
    return RetrievalDataset(
        set_id=dataset.set_id,
        version=dataset.version,
        cutoff=dataset.cutoff,
        documents=documents,
        queries=queries,
    )


def fixture_chunks(corpus: RetrievalDataset) -> dict[str, fixtures.FixtureChunk]:
    """Each corpus document's fixture chunk, checked field by field against the copy."""
    versions = {row["doc_id"]: row["version"] for row in rp.expected_states(corpus)[0]}
    out: dict[str, fixtures.FixtureChunk] = {}
    for document in corpus.documents:
        index = int(document.source.symbol.removeprefix("CORPUS[").removesuffix("]"))
        chunk = fixtures.CORPUS[index]
        role = "neighbour" if chunk.hotel == fixtures.NEIGHBOUR_HOTEL else "evaluated"
        copied = (document.hotel, document.status, document.title, document.paragraphs)
        if copied != (role, chunk.status, chunk.title, (chunk.text,)) or chunk.ordinal != 0:
            raise RetrievalSnapshotError(f"{document.doc_id} is not the fixture chunk it copies")
        if versions[document.doc_id] != chunk.version:
            raise RetrievalSnapshotError(f"{document.doc_id}: version differs from its fixture")
        out[document.doc_id] = chunk
    return out


def _in_scope(document: CorpusDocument) -> bool:
    return document.hotel == "evaluated" and document.status == "active"


# --- the searches ---------------------------------------------------------------------------------


def _effective(arguments: Mapping[str, Any], where: str) -> tuple[str, int]:
    """The (query, limit) the search tool sends for *arguments*: its argument model decides."""
    try:
        parsed = KnowledgeSearchArguments.model_validate(dict(arguments))
    except ValueError as exc:
        raise RetrievalSnapshotError(f"{where}: arguments the search tool refuses") from exc
    return parsed.query, parsed.limit


def _search_arguments(node: object) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(node, Mapping):
        if node.get("name") == SEARCH_TOOL:
            arguments = node.get("arguments")
            if not isinstance(arguments, Mapping):
                raise RetrievalSnapshotError("a search call's arguments are not an object")
            found.append(arguments)
        for value in node.values():
            found.extend(_search_arguments(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_search_arguments(value))
    return found


def replay_searches() -> set[tuple[str, int]]:
    """Every (query, limit) the reference replays search, refused unless the file is the pinned
    one. A call without `limit` gets the tool's default, as the tool would give it."""
    raw = (REPOSITORY_ROOT / REPLAYS_FILE).read_bytes().replace(b"\r\n", b"\n")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != REPLAYS_SHA256:
        raise RetrievalSnapshotError(f"replays SHA-256 {actual} is not the pinned {REPLAYS_SHA256}")
    replays = json.loads(raw.decode("utf-8"))
    return {_effective(a, REPLAYS_FILE) for a in _search_arguments(replays["cases"])}


def capture_searches() -> set[tuple[str, int]]:
    """Every (query, limit) of the R2.9Y capture, read through `retrieval_v2`'s pinned reader."""
    return {
        _effective({"query": item.query, "limit": item.limit}, v2.CAPTURE_FILE)
        for item in v2.captured_queries(v2.read_capture())
    }


def copilot_searches() -> tuple[tuple[str, int], ...]:
    """The snapshot's keys: every search the copilot evaluation makes, sorted."""
    return tuple(sorted(replay_searches() | capture_searches()))


# --- the snapshot document ----------------------------------------------------------------------


def build(
    corpus: RetrievalDataset,
    server_version_num: int,
    results: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """The snapshot document for *results*: `{(query, limit): [{doc_id, ordinal, served_sha256}]}`
    as `probes.served_results` returns them. Refuses a result that leaks (§3.7) or exceeds its
    limit, and searches other than `copilot_searches()`, so no such record can be written."""
    if sorted(results) != list(copilot_searches()):
        raise RetrievalSnapshotError("the searches are not exactly the copilot evaluation's")
    documents = {document.doc_id: document for document in corpus.documents}
    searches = []
    for (query, limit), rows in results.items():
        if len(rows) > limit:
            raise RetrievalSnapshotError(f"{query!r} (limit {limit}): more results than the limit")
        for row in rows:
            served = documents.get(row["doc_id"])
            if served is None or not _in_scope(served):
                raise RetrievalSnapshotError(f"{query!r} (limit {limit}) leaks {row['doc_id']}")
        searches.append(
            {
                "query": query,
                "limit": limit,
                "results": [
                    {
                        "doc_id": row["doc_id"],
                        "ordinal": row["ordinal"],
                        "served_sha256": row["served_sha256"],
                    }
                    for row in rows
                ],
            }
        )
    document = {
        "snapshot": SNAPSHOT_ID,
        "source": "postgresql",
        "corpus": {
            "dataset": {"identity": corpus.identity, "sha256": v2.DATASET_SHA256},
            "documents": rp.plan(corpus),
        },
        "engine": {"server_version_num": server_version_num},
        "searches": canon.sorted_unique(searches),
    }
    canon.check(document, SNAPSHOT_SPEC)
    return document


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A loaded snapshot: for each (query, limit), the fixture chunks real search ranked."""

    sha256: str
    searches: Mapping[tuple[str, int], tuple[fixtures.FixtureChunk, ...]]


def parse(data: bytes, corpus: RetrievalDataset) -> Snapshot:
    """Read snapshot bytes. Everything is checked; anything else refuses."""
    try:
        document = canon.loads(data)
        canon.check(document, SNAPSHOT_SPEC)
    except canon.CanonicalError as exc:
        raise RetrievalSnapshotError(
            f"the snapshot is not canonical or not its type: {exc}"
        ) from exc
    if (document["snapshot"], document["source"]) != (SNAPSHOT_ID, "postgresql"):
        raise RetrievalSnapshotError("this is not the copilot retrieval snapshot")
    expected_corpus = {
        "dataset": {"identity": corpus.identity, "sha256": v2.DATASET_SHA256},
        "documents": rp.plan(corpus),
    }
    if document["corpus"] != expected_corpus:
        raise RetrievalSnapshotError("the snapshot was taken of another corpus")
    chunks = fixture_chunks(corpus)
    documents = {item.doc_id: item for item in corpus.documents}
    searches: dict[tuple[str, int], tuple[fixtures.FixtureChunk, ...]] = {}
    for entry in document["searches"]:
        key = (entry["query"], entry["limit"])
        where = f"{key[0]!r} (limit {key[1]})"
        if key in searches:
            raise RetrievalSnapshotError(f"{where} is recorded twice")
        if len(entry["results"]) > key[1]:
            raise RetrievalSnapshotError(f"{where}: more results than the limit")
        served: list[fixtures.FixtureChunk] = []
        for row in entry["results"]:
            item = documents.get(row["doc_id"])
            if item is None or not _in_scope(item):
                raise RetrievalSnapshotError(f"{where} leaks {row['doc_id']}")
            chunk = chunks[row["doc_id"]]
            if row["ordinal"] != chunk.ordinal:
                raise RetrievalSnapshotError(f"{where}: {row['doc_id']} has no such chunk")
            identity = rp.served_sha256(
                row["doc_id"], chunk.ordinal, chunk.title, chunk.version, item.language, chunk.text
            )
            if row["served_sha256"] != identity:
                raise RetrievalSnapshotError(f"{where}: {row['doc_id']} is not the fixture chunk")
            if chunk in served:
                raise RetrievalSnapshotError(f"{where} serves {row['doc_id']} twice")
            served.append(chunk)
        searches[key] = tuple(served)
    if sorted(searches) != list(copilot_searches()):
        raise RetrievalSnapshotError("the snapshot does not hold exactly the copilot's searches")
    return Snapshot(sha256=hashlib.sha256(data).hexdigest(), searches=searches)


def pinned_bytes() -> bytes:
    """The committed snapshot, refused unless its bytes hash to `SNAPSHOT_SHA256`."""
    data = SNAPSHOT_PATH.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != SNAPSHOT_SHA256:
        raise RetrievalSnapshotError(
            f"snapshot SHA-256 {actual} is not the pinned {SNAPSHOT_SHA256}"
        )
    return data


def load() -> Snapshot:
    """The pinned snapshot, against the pinned dataset's copilot corpus."""
    return parse(pinned_bytes(), copilot_corpus(v2.load()))


def regeneration_requested(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether to rewrite the snapshot from live search. Only "1" asks; never in CI, where gate 3
    must compare, so asking there refuses."""
    if environ.get(REGENERATE_VARIABLE) != "1":
        return False
    if environ.get("CI") == "true":
        raise RetrievalSnapshotError("the snapshot is never regenerated in CI; gate 3 compares it")
    return True


def differences(pinned: Mapping[str, Any], live: Mapping[str, Any]) -> list[str]:
    """What differs between two snapshot documents, for gate 3's failure message."""
    found = [key for key in ("snapshot", "source", "corpus", "engine") if pinned[key] != live[key]]
    pinned_searches = {(s["query"], s["limit"]): s["results"] for s in pinned["searches"]}
    live_searches = {(s["query"], s["limit"]): s["results"] for s in live["searches"]}
    for key in sorted(set(pinned_searches) | set(live_searches)):
        if pinned_searches.get(key) != live_searches.get(key):
            found.append(f"search {key[0]!r} (limit {key[1]})")
    return found


# --- the opt-in stand-in ------------------------------------------------------------------------


class SnapshotKnowledge:
    """Stands where `KnowledgeService` does, as `FixtureKnowledge` does, serving the snapshot.

    The copilot's tool layer turns any exception a service raises into a failed tool result, so a
    refused search could pass unseen as "the search failed". Every refusal is therefore also kept
    in `misses`, and the harness refuses the run when any is there (`knowledge_harness`).
    """

    def __init__(self, snapshot: Snapshot | None = None) -> None:
        self.snapshot = snapshot if snapshot is not None else load()
        self.misses: list[tuple[str, str, int]] = []

    def search(self, hotel_public_id: uuid.UUID, query: str, limit: int) -> KnowledgeSearchResponse:
        if hotel_public_id != EVAL_HOTEL:
            self.misses.append((str(hotel_public_id), query, limit))
            raise SnapshotMissError(
                f"the snapshot holds the evaluated hotel only, not {hotel_public_id}"
            )
        chunks = self.snapshot.searches.get((query, limit))
        if chunks is None:
            self.misses.append((str(hotel_public_id), query, limit))
            raise SnapshotMissError(f"the snapshot does not hold {query!r} with limit {limit}")
        return KnowledgeSearchResponse(
            query=query,
            limit=limit,
            results=[
                KnowledgeSearchResult(
                    chunk_public_id=chunk.chunk_public_id,
                    document_public_id=chunk.document_public_id,
                    title=chunk.title,
                    version=chunk.version,
                    language="english",
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                )
                for chunk in chunks
            ],
        )


__all__ = [
    "FIXTURE_MODULE",
    "REGENERATE_VARIABLE",
    "REPLAYS_FILE",
    "REPLAYS_SHA256",
    "SNAPSHOT_ID",
    "SNAPSHOT_PATH",
    "SNAPSHOT_SHA256",
    "SNAPSHOT_SPEC",
    "SOURCE_LABEL",
    "RetrievalSnapshotError",
    "Snapshot",
    "SnapshotKnowledge",
    "SnapshotMissError",
    "build",
    "capture_searches",
    "copilot_corpus",
    "copilot_searches",
    "differences",
    "fixture_chunks",
    "load",
    "parse",
    "pinned_bytes",
    "regeneration_requested",
    "replay_searches",
]
