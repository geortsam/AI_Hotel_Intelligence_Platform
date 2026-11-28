"""Offline stand-ins for the retrieval fingerprint's tests: a fake PostgreSQL, an ingestion, a repo.

- `FakeDatabase` answers exactly the probes' SQL (`retrieval_probes`, `retrieval_artifacts`) from
  a small catalog and from a simulated ingestion of the dataset, so the probes, the state
  comparison and the measurement path run without a server. Tests change one answer to prove the
  matching refusal.
- `ingest` simulates the harness's plan: uploads in dataset order, new versions through the
  supersede route, withdrawals; it returns the `UploadRecord` the API would have answered.
- `make_repository` builds a throwaway Git repository holding copies of every fingerprinted
  source and the signed C.3 files, committed clean, so `run_measurement` and `verify` run against
  real Git without touching this repository.

Not a fingerprinted source: it is test scaffolding and never part of a measurement.
"""

# ruff: noqa: RUF001 - the Greek stop words are deliberate.

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.knowledge.chunking import checksum
from tests.evaluation import retrieval_artifacts as ra
from tests.evaluation import retrieval_fingerprint as fp
from tests.evaluation import retrieval_probes as rp
from tests.evaluation import retrieval_review as rr
from tests.evaluation import retrieval_signoff as so
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_schema import RetrievalDataset

ROOT = v2.REPOSITORY_ROOT
VERSION = "PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc"
DIGEST = "sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280"
IMAGE = f"postgres:18.6@{DIGEST}"
IMAGE_ID = "sha256:" + "ab" * 32
SHAREDIR = "/usr/share/postgresql/18"
STOPWORDS = {"english": b"a\nan\nthe\n", "greek": "και\nτο\n".encode()}

# Catalog oids of the fake server.
ENGLISH, GREEK, PARSER = 13001, 13002, 3722
ENGLISH_STEM, GREEK_STEM, SIMPLE = 14001, 14002, 3765
SNOWBALL, SIMPLE_TEMPLATE = 15001, 3727


def public_id(*parts: object) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join(str(p) for p in parts)))


@dataclass
class Ingested:
    """What the simulated ingestion left in the two tables."""

    documents: list[list[Any]]
    chunks: list[list[Any]]
    uploads: rp.UploadRecord


def ingest(dataset: RetrievalDataset) -> Ingested:
    """The plan, simulated: one row per document in creation order, one per paragraph."""
    hotels = {1: "evaluated", 2: "neighbour"}
    role_ids = {role: hotel_id for hotel_id, role in hotels.items()}
    by_id = {document.doc_id: document for document in dataset.documents}
    row_of: dict[str, int] = {}
    version_of: dict[str, int] = {}
    documents: list[list[Any]] = []
    chunks: list[list[Any]] = []
    document_ids: dict[str, str] = {}
    chunk_ids: dict[str, tuple[str, int]] = {}
    for doc_id in rp.plan(dataset):
        document = by_id[doc_id]
        row = len(documents) + 1
        row_of[doc_id] = row
        predecessor = document.supersedes
        version_of[doc_id] = 1 if predecessor is None else version_of[predecessor] + 1
        public = public_id("document", doc_id)
        document_ids[public] = doc_id
        documents.append(
            [
                row,
                public,
                role_ids[document.hotel],
                document.status,
                document.language,
                document.title,
                version_of[doc_id],
                checksum(document.content),
                None if predecessor is None else row_of[predecessor],
                len(document.paragraphs),
            ]
        )
        for ordinal, paragraph in enumerate(document.paragraphs):
            chunk_public = public_id("chunk", doc_id, ordinal)
            chunk_ids[chunk_public] = (doc_id, ordinal)
            chunks.append(
                [chunk_public, row, ordinal, paragraph, f"'{doc_id}':{ordinal + 1}", True]
            )
    return Ingested(documents, chunks, rp.UploadRecord(hotels, document_ids, chunk_ids))


def _table(table: str) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "columns": [
            ("id", "bigint", True, "a", "", []),
            ("public_id", "uuid", True, "", "", ["gen_random_uuid()"]),
            ("title" if table == "hotel_documents" else "text", "text", True, "", "", []),
        ],
        "constraints": [(f"{table}_pkey", "p", "PRIMARY KEY (id)")],
        "indexes": [(f"{table}_pkey", f"CREATE UNIQUE INDEX {table}_pkey ON {table} (id)")],
        "triggers": [],
    }


#: The harness hotels' public ids, as the API would return them, and the rows they name.
HOTEL_PUBLIC_IDS: dict[str, str] = {
    "evaluated": "6b1f4c1e-0000-4000-8000-000000000001",
    "neighbour": "6b1f4c1e-0000-4000-8000-000000000002",
}
HOTEL_PUBLIC_IDS_TO_ROWS: dict[str, int] = {
    HOTEL_PUBLIC_IDS["evaluated"]: 1,
    HOTEL_PUBLIC_IDS["neighbour"]: 2,
}


@dataclass
class FakeDatabase:
    """Answers the probes' SQL. `overrides` replaces one statement's answer."""

    dataset: RetrievalDataset
    ingested: Ingested
    overrides: dict[str, Callable[[Mapping[str, Any]], Sequence[tuple[Any, ...]]]] = field(
        default_factory=dict
    )
    calls: list[str] = field(default_factory=list)

    def __call__(self, sql: str, params: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        self.calls.append(sql)
        if sql in self.overrides:
            return [tuple(row) for row in self.overrides[sql](params)]
        return [tuple(row) for row in self.answer(sql, params)]

    def answer(self, sql: str, params: Mapping[str, Any]) -> Sequence[Sequence[Any]]:
        if sql == rp.HOTEL_ID_SQL:
            return [(HOTEL_PUBLIC_IDS_TO_ROWS[params["public_id"]],)]
        if sql == rp.SERVER_SQL:
            return [(180006, VERSION)]
        if sql == rp.EXTENSIONS_SQL:
            return [("btree_gist", "1.8"), ("plpgsql", "1.0")]
        if sql == rp.DATABASE_SQL:
            # The official Debian image: libc, so PostgreSQL leaves datlocale NULL (§12.2).
            return [("UTF8", "UTF8", "c", "en_US.utf8", "en_US.utf8", None, "2.41")]
        if sql == rp.SEARCH_PATH_SQL:
            return [("pg_catalog",), ("public",)]
        if sql == rp.CONFIGURATION_SQL:
            oid = {"english": ENGLISH, "greek": GREEK}[params["name"]]
            functions = (
                "prsd_start",
                "prsd_nexttoken",
                "prsd_end",
                "prsd_headline",
                "prsd_lextype",
            )
            return [(oid, "pg_catalog", PARSER, "default", "pg_catalog", *functions)]
        if sql == rp.TOKEN_TYPES_SQL:
            return [(1, "asciiword", "Word, all ASCII"), (2, "word", "Word, all letters"),
                    (3, "numword", "Word, letters and digits")]  # fmt: skip
        if sql == rp.MAPPINGS_SQL:
            stem = {ENGLISH: ENGLISH_STEM, GREEK: GREEK_STEM}[params["config"]]
            return [(1, 1, stem), (2, 1, stem), (3, 1, SIMPLE)]
        if sql == rp.DICTIONARY_SQL:
            return [
                {
                    ENGLISH_STEM: ("english_stem", "pg_catalog", SNOWBALL,
                                   "language = 'english', stopwords = 'english'"),
                    GREEK_STEM: ("greek_stem", "pg_catalog", SNOWBALL,
                                 "language = 'greek', stopwords = 'greek'"),
                    # PostgreSQL's own bootstrap row: NULL init options (§12.2).
                    SIMPLE: ("simple", "pg_catalog", SIMPLE_TEMPLATE, None),
                }[params["dictionary"]]
            ]  # fmt: skip
        if sql == rp.TEMPLATE_SQL:
            return [
                {
                    SNOWBALL: ("snowball", "pg_catalog", "dsnowball_init", "dsnowball_lexize"),
                    SIMPLE_TEMPLATE: ("simple", "pg_catalog", "dsimple_init", "dsimple_lexize"),
                }[params["template"]]
            ]
        if sql == rp.SHAREDIR_SQL:
            return [(SHAREDIR,)]
        if sql == rp.STOPWORD_FILE_SQL:
            name = params["path"].rsplit("/", 1)[1].removesuffix(".stop")
            return [(STOPWORDS[name],)]
        if sql == rp.TSQUERY_SQL:
            words = [w.lower() for w in params["query"].split() if w.isalpha()]
            return [(" & ".join(f"'{w}'" for w in words),)]
        if sql == rp.ALEMBIC_SQL:
            return [("0020_guest_email_rules",)]
        for key, statement in (
            ("columns", rp.COLUMNS_SQL),
            ("constraints", rp.CONSTRAINTS_SQL),
            ("indexes", rp.INDEXES_SQL),
            ("triggers", rp.TRIGGERS_SQL),
        ):
            if sql == statement:
                return _table(params["table"])[key]
        if sql == rp.DOCUMENTS_SQL:
            return self.ingested.documents
        if sql == rp.CHUNKS_SQL:
            return self.ingested.chunks
        if sql == ra.SETTINGS_SQL:
            return [("search_path", '"$user", public', "default")]
        raise AssertionError(f"unexpected SQL: {sql}")


def database(dataset: RetrievalDataset) -> FakeDatabase:
    return FakeDatabase(dataset, ingest(dataset))


def search(dataset: RetrievalDataset, ingested: Ingested) -> Callable[[str, int], dict[str, Any]]:
    """A search that returns each query's judged chunks, best grade first, within its limit."""
    queries: dict[tuple[str, int], Any] = {}
    for query in dataset.queries:
        queries.setdefault((query.query, query.limit), query)
    documents = {row[0]: row for row in ingested.documents}
    chunk_rows = {
        (ingested.uploads.documents[documents[c[1]][1]], c[2]): c for c in ingested.chunks
    }

    def run(text: str, limit: int) -> dict[str, Any]:
        query = queries[(text, limit)]
        judged = sorted(query.relevant, key=lambda j: (-j.grade, j.doc_id, j.ordinal))
        results = []
        for judgment in judged[:limit]:
            chunk = chunk_rows[(judgment.doc_id, judgment.ordinal)]
            document = documents[chunk[1]]
            results.append(
                {
                    "chunk_public_id": chunk[0],
                    "document_public_id": document[1],
                    "title": document[5],
                    "version": document[6],
                    "language": document[4],
                    "ordinal": chunk[2],
                    "text": chunk[3],
                }
            )
        return {"query": text, "limit": limit, "results": results}

    return run


def docker_metadata(**changes: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "container": {"Id": "c" * 64, "Image": IMAGE_ID},
        "image": {
            "Id": IMAGE_ID,
            "RepoDigests": [f"postgres@{DIGEST}"],
            "Os": "linux",
            "Architecture": "amd64",
        },
        "info": {"Driver": "overlay2", "DriverStatus": [["Backing Filesystem", "extfs"]]},
    }
    for path, value in changes.items():
        block, key = path.split("__")
        if value is None:
            metadata[block].pop(key, None)
        else:
            metadata[block][key] = value
    return metadata


# --- a throwaway repository -----------------------------------------------------------------------

#: Everything a measurement reads at the commit: the sources, the signed C.3 files, and the
#: excluded sources, which must exist there too (§12.3).
REPOSITORY_FILES = (
    *(path for path in fp.SOURCE_FILES if path != fp.INTEGRATION_TEST),
    *fp.EXCLUDED_SOURCES,
    rr.SHEET_FILE,
    rr.FORM_FILE,
    rr.SIGNOFF_FILE,
    so.DECISION_RECORD_FILE,
)
INTEGRATION_STUB = b'"""The C.4 integration test (a stand-in for the fingerprint tests)."""\n'


def git(repository: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, check=True, text=True
    )
    return done.stdout.strip()


def make_repository(target: Path, *, integration_test: bool = True) -> Path:
    """Copy every file a measurement reads into a new repository and commit it, clean."""
    target.mkdir(parents=True)
    git(target, "init", "-q")
    for key, value in (
        ("user.name", "fingerprint test"),
        ("user.email", "fingerprint@example.test"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        git(target, "config", key, value)
    for path in REPOSITORY_FILES:
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination)
    for source in (ROOT / fp.MIGRATIONS_DIR).glob("*.py"):
        destination = target / fp.MIGRATIONS_DIR / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    if integration_test:
        stub = target / fp.INTEGRATION_TEST
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_bytes(INTEGRATION_STUB)
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "fingerprint test fixture")
    # Re-check out under the copied .gitattributes, so the tree is clean by Git's own rules.
    git(target, "rm", "-q", "-r", "--cached", ".")
    git(target, "reset", "-q", "--hard")
    return target


def environ(**extra: str) -> dict[str, str]:
    return {fp.IMAGE_VARIABLE: IMAGE, "PYTHONHASHSEED": "0", **extra}


def installed_from_lock() -> dict[str, str]:
    """The installed set the dev lock prescribes for this interpreter (the CI install)."""
    lock = (ROOT / ra.DEV_LOCK).read_bytes().decode("utf-8")
    return fp.applicable(fp.parse_lock(lock, "dev lock"), "dev lock")


def write_docker(directory: Path, metadata: Mapping[str, Any] | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ra.DOCKER_FILE).write_text(
        json.dumps(docker_metadata() if metadata is None else metadata), encoding="utf-8"
    )


def harness(dataset: RetrievalDataset, db: FakeDatabase | None = None) -> ra.Harness:
    db = database(dataset) if db is None else db
    ingested = db.ingested
    return ra.Harness(
        fetch=db, ingest=lambda _d: ingested.uploads, search=search(dataset, ingested)
    )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
