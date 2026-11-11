"""`knowledge_retrieval_v2`: the immutable dataset snapshot and its integrity checks (R2.10C.3b).

`docs/retrieval-evaluation-decision.md` is the source of truth. This module loads the snapshot, and
holds the checks that tie it to what it was copied from. It runs no search and opens no database.

## The snapshot (decision C)

`datasets/knowledge_retrieval_v2.json` is the dataset in schema v2 (`retrieval_schema`). It is
written once and never regenerated: `load()` refuses it unless its canonical SHA-256 equals
`DATASET_SHA256`, so any edit is a new version, not a change to this one. The file itself is the
canonical mapping with sorted keys and two-space indentation (`render`); a test holds it to that
form so it cannot drift into a hand-edited layout either.

**The judgments are the developer's, reviewed by you in C.3c and not yet signed off.** Every
grade, rationale and family was written in C.3b from the copied text; the C.3c review accepted
them with two revisions, and its adjudication made five more (R1-R5, decision record §11),
which made this version. They stay unsigned until C.3d's sign-off (§3.6); nothing here, and no
test, stands in for it.

## Copied documents and drift (decision C)

Every document records the module and symbol its text was copied from. `source_drift` re-reads
each source and lists every difference -- title, language, status, hotel, paragraphs, and the
text's SHA-256 -- so a later edit to `retrieval.py` or `fixture_documents.py` fails a test instead
of reaching the snapshot. The copy is never updated from the source.

## The R2.9Y queries (decision D, F4 Option A)

`captured_queries` reads the hash-checked capture -- hashed in its committed LF form, so a CRLF
checkout is the same capture -- and groups every `search_hotel_knowledge` call
by (case, query, limit), keeping its (run, attempt) occurrences in capture order. A second identical
call in the same run and attempt cannot be represented without a `call_index` (F4 Option A), so it
is refused rather than merged. `capture_mismatches` compares the dataset's `model_captured` queries
with that extraction and lists every difference: a query missing, extra or duplicated, and any
difference of id, text, case, limit, capture file or SHA-256, or occurrence.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from tests.evaluation.retrieval_schema import (
    CorpusDocument,
    DatasetError,
    DocumentSource,
    RetrievalDataset,
    content_sha256,
    parse_dataset_json,
)

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]

DATASET_PATH: Final = HERE / "datasets" / "knowledge_retrieval_v2.json"
#: The canonical SHA-256 of `knowledge_retrieval_v2`. Pinned; changing it is a new version.
DATASET_SHA256: Final = "7f35b8a473c73f6d318e48ce5cc2667294f0d50816c511cfaf7851585c617031"

#: The R2.9Y capture, as every model-captured query's provenance names it.
CAPTURE_FILE: Final = "tests/evaluation/fixtures/r29y_knowledge_capture.json"
CAPTURE_SHA256: Final = "6cefba9463356889f3e21471a03c1e807a6386ee1b0894ccbe9dc87901755cd1"
SEARCH_TOOL: Final = "search_hotel_knowledge"


class SnapshotError(ValueError):
    """The snapshot, or the capture it is checked against, is not the one pinned."""


# --- the snapshot ---------------------------------------------------------------------------------


def render(dataset: RetrievalDataset) -> str:
    """The snapshot file's exact text: sorted keys, two-space indent, UTF-8, a final newline."""
    return json.dumps(dataset.as_mapping(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load(path: Path = DATASET_PATH, expected_sha256: str = DATASET_SHA256) -> RetrievalDataset:
    """The snapshot, refused unless its canonical SHA-256 is the pinned one."""
    dataset = parse_dataset_json(path.read_text(encoding="utf-8"))
    actual = dataset.checksum()
    if actual != expected_sha256:
        raise SnapshotError(f"snapshot checksum {actual} is not the pinned {expected_sha256}")
    return dataset


# --- the capture ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapturedQuery:
    """One distinct (case, query, limit) of the capture and every (run, attempt) it was sent in."""

    case: str
    query: str
    limit: int
    #: In capture order: runs as the capture lists them, attempts as each run lists them.
    occurrences: tuple[tuple[str, int], ...]


def read_capture(
    path: Path | None = None, expected_sha256: str = CAPTURE_SHA256
) -> Mapping[str, Any]:
    """The capture, refused unless its bytes hash to the pinned SHA-256."""
    raw = (path or REPOSITORY_ROOT / CAPTURE_FILE).read_bytes()
    # The pinned digest is of the committed LF form. A Windows checkout may turn LF into CRLF
    # (`text=auto`), so the bytes are hashed as LF -- the convention test_expectations_v3_v2.py
    # pins this same file by. JSON escapes a carriage return inside a string, so nothing is lost.
    content = raw.replace(b"\r\n", b"\n")
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected_sha256:
        raise SnapshotError(f"capture SHA-256 {actual} is not the pinned {expected_sha256}")
    capture = json.loads(content.decode("utf-8"))
    if not isinstance(capture, Mapping):
        raise SnapshotError("the capture is not a JSON object")
    return capture


def _search_calls(node: object) -> Iterator[Mapping[str, Any]]:
    if isinstance(node, Mapping):
        if node.get("name") == SEARCH_TOOL and isinstance(node.get("arguments"), Mapping):
            yield node["arguments"]
        for value in node.values():
            yield from _search_calls(value)
    elif isinstance(node, list):
        for value in node:
            yield from _search_calls(value)


def captured_queries(capture: Mapping[str, Any]) -> tuple[CapturedQuery, ...]:
    """Every distinct (case, query, limit) searched in the capture, in order of first occurrence."""
    groups: dict[tuple[str, str, int], list[tuple[str, int]]] = {}
    for run, block in capture["runs"].items():
        for attempt, recorded in block["attempts"].items():
            if not str(attempt).isdigit():
                raise SnapshotError(f"run {run!r}: attempt {attempt!r} is not a number")
            for case, turns in recorded["turns"].items():
                for arguments in _search_calls(turns):
                    query, limit = arguments.get("query"), arguments.get("limit")
                    if not isinstance(query, str) or isinstance(limit, bool):
                        raise SnapshotError(f"{case} in {run}#{attempt}: malformed search call")
                    if not isinstance(limit, int):
                        raise SnapshotError(f"{case} in {run}#{attempt}: limit is not an integer")
                    occurrence = (run, int(attempt))
                    seen = groups.setdefault((case, query, limit), [])
                    if occurrence in seen:
                        raise SnapshotError(
                            f"{case} sent {query!r} twice in {run}#{attempt}: a (run, attempt) "
                            "occurrence cannot record a repeat without a call_index (F4 Option A)"
                        )
                    seen.append(occurrence)
    return tuple(
        CapturedQuery(case, query, limit, tuple(occurrences))
        for (case, query, limit), occurrences in groups.items()
    )


def captured_query_id(captured: Sequence[CapturedQuery], item: CapturedQuery) -> str:
    """`r29y.<case>.<n>`: n counts the case's distinct queries in order of first occurrence."""
    same_case = [entry for entry in captured if entry.case == item.case]
    return f"r29y.{item.case}.{same_case.index(item) + 1}"


def capture_mismatches(
    dataset: RetrievalDataset, captured: Sequence[CapturedQuery], capture_sha256: str
) -> list[str]:
    """Every difference between the dataset's model-captured queries and the capture."""
    problems: list[str] = []
    expected = {captured_query_id(captured, item): item for item in captured}
    actual = [query for query in dataset.queries if query.origin == "model_captured"]
    actual_ids = [query.query_id for query in actual]
    for query_id in sorted(set(expected) - set(actual_ids)):
        problems.append(f"{query_id}: in the capture, missing from the dataset")
    for query_id in sorted(set(actual_ids) - set(expected)):
        problems.append(f"{query_id}: in the dataset, not in the capture")
    texts = [(query.provenance.case if query.provenance else "", query.query) for query in actual]
    for case_and_text in sorted({entry for entry in texts if texts.count(entry) > 1}):
        problems.append(f"{case_and_text}: recorded by more than one dataset query")
    for query in actual:
        item = expected.get(query.query_id)
        provenance = query.provenance
        if item is None or provenance is None:
            continue
        where = query.query_id
        if query.query != item.query:
            problems.append(f"{where}: query {query.query!r}, captured {item.query!r}")
        if provenance.case != item.case:
            problems.append(f"{where}: case {provenance.case!r}, captured {item.case!r}")
        if query.limit != item.limit or provenance.limit != item.limit:
            problems.append(f"{where}: limit {query.limit}, captured {item.limit}")
        if provenance.capture_file != CAPTURE_FILE:
            problems.append(f"{where}: capture file {provenance.capture_file!r}")
        if provenance.capture_sha256 != capture_sha256:
            problems.append(f"{where}: capture SHA-256 {provenance.capture_sha256}")
        recorded = sorted((o.run, o.attempt) for o in provenance.occurrences)
        if recorded != sorted(item.occurrences):
            problems.append(f"{where}: occurrences {recorded}, captured {sorted(item.occurrences)}")
    return problems


# --- copied documents and drift -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """What a copied document's source says, read back from the module it was copied from."""

    title: str
    language: str
    status: str
    hotel: str
    paragraphs: tuple[str, ...]


_INDEXED = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\[(?P<index>[0-9]+)\]")
_SOURCE_MODULES: Final = {
    "tests/evaluation/retrieval.py": "tests.evaluation.retrieval",
    "tests/evaluation/fixture_documents.py": "tests.evaluation.fixture_documents",
}


def source_document(source: DocumentSource) -> SourceDocument:
    """Read the document a `source` names. Only the two copied modules are known sources."""
    module_name = _SOURCE_MODULES.get(source.module)
    match = _INDEXED.fullmatch(source.symbol)
    if module_name is None or match is None:
        raise SnapshotError(f"unknown source {source.module}:{source.symbol}")
    module = importlib.import_module(module_name)
    item = getattr(module, match["name"])[int(match["index"])]
    if module_name.endswith(".retrieval"):
        return SourceDocument(item.title, item.language, "active", "evaluated", item.paragraphs)
    from tests.evaluation.fixture_documents import NEIGHBOUR_HOTEL

    hotel = "neighbour" if item.hotel == NEIGHBOUR_HOTEL else "evaluated"
    # Every fixture document is English: FixtureKnowledge serves each with language "english".
    return SourceDocument(item.title, "english", item.status, hotel, (item.text,))


def source_drift(documents: Sequence[CorpusDocument]) -> list[str]:
    """Every difference between each copied document and the source it names."""
    problems: list[str] = []
    for document in documents:
        where = f"{document.doc_id} ({document.source.module}:{document.source.symbol})"
        try:
            source = source_document(document.source)
        except (SnapshotError, AttributeError, IndexError, ImportError) as missing:
            problems.append(f"{where}: source cannot be read: {missing}")
            continue
        for field in ("title", "language", "status", "hotel", "paragraphs"):
            if getattr(document, field) != getattr(source, field):
                problems.append(f"{where}: {field} differs from its source")
        if document.source.text_sha256 != content_sha256(source.paragraphs):
            problems.append(f"{where}: text_sha256 does not match the source text")
    return problems


__all__ = [
    "CAPTURE_FILE",
    "CAPTURE_SHA256",
    "DATASET_PATH",
    "DATASET_SHA256",
    "CapturedQuery",
    "DatasetError",
    "SnapshotError",
    "SourceDocument",
    "capture_mismatches",
    "captured_queries",
    "captured_query_id",
    "load",
    "read_capture",
    "render",
    "source_document",
    "source_drift",
]
