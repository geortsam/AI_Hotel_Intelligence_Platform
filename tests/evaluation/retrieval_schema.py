"""The schema of a `knowledge_retrieval` evaluation dataset, v2 onward (R2.10C.2).

`docs/retrieval-evaluation-decision.md` is the source of truth for what a dataset holds; this
module is its schema, its validation and its checksum. It holds **no dataset**: Dataset v2 is
written and judged in C.3. It reads no database, runs no search and imports nothing that does.

## What a dataset is

    schema     SCHEMA_VERSION, so a dataset written for another schema is refused
    set_id     e.g. "knowledge_retrieval"         version  e.g. "v2"
    cutoff     5, the working cut-off (`retrieval.CUTOFF`)
    documents  the corpus, in upload order: doc_id, hotel, status, language, title, paragraphs
    queries    in declared order: query_id, query, language, family, origin, provenance,
               answerable, relevant, forbidden, rationale

A chunk is named by `(doc_id, ordinal)`, and its ordinal is its paragraph's index. Whether the real
chunker produces exactly those ordinals is C.3's offline check, not this module's.

## Rules, each enforced on construction and on loading

- **Nothing is repaired, guessed or dropped.** An unknown field, a missing field, a wrong type, a
  `true` where an integer belongs, a duplicate JSON key, a malformed identifier or digest: each is a
  `DatasetError`.
- **Grades** are 0 (not relevant), 1 (partial, same topic, or another language) and 2 (the chunk
  alone states the fact the question needs). An unjudged chunk is grade 0.
- **Answerable** means at least one grade-2 chunk; a no-answer query has none (it may have grade 1).
  A query of family `no_answer` is never answerable. Every metric keeps the two apart.
- **Scope.** A judged chunk belongs to an active document of the evaluated hotel: search can
  return nothing else. A forbidden chunk belongs to an inactive version or to the neighbour hotel,
  and a chunk is never both judged and forbidden. `forbidden` is an annotation: the scope traps a
  query was written to spring. It need not list every out-of-scope chunk, because scope leakage is
  decided from each document's `hotel` and `status` (`retrieval_metrics.leaked`), not from it.
- **Provenance.** A `model_captured` query carries its run, case, prompt and the SHA-256 of the
  capture file it was copied from; any other origin carries none.

## Canonical form and checksum

`canonical_json()` is the dataset as JSON with sorted keys, compact separators and UTF-8 text, and
`checksum()` is the SHA-256 of it. Documents and queries keep their declared order: document order
is upload order, which decides the search's tie-break. A query's `relevant` and `forbidden` lists
are sets, so they are stored sorted by `(doc_id, ordinal)` and their authoring order changes
nothing. Text is kept exactly as written; no Unicode normalisation is applied.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, get_args

from app.schemas.knowledge import LanguageName
from tests.evaluation.retrieval import CUTOFF

SCHEMA_VERSION: Final = "knowledge_retrieval_schema_v1"

GRADES: Final = (0, 1, 2)
#: The grade at which one chunk answers the question on its own (decision record §3.5).
SUFFICIENT: Final = 2

#: Decision record §3.3.
FAMILIES: Final = (
    "shared_words",
    "paraphrase",
    "multi_idea",
    "cross_language",
    "title_only",
    "no_answer",
    "scope_trap",
)
ORIGINS: Final = ("developer", "model_captured", "operator")
HOTELS: Final = ("evaluated", "neighbour")
STATUSES: Final = ("active", "superseded", "withdrawn")
#: The upload route's languages: the same list, not a copy of it.
LANGUAGES: Final = frozenset(get_args(LanguageName))

_SET_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VERSION = re.compile(r"v[1-9][0-9]{0,5}")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class DatasetError(ValueError):
    """The dataset is malformed or inconsistent. Nothing is repaired, guessed or dropped."""


# --- field checks, shared by construction and loading ---------------------------------------------


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{where} must be a non-empty string")
    return value


def _identifier(value: object, pattern: re.Pattern[str], where: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise DatasetError(f"{where} is not a valid identifier: {value!r}")
    return value


def _integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatasetError(f"{where} must be an integer")
    return value


def _ordinal(value: object, where: str) -> int:
    ordinal = _integer(value, where)
    if ordinal < 0:
        raise DatasetError(f"{where} must not be negative")
    return ordinal


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise DatasetError(f"{where} must be true or false")
    return value


def _choice(value: object, choices: Sequence[str] | frozenset[str], where: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise DatasetError(f"{where} must be one of {sorted(choices)}, not {value!r}")
    return value


def _grade(value: object, where: str) -> int:
    grade = _integer(value, where)
    if grade not in GRADES:
        raise DatasetError(f"{where} must be one of {list(GRADES)}, not {grade}")
    return grade


def _object(
    value: object, where: str, required: Sequence[str], optional: Sequence[str] = ()
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DatasetError(f"{where} must be an object")
    unknown = sorted(str(key) for key in value if key not in (*required, *optional))
    if unknown:
        raise DatasetError(f"{where}: unknown field(s) {unknown}")
    missing = [key for key in required if key not in value]
    if missing:
        raise DatasetError(f"{where}: missing field(s) {missing}")
    return value


def _array(value: object, where: str) -> Sequence[Any]:
    if not isinstance(value, list | tuple):
        raise DatasetError(f"{where} must be an array")
    return value


# --- the schema ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class ChunkRef:
    """One chunk: its document and its ordinal (the paragraph's index)."""

    doc_id: str
    ordinal: int

    def __post_init__(self) -> None:
        _identifier(self.doc_id, _ID, "chunk doc_id")
        _ordinal(self.ordinal, "chunk ordinal")

    def as_dict(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "ordinal": self.ordinal}


@dataclass(frozen=True, slots=True)
class Judgment:
    """A chunk's relevance to one query: grade 0, 1 or 2."""

    doc_id: str
    ordinal: int
    grade: int

    def __post_init__(self) -> None:
        _identifier(self.doc_id, _ID, "judgment doc_id")
        _ordinal(self.ordinal, "judgment ordinal")
        _grade(self.grade, "judgment grade")

    @property
    def ref(self) -> ChunkRef:
        return ChunkRef(self.doc_id, self.ordinal)

    def as_dict(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "ordinal": self.ordinal, "grade": self.grade}


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a model-captured query was copied from, verbatim."""

    run: str
    case: str
    prompt: str
    capture_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.run, _ID, "provenance run")
        _identifier(self.case, _ID, "provenance case")
        _identifier(self.prompt, _ID, "provenance prompt")
        _identifier(self.capture_sha256, _SHA256, "provenance capture_sha256")

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "case": self.case,
            "prompt": self.prompt,
            "capture_sha256": self.capture_sha256,
        }


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    doc_id: str
    hotel: str
    status: str
    language: str
    title: str
    #: One per chunk: the chunk's ordinal is its index here.
    paragraphs: tuple[str, ...]

    def __post_init__(self) -> None:
        where = f"document {self.doc_id!r}"
        _identifier(self.doc_id, _ID, "document doc_id")
        _choice(self.hotel, HOTELS, f"{where} hotel")
        _choice(self.status, STATUSES, f"{where} status")
        _choice(self.language, LANGUAGES, f"{where} language")
        _text(self.title, f"{where} title")
        if not isinstance(self.paragraphs, tuple) or not self.paragraphs:
            raise DatasetError(f"{where} needs at least one paragraph, as a tuple")
        for index, paragraph in enumerate(self.paragraphs):
            _text(paragraph, f"{where} paragraph {index}")

    @property
    def in_scope(self) -> bool:
        """Whether search at the evaluated hotel may return this document's chunks."""
        return self.hotel == "evaluated" and self.status == "active"

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "hotel": self.hotel,
            "status": self.status,
            "language": self.language,
            "title": self.title,
            "paragraphs": list(self.paragraphs),
        }


@dataclass(frozen=True, slots=True)
class DatasetQuery:
    query_id: str
    query: str
    language: str
    family: str
    origin: str
    provenance: Provenance | None
    answerable: bool
    relevant: tuple[Judgment, ...]
    forbidden: tuple[ChunkRef, ...]
    rationale: str

    def __post_init__(self) -> None:
        where = f"query {self.query_id!r}"
        _identifier(self.query_id, _ID, "query_id")
        _text(self.query, f"{where} query")
        _choice(self.language, LANGUAGES, f"{where} language")
        _choice(self.family, FAMILIES, f"{where} family")
        _choice(self.origin, ORIGINS, f"{where} origin")
        _boolean(self.answerable, f"{where} answerable")
        _text(self.rationale, f"{where} rationale")
        if (self.origin == "model_captured") != (self.provenance is not None):
            raise DatasetError(f"{where}: provenance is required for model_captured, and only then")
        if self.provenance is not None and not isinstance(self.provenance, Provenance):
            raise DatasetError(f"{where}: provenance must be a Provenance")
        if not isinstance(self.relevant, tuple) or not isinstance(self.forbidden, tuple):
            raise DatasetError(f"{where}: relevant and forbidden must be tuples")
        if not all(isinstance(item, Judgment) for item in self.relevant):
            raise DatasetError(f"{where}: relevant must hold judgments")
        if not all(isinstance(item, ChunkRef) for item in self.forbidden):
            raise DatasetError(f"{where}: forbidden must hold chunk references")
        judged = [item.ref for item in self.relevant]
        if len(set(judged)) != len(judged):
            raise DatasetError(f"{where}: a chunk is judged more than once")
        if len(set(self.forbidden)) != len(self.forbidden):
            raise DatasetError(f"{where}: a chunk is forbidden more than once")
        if set(judged) & set(self.forbidden):
            raise DatasetError(f"{where}: a chunk is both judged and forbidden")
        sufficient = any(item.grade == SUFFICIENT for item in self.relevant)
        if self.answerable and not sufficient:
            raise DatasetError(f"{where}: an answerable query needs a grade-2 chunk")
        if not self.answerable and sufficient:
            raise DatasetError(f"{where}: a no-answer query cannot have a grade-2 chunk")
        if self.family == "no_answer" and self.answerable:
            raise DatasetError(f"{where}: a no_answer query cannot be answerable")
        # Sets, so their authoring order means nothing and the stored order is canonical.
        object.__setattr__(
            self, "relevant", tuple(sorted(self.relevant, key=lambda item: item.ref))
        )
        object.__setattr__(self, "forbidden", tuple(sorted(self.forbidden)))

    def grade_of(self, chunk: ChunkRef) -> int:
        """The judged grade of *chunk* for this query; an unjudged chunk is grade 0."""
        for item in self.relevant:
            if item.ref == chunk:
                return item.grade
        return 0

    @property
    def sufficient(self) -> frozenset[ChunkRef]:
        """The grade-2 chunks: each alone answers the question."""
        return frozenset(item.ref for item in self.relevant if item.grade == SUFFICIENT)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query": self.query,
            "language": self.language,
            "family": self.family,
            "origin": self.origin,
            "provenance": self.provenance.as_dict() if self.provenance is not None else None,
            "answerable": self.answerable,
            "relevant": [item.as_dict() for item in self.relevant],
            "forbidden": [item.as_dict() for item in self.forbidden],
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class RetrievalDataset:
    set_id: str
    version: str
    cutoff: int
    documents: tuple[CorpusDocument, ...]
    queries: tuple[DatasetQuery, ...]

    def __post_init__(self) -> None:
        _identifier(self.set_id, _SET_ID, "set_id")
        _identifier(self.version, _VERSION, "version")
        if _integer(self.cutoff, "cutoff") != CUTOFF:
            raise DatasetError(f"cutoff must be {CUTOFF}, the working cut-off; got {self.cutoff}")
        if not isinstance(self.documents, tuple) or not self.documents:
            raise DatasetError("the dataset needs at least one document, as a tuple")
        if not isinstance(self.queries, tuple) or not self.queries:
            raise DatasetError("the dataset needs at least one query, as a tuple")
        if not all(isinstance(item, CorpusDocument) for item in self.documents):
            raise DatasetError("documents must be CorpusDocument")
        if not all(isinstance(item, DatasetQuery) for item in self.queries):
            raise DatasetError("queries must be DatasetQuery")
        doc_ids = [document.doc_id for document in self.documents]
        if len(set(doc_ids)) != len(doc_ids):
            raise DatasetError("a doc_id is used twice")
        query_ids = [query.query_id for query in self.queries]
        if len(set(query_ids)) != len(query_ids):
            raise DatasetError("a query_id is used twice")
        documents = {document.doc_id: document for document in self.documents}
        for query in self.queries:
            where = f"query {query.query_id!r}"
            for chunk in [item.ref for item in query.relevant]:
                if not self._resolve(documents, chunk, where).in_scope:
                    raise DatasetError(
                        f"{where}: judged chunk {chunk.doc_id}#{chunk.ordinal} is not in scope; "
                        "search can return only the evaluated hotel's active documents"
                    )
            for chunk in query.forbidden:
                if self._resolve(documents, chunk, where).in_scope:
                    raise DatasetError(
                        f"{where}: forbidden chunk {chunk.doc_id}#{chunk.ordinal} is in scope; "
                        "only an inactive version or the neighbour hotel's can be forbidden"
                    )

    @staticmethod
    def _resolve(
        documents: Mapping[str, CorpusDocument], chunk: ChunkRef, where: str
    ) -> CorpusDocument:
        document = documents.get(chunk.doc_id)
        if document is None:
            raise DatasetError(f"{where}: names no document {chunk.doc_id!r}")
        if chunk.ordinal >= len(document.paragraphs):
            raise DatasetError(f"{where}: document {chunk.doc_id!r} has no chunk {chunk.ordinal}")
        return document

    @property
    def identity(self) -> str:
        return f"{self.set_id}_{self.version}"

    def chunks(self) -> frozenset[ChunkRef]:
        """Every chunk of the corpus, in scope or not."""
        return frozenset(
            ChunkRef(document.doc_id, ordinal)
            for document in self.documents
            for ordinal in range(len(document.paragraphs))
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "set_id": self.set_id,
            "version": self.version,
            "cutoff": self.cutoff,
            "documents": [document.as_dict() for document in self.documents],
            "queries": [query.as_dict() for query in self.queries],
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# --- loading -------------------------------------------------------------------------------------

_DATASET_FIELDS = ("schema", "set_id", "version", "cutoff", "documents", "queries")
_DOCUMENT_FIELDS = ("doc_id", "hotel", "status", "language", "title", "paragraphs")
_QUERY_FIELDS = (
    "query_id",
    "query",
    "language",
    "family",
    "origin",
    "provenance",
    "answerable",
    "relevant",
    "forbidden",
    "rationale",
)
_PROVENANCE_FIELDS = ("run", "case", "prompt", "capture_sha256")


def load_dataset(raw: object) -> RetrievalDataset:
    """A dataset from its JSON-shaped form (`as_mapping`'s), validated in full."""
    block = _object(raw, "dataset", _DATASET_FIELDS)
    if block["schema"] != SCHEMA_VERSION:
        raise DatasetError(f"schema must be {SCHEMA_VERSION!r}, not {block['schema']!r}")
    documents = tuple(
        _document(entry, f"documents[{index}]")
        for index, entry in enumerate(_array(block["documents"], "documents"))
    )
    queries = tuple(
        _query(entry, f"queries[{index}]")
        for index, entry in enumerate(_array(block["queries"], "queries"))
    )
    return RetrievalDataset(
        set_id=block["set_id"],
        version=block["version"],
        cutoff=block["cutoff"],
        documents=documents,
        queries=queries,
    )


def _document(raw: object, where: str) -> CorpusDocument:
    block = _object(raw, where, _DOCUMENT_FIELDS)
    return CorpusDocument(
        doc_id=block["doc_id"],
        hotel=block["hotel"],
        status=block["status"],
        language=block["language"],
        title=block["title"],
        paragraphs=tuple(_array(block["paragraphs"], f"{where}.paragraphs")),
    )


def _query(raw: object, where: str) -> DatasetQuery:
    block = _object(raw, where, _QUERY_FIELDS)
    provenance = None
    if block["provenance"] is not None:
        source = _object(block["provenance"], f"{where}.provenance", _PROVENANCE_FIELDS)
        provenance = Provenance(
            run=source["run"],
            case=source["case"],
            prompt=source["prompt"],
            capture_sha256=source["capture_sha256"],
        )
    relevant = []
    for index, item in enumerate(_array(block["relevant"], f"{where}.relevant")):
        judged = _object(item, f"{where}.relevant[{index}]", ("doc_id", "ordinal", "grade"))
        relevant.append(Judgment(judged["doc_id"], judged["ordinal"], judged["grade"]))
    forbidden = []
    for index, item in enumerate(_array(block["forbidden"], f"{where}.forbidden")):
        chunk = _object(item, f"{where}.forbidden[{index}]", ("doc_id", "ordinal"))
        forbidden.append(ChunkRef(chunk["doc_id"], chunk["ordinal"]))
    return DatasetQuery(
        query_id=block["query_id"],
        query=block["query"],
        language=block["language"],
        family=block["family"],
        origin=block["origin"],
        provenance=provenance,
        answerable=block["answerable"],
        relevant=tuple(relevant),
        forbidden=tuple(forbidden),
        rationale=block["rationale"],
    )


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise DatasetError(
            f"duplicate JSON key(s) {duplicated}: one would silently replace another"
        )
    return dict(pairs)


def _no_constant(name: str) -> float:
    raise DatasetError(f"{name} is not a JSON number")


def parse_dataset_json(text: str) -> RetrievalDataset:
    """A dataset from JSON text. Duplicate keys and NaN or Infinity are refused, not resolved."""
    try:
        raw = json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except json.JSONDecodeError as problem:
        raise DatasetError(f"not valid JSON: {problem}") from problem
    return load_dataset(raw)


__all__ = [
    "FAMILIES",
    "GRADES",
    "HOTELS",
    "LANGUAGES",
    "ORIGINS",
    "SCHEMA_VERSION",
    "STATUSES",
    "SUFFICIENT",
    "ChunkRef",
    "CorpusDocument",
    "DatasetError",
    "DatasetQuery",
    "Judgment",
    "Provenance",
    "RetrievalDataset",
    "load_dataset",
    "parse_dataset_json",
]
