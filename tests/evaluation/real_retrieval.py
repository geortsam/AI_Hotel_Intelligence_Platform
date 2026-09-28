"""Measure recall@5 on an operator-supplied corpus of real hotel documents (Stage 7.15's evidence).

    python -m tests.evaluation.real_retrieval --spec PATH --database-url URL [--set-sha256 HEX]
    python -m tests.evaluation.real_retrieval --list-chunks FILE

Stage 7.15 (pgvector) is conditional on one measurement, declared in `retrieval.py` and
docs/copilot-evaluation.md §8.2 before anything was measured:

    recall@5 below 0.90 on a corpus of real hotel documents, with queries of the kind the
    copilot sends.

`knowledge_retrieval_v1` cannot supply it: every document and query in it was written by the
developer. This command measures a **different, operator-supplied set** the same way -- the same
cutoff, the same threshold, the same real upload and search routes, real PostgreSQL -- and says
whether the result is qualifying evidence. It does not implement Stage 7.15, and it changes
nothing in the application.

## What the operator supplies, outside the repository

A JSON specification (see docs/copilot-evaluation.md §8.4) naming the set, its documents -- plain
UTF-8 text files -- and its queries, each with the chunk(s) a correct search should return, plus
the SHA-256 of every document's normalised text, of every expected chunk's text, of the corpus
and of the query set. Nothing here repairs, guesses or skips: any mismatch refuses the run.

## What is trusted, and what is not

The harness cannot tell a real hotel's documents from invented ones, or a copilot-like query
from any other. The specification therefore carries an **attestation**, and the verdict is
TRIGGER CONDITION SATISFIED / NOT SATISFIED only when it attests both; otherwise the measurement
is reported and the verdict is NOT ASSESSABLE. Permission to use the documents and the absence of
guest personal data are attested too, and the run refuses without them -- the upload route
requires the second in any case.

## Isolation

The database must pass `tests/db_safety.py` (a disposable `*_test` database), and is migrated from
empty. One fresh hotel is created; before any upload it must hold no document, and every search
result must be a chunk this run uploaded, of a document this run uploaded. Documents and the
specification must live outside the repository, which keeps them out of Git and out of every
image (`.dockerignore` excludes `tests/` and the harness with it).

## Scoring

Independent of the search: a query is recalled when at least one of its expected chunks is among
the first `cutoff` results; recall is recalled / scored. Every query must name at least one
expected chunk, so every query is scored. The comparison with the threshold is exact (integer
arithmetic), not a rounded float.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.evaluation.retrieval import CUTOFF, KNOWLEDGE_RETRIEVAL_V1, PGVECTOR_RECALL_THRESHOLD

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FROZEN_SET_ID = KNOWLEDGE_RETRIEVAL_V1.set_id
LANGUAGES = frozenset({"simple", "english", "greek", "french", "german", "italian", "spanish"})
ATTESTED = (
    "real_hotel_documents",
    "copilot_like_queries",
    "permission_to_use",
    "contains_no_guest_personal_data",
)
#: A qualifying run needs both of these; the other two are required for any run.
QUALIFYING = ("real_hotel_documents", "copilot_like_queries")

SATISFIED = "TRIGGER CONDITION SATISFIED — EVIDENCE SUPPORTS PROCEEDING TO STAGE 7.15"
NOT_SATISFIED = "TRIGGER CONDITION NOT SATISFIED"
NOT_ASSESSABLE = (
    "NOT ASSESSABLE — the set is not attested as real hotel documents with copilot-like queries"
)


class RefusedError(RuntimeError):
    """The run cannot proceed as specified. Nothing is repaired or skipped."""


# --- canonical forms and digests -----------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalise(content: str) -> str:
    """The upload route's normalisation (docs: CRLF to LF, each line's trailing whitespace
    trimmed, the whole trimmed), restated here so the harness can hash a file without the app.
    The route's own `content_checksum` is compared with this after upload."""
    return "\n".join(line.rstrip() for line in content.replace("\r\n", "\n").split("\n")).strip()


# --- the specification ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecDocument:
    document_id: str
    title: str
    language: str
    source: str | None
    path: Path
    text_sha256: str


@dataclass(frozen=True)
class Expected:
    document_id: str
    ordinal: int
    chunk_sha256: str


@dataclass(frozen=True)
class SpecQuery:
    query_id: str
    query: str
    expected: tuple[Expected, ...]


@dataclass(frozen=True)
class Spec:
    set_id: str
    version: str
    cutoff: int
    threshold: float
    documents: tuple[SpecDocument, ...]
    queries: tuple[SpecQuery, ...]
    corpus_sha256: str
    query_set_sha256: str
    attestation: Mapping[str, bool]
    set_sha256: str

    @property
    def identity(self) -> str:
        return f"{self.set_id}_{self.version}"

    @property
    def qualifying(self) -> bool:
        return all(self.attestation[key] for key in QUALIFYING)


def _field(block: Mapping[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in block:
        raise RefusedError(f"{where}: `{key}` is missing")
    value = block[key]
    if (kind is int and isinstance(value, bool)) or not isinstance(value, kind):
        raise RefusedError(f"{where}: `{key}` must be {kind.__name__}")
    if isinstance(value, str) and not value.strip():
        raise RefusedError(f"{where}: `{key}` is empty")
    return value


def _digest(block: Mapping[str, Any], key: str, where: str) -> str:
    value = _field(block, key, str, where)
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise RefusedError(f"{where}: `{key}` must be a lower-case SHA-256")
    return str(value)


def corpus_digest(documents: Sequence[Mapping[str, Any]]) -> str:
    """Over what identifies each document, in document-id order: never over its text."""
    listing = sorted(
        (
            {
                "document_id": d["document_id"],
                "title": d["title"],
                "language": d["language"],
                "source": d.get("source"),
                "text_sha256": d["text_sha256"],
            }
            for d in documents
        ),
        key=lambda d: str(d["document_id"]),
    )
    return sha256_text(canonical_json(listing))


def query_set_digest(queries: Sequence[Mapping[str, Any]]) -> str:
    listing = sorted(
        (
            {"query_id": q["query_id"], "query": q["query"], "expected": q["expected"]}
            for q in queries
        ),
        key=lambda q: str(q["query_id"]),
    )
    return sha256_text(canonical_json(listing))


def inside_repository(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == REPOSITORY_ROOT or REPOSITORY_ROOT in resolved.parents


def load_spec(path: Path) -> Spec:
    """Parse and verify a specification. Every document is read and hashed here."""
    if not path.is_file():
        raise RefusedError(f"specification {path} does not exist")
    if inside_repository(path):
        raise RefusedError("the specification must live outside the repository")
    raw = path.read_bytes()
    try:
        block = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as problem:
        raise RefusedError(f"specification is not valid UTF-8 JSON: {problem}") from problem
    if not isinstance(block, dict):
        raise RefusedError("specification must be a JSON object")

    set_id = _field(block, "set_id", str, "spec")
    version = _field(block, "version", str, "spec")
    if set_id == FROZEN_SET_ID:
        raise RefusedError(
            f"`{FROZEN_SET_ID}` is the frozen developer-written set; use a new set_id"
        )
    cutoff = _field(block, "cutoff", int, "spec")
    threshold = _field(block, "threshold", float, "spec")
    if cutoff != CUTOFF:
        raise RefusedError(f"cutoff must be {CUTOFF}, the declared working cut-off; got {cutoff}")
    if threshold != PGVECTOR_RECALL_THRESHOLD:
        raise RefusedError(f"threshold must be {PGVECTOR_RECALL_THRESHOLD}; got {threshold}")

    attestation_block = _field(block, "attestation", dict, "spec")
    attestation: dict[str, bool] = {}
    for key in ATTESTED:
        attestation[key] = bool(_field(attestation_block, key, bool, "attestation"))
    for key in ("permission_to_use", "contains_no_guest_personal_data"):
        if not attestation[key]:
            raise RefusedError(f"attestation `{key}` must be true to run at all")

    documents = _field(block, "documents", list, "spec")
    queries = _field(block, "queries", list, "spec")
    if not documents or not queries:
        raise RefusedError("the set needs at least one document and one query")

    parsed_documents = _documents(documents, path.parent)
    parsed_queries = _queries(queries, {d.document_id for d in parsed_documents})

    declared_corpus = _digest(block, "corpus_sha256", "spec")
    declared_queries = _digest(block, "query_set_sha256", "spec")
    if corpus_digest(documents) != declared_corpus:
        raise RefusedError("the corpus does not match its declared corpus_sha256")
    if query_set_digest(queries) != declared_queries:
        raise RefusedError("the queries do not match the declared query_set_sha256")

    _refuse_the_frozen_corpus(parsed_documents)
    return Spec(
        set_id=set_id,
        version=version,
        cutoff=cutoff,
        threshold=threshold,
        documents=parsed_documents,
        queries=parsed_queries,
        corpus_sha256=declared_corpus,
        query_set_sha256=declared_queries,
        attestation=attestation,
        set_sha256=sha256_text(canonical_json(block)),
    )


def _documents(entries: Sequence[Any], base: Path) -> tuple[SpecDocument, ...]:
    parsed: list[SpecDocument] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"documents[{index}]"
        if not isinstance(entry, dict):
            raise RefusedError(f"{where} must be an object")
        document_id = _field(entry, "document_id", str, where)
        if document_id in seen:
            raise RefusedError(f"{where}: duplicate document_id {document_id!r}")
        seen.add(document_id)
        title = _field(entry, "title", str, where)
        language = _field(entry, "language", str, where)
        if language not in LANGUAGES:
            raise RefusedError(f"{where}: language must be one of {sorted(LANGUAGES)}")
        source = entry.get("source")
        if source is not None and not (isinstance(source, str) and source.strip()):
            raise RefusedError(f"{where}: `source` must be a non-empty string when present")
        file = Path(_field(entry, "file", str, where))
        path = file if file.is_absolute() else base / file
        if inside_repository(path):
            raise RefusedError(f"{where}: documents must live outside the repository")
        if not path.is_file():
            raise RefusedError(f"{where}: {path} does not exist")
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as problem:
            raise RefusedError(f"{where}: cannot be read as UTF-8 text: {problem}") from problem
        declared = _digest(entry, "text_sha256", where)
        if sha256_text(normalise(text)) != declared:
            raise RefusedError(f"{where}: its normalised text does not match text_sha256")
        parsed.append(SpecDocument(document_id, title, language, source, path, declared))
    return tuple(parsed)


def _queries(entries: Sequence[Any], document_ids: set[str]) -> tuple[SpecQuery, ...]:
    parsed: list[SpecQuery] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"queries[{index}]"
        if not isinstance(entry, dict):
            raise RefusedError(f"{where} must be an object")
        query_id = _field(entry, "query_id", str, where)
        if query_id in seen:
            raise RefusedError(f"{where}: duplicate query_id {query_id!r}")
        seen.add(query_id)
        query = _field(entry, "query", str, where)
        expected_entries = _field(entry, "expected", list, where)
        if not expected_entries:
            raise RefusedError(f"{where}: every query must name at least one expected chunk")
        expected: list[Expected] = []
        for position, item in enumerate(expected_entries):
            at = f"{where}.expected[{position}]"
            if not (isinstance(item, list) and len(item) == 3):
                raise RefusedError(f"{at} must be [document_id, ordinal, chunk_sha256]")
            document_id, ordinal, chunk_sha = item
            if document_id not in document_ids:
                raise RefusedError(f"{at}: names no document of this set")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise RefusedError(f"{at}: ordinal must be a non-negative integer")
            _digest({"chunk_sha256": chunk_sha}, "chunk_sha256", at)
            expected.append(Expected(document_id, ordinal, chunk_sha))
        parsed.append(SpecQuery(query_id, query, tuple(expected)))
    return tuple(parsed)


def _refuse_the_frozen_corpus(documents: Sequence[SpecDocument]) -> None:
    frozen = {sha256_text(normalise(d.content)) for d in KNOWLEDGE_RETRIEVAL_V1.documents}
    reused = [d.document_id for d in documents if d.text_sha256 in frozen]
    if reused:
        raise RefusedError(
            f"documents {reused} are knowledge_retrieval_v1's developer-written text"
        )


# --- checks on what the application stored and returned ------------------------------------------


@dataclass(frozen=True)
class StoredChunk:
    document_id: str
    document_public_id: str
    chunk_public_id: str
    ordinal: int
    text_sha256: str


def verify_expected_chunks(spec: Spec, stored: Sequence[StoredChunk]) -> None:
    by_position = {(c.document_id, c.ordinal): c for c in stored}
    for query in spec.queries:
        for item in query.expected:
            chunk = by_position.get((item.document_id, item.ordinal))
            if chunk is None:
                raise RefusedError(
                    f"query {query.query_id}: document {item.document_id} has no chunk "
                    f"{item.ordinal} after ingestion"
                )
            if chunk.text_sha256 != item.chunk_sha256:
                raise RefusedError(
                    f"query {query.query_id}: chunk {item.ordinal} of {item.document_id} does "
                    "not match its chunk_sha256"
                )


def locate_results(
    results: Sequence[Mapping[str, Any]], stored: Sequence[StoredChunk]
) -> list[tuple[str, int]]:
    """Map one search's results to (document_id, ordinal). A result this run did not upload is
    another hotel's or another run's, and refuses the measurement. A known chunk must also be
    reported under the document it was uploaded in, which is necessarily one of this run's."""
    by_chunk = {c.chunk_public_id: c for c in stored}
    located: list[tuple[str, int]] = []
    for result in results:
        chunk = by_chunk.get(str(result.get("chunk_public_id")))
        if chunk is None:
            raise RefusedError("a search result is not a chunk this run uploaded")
        if chunk.document_public_id != str(result.get("document_public_id")):
            raise RefusedError("a search result names a chunk under the wrong document")
        located.append((chunk.document_id, chunk.ordinal))
    return located


# --- scoring -------------------------------------------------------------------------------------


def recalled(expected: Sequence[Expected], results: Sequence[tuple[str, int]], cutoff: int) -> bool:
    top = set(results[:cutoff])
    return any((item.document_id, item.ordinal) in top for item in expected)


def below_threshold(hits: int, scored: int) -> bool:
    """hits / scored < 0.90, exactly: 10 * hits < 9 * scored."""
    return 10 * hits < 9 * scored


def report(
    spec: Spec, stored: Sequence[StoredChunk], outcomes: Mapping[str, bool]
) -> dict[str, Any]:
    """Text-free: identities, digests, counts and the verdict. No document, query or chunk text."""
    if set(outcomes) != {q.query_id for q in spec.queries}:
        raise RefusedError("not every query was measured")
    hits, scored = sum(outcomes.values()), len(outcomes)
    below = below_threshold(hits, scored)
    relation = "below" if below else ("equal to" if 10 * hits == 9 * scored else "above")
    verdict = (SATISFIED if below else NOT_SATISFIED) if spec.qualifying else NOT_ASSESSABLE
    return {
        "set": {"identity": spec.identity, "sha256": spec.set_sha256},
        "corpus_sha256": spec.corpus_sha256,
        "query_set_sha256": spec.query_set_sha256,
        "documents": len(spec.documents),
        "chunks": len(stored),
        "queries": scored,
        "cutoff": spec.cutoff,
        "threshold": spec.threshold,
        "recall": {"recalled": hits, "scored": scored, "value": round(hits / scored, 4)},
        "relation_to_threshold": relation,
        "attestation": dict(spec.attestation),
        "qualifying_evidence": spec.qualifying,
        "verdict": verdict,
        "per_query": {query_id: outcomes[query_id] for query_id in sorted(outcomes)},
    }


# --- the run against real PostgreSQL -------------------------------------------------------------

Client = Any  # fastapi.testclient.TestClient; imported lazily so parsing needs no application


def ingest(client: Client, base: str, spec: Spec) -> list[StoredChunk]:
    listing = client.get(f"{base}/documents")
    if listing.status_code != 200 or listing.json()["items"]:
        raise RefusedError("the evaluation hotel is not empty before ingestion")
    stored: list[StoredChunk] = []
    for document in spec.documents:
        body: dict[str, Any] = {
            "title": document.title,
            "language": document.language,
            "content": document.path.read_bytes().decode("utf-8"),
            "contains_no_guest_personal_data": True,
        }
        if document.source is not None:
            body["source"] = document.source
        uploaded = client.post(f"{base}/documents", json=body)
        if uploaded.status_code != 201:
            code = uploaded.json().get("error", {}).get("code", "?")
            raise RefusedError(
                f"document {document.document_id} was refused by the upload route "
                f"({uploaded.status_code} {code})"
            )
        detail = uploaded.json()
        if detail["content_checksum"] != document.text_sha256:
            raise RefusedError(f"document {document.document_id}: the stored checksum differs")
        stored.extend(
            StoredChunk(
                document.document_id,
                str(detail["public_id"]),
                str(chunk["public_id"]),
                int(chunk["ordinal"]),
                sha256_text(chunk["text"]),
            )
            for chunk in detail["chunks"]
        )
    return stored


def measure(client: Client, base: str, spec: Spec) -> dict[str, Any]:
    stored = ingest(client, base, spec)
    verify_expected_chunks(spec, stored)
    outcomes: dict[str, bool] = {}
    for query in spec.queries:
        response = client.get(
            f"{base}/knowledge/search", params={"q": query.query, "limit": spec.cutoff}
        )
        if response.status_code != 200:
            code = response.json().get("error", {}).get("code", "?")
            raise RefusedError(
                f"query {query.query_id}: search failed ({response.status_code} {code})"
            )
        located = locate_results(response.json()["results"], stored)
        outcomes[query.query_id] = recalled(query.expected, located, spec.cutoff)
    return report(spec, stored, outcomes)


def run(spec: Spec, database_url: str) -> dict[str, Any]:
    """Migrate a disposable database from empty, make one hotel, measure. Test-only helpers are
    imported here, so parsing and --list-chunks need neither the application nor a database."""
    import os

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from tests.db_safety import UnsafeTestDatabaseError, assert_safe_destructive_target

    try:
        assert_safe_destructive_target(database_url)
    except UnsafeTestDatabaseError as refused:
        if "could not be inspected" in str(refused):
            raise RefusedError(
                "PostgreSQL is unavailable or unreadable at --database-url"
            ) from refused
        raise RefusedError(f"not a disposable test database: {refused}") from refused

    os.environ["TEST_DATABASE_URL"] = database_url
    from tests.integration.conftest import (
        create_test_app,
        grant_membership,
        make_hotel,
        register_and_login,
    )

    cfg = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPOSITORY_ROOT / "database" / "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(database_url, future=True, poolclass=sa.pool.NullPool)
    try:
        with Session(engine) as session:
            hotel = make_hotel(session, slug=f"real-retrieval-{uuid.uuid4().hex[:8]}")
            session.commit()
            hotel_public_id = str(hotel.public_id)
        app = create_test_app(engine, auth_login_rate_limit=100)
        email = f"real-retrieval-{uuid.uuid4().hex[:8]}@example.test"
        token = register_and_login(TestClient(app), email)
        grant_membership(engine, email, hotel_public_id, "manager")
        client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        return measure(client, f"/api/v1/hotels/{hotel_public_id}", spec)
    finally:
        engine.dispose()


# --- helping an operator write a specification ---------------------------------------------------


def list_chunks(path: Path) -> list[tuple[int, int, str]]:
    """(ordinal, words, sha256) of each chunk the upload route will store. Uses the route's own
    chunker, so the ordinals are the ones ingestion produces. Prints no text."""
    from app.knowledge.chunking import chunk_text

    text = path.read_bytes().decode("utf-8")
    return [(c.ordinal, c.token_count, sha256_text(c.text)) for c in chunk_text(text)]


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[[Spec, str], dict[str, Any]] = run,
) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--spec", type=Path, help="the evaluation specification (outside the repository)"
    )
    parser.add_argument(
        "--database-url", help="a disposable PostgreSQL database (scripts/testdb.py create)"
    )
    parser.add_argument("--set-sha256", help="refuse unless the specification hashes to this")
    parser.add_argument(
        "--list-chunks",
        type=Path,
        metavar="FILE",
        help="print a document's chunk ordinals and hashes",
    )
    args = parser.parse_args(argv)

    try:
        if args.list_chunks is not None:
            normalised = sha256_text(normalise(args.list_chunks.read_bytes().decode("utf-8")))
            print(f"text_sha256 {normalised}")
            for ordinal, words, digest in list_chunks(args.list_chunks):
                print(f"chunk {ordinal}: {words} words, sha256 {digest}")
            return 0
        if args.spec is None or not args.database_url:
            raise RefusedError("--spec and --database-url are both required")
        spec = load_spec(args.spec)
        if args.set_sha256 is not None and args.set_sha256 != spec.set_sha256:
            raise RefusedError("the specification does not hash to --set-sha256")
        result = runner(spec, args.database_url)
    except (RefusedError, OSError, UnicodeDecodeError) as refused:
        print(f"real_retrieval REFUSED: {refused}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
