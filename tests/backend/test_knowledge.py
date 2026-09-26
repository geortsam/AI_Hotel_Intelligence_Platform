"""Stage 7.9 — knowledge documents without a database: chunking, contracts and structure.

`tests/integration/test_knowledge_api.py` proves the behaviour against real PostgreSQL. This
module proves what can be proved without one: that chunking and checksums are deterministic, that
the contracts are exactly what the design document says, and -- by compiling the real statement --
that the search query applies the hotel and the status in SQL, in the same WHERE clause as the
text match.
"""

from __future__ import annotations

import ast
import inspect
import re
import typing
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Table
from sqlalchemy.dialects import postgresql

from app.api.v1.endpoints import knowledge as endpoints
from app.core.errors import ValidationError
from app.knowledge import chunking as chunking_module
from app.knowledge.chunking import MAX_CHUNK_WORDS, checksum, chunk_text, normalise
from app.models.enums import (
    AuditAction,
    AuditResourceType,
    DocumentLanguage,
    DocumentStatus,
)
from app.models.knowledge import HotelDocument, HotelDocumentChunk
from app.repositories import knowledge as repository_module
from app.repositories.knowledge import KnowledgeRepository
from app.schemas.knowledge import (
    MAX_SEARCH_RESULTS,
    DocumentCreate,
    DocumentDetail,
    KnowledgeSearchResult,
    LanguageName,
    StatusName,
)
from app.services import knowledge as service_module

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP = REPOSITORY_ROOT / "backend" / "app"
MIGRATION = (
    REPOSITORY_ROOT / "database" / "migrations" / "versions" / "20260926_0014_hotel_documents.py"
)
DOCUMENTS = cast(Table, HotelDocument.__table__)
CHUNKS = cast(Table, HotelDocumentChunk.__table__)


def body(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "Pool policy",
        "language": "english",
        "content": "The pool opens at 07:00.",
        "contains_no_guest_personal_data": True,
        **overrides,
    }


# ======================================================================================
# Chunking and checksums
# ======================================================================================


def test_paragraphs_become_ordered_chunks() -> None:
    drafts = chunk_text("First paragraph here.\n\nSecond one.\n\n\n\nThird.")
    assert [(d.ordinal, d.text, d.token_count) for d in drafts] == [
        (0, "First paragraph here.", 3),
        (1, "Second one.", 2),
        (2, "Third.", 1),
    ]


def test_a_long_paragraph_is_split_at_the_word_cap() -> None:
    words = [f"w{n}" for n in range(MAX_CHUNK_WORDS * 2 + 5)]
    drafts = chunk_text(" ".join(words))
    assert [d.token_count for d in drafts] == [MAX_CHUNK_WORDS, MAX_CHUNK_WORDS, 5]
    assert drafts[1].text.split()[0] == f"w{MAX_CHUNK_WORDS}"


def test_chunking_is_deterministic_and_whitespace_insensitive() -> None:
    a = chunk_text("Line one\nline two.\n\nNext   paragraph.  ")
    b = chunk_text("Line one\r\nline two.\r\n\r\nNext paragraph.")
    assert a == b
    assert a[0].text == "Line one line two."


def test_the_checksum_ignores_line_endings_and_trailing_space_only() -> None:
    assert checksum("a\r\nb  \n") == checksum("a\nb")
    assert checksum("a\nb") != checksum("a\n\nb")
    assert len(checksum("x")) == 64
    assert normalise("  x  \r\n") == "x"


@pytest.mark.parametrize("content", ["", "   \n\n  ", "x" * 5000])
def test_content_that_cannot_be_chunked_is_a_validation_error(content: str) -> None:
    with pytest.raises(ValidationError):
        chunk_text(content)


def test_too_many_chunks_are_refused() -> None:
    with pytest.raises(ValidationError, match="more than"):
        chunk_text("\n\n".join("word" for _ in range(chunking_module.MAX_CHUNKS + 1)))


def test_chunking_is_pure() -> None:
    """Text in, text out: no session, no repository, no service, no hotel."""
    tree = ast.parse(Path(chunking_module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported == {"__future__", "dataclasses", "app.core.errors", "app.models.knowledge"}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not {name for name in names if "hotel" in name.lower() or "session" in name.lower()}


def test_the_service_delegates_text_processing() -> None:
    """The service orchestrates; it neither chunks nor hashes text itself."""
    source = Path(service_module.__file__).read_text(encoding="utf-8")
    assert "hashlib" not in source
    assert ".join(" not in source
    assert service_module.chunk_text is chunk_text
    assert service_module.checksum is checksum


# ======================================================================================
# The HTTP contract
# ======================================================================================


def test_the_upload_accepts_exactly_these_fields() -> None:
    assert set(DocumentCreate.model_fields) == {
        "title",
        "source",
        "language",
        "content",
        "contains_no_guest_personal_data",
    }
    assert DocumentCreate.model_config.get("extra") == "forbid"


@pytest.mark.parametrize(
    "overrides",
    [
        {"contains_no_guest_personal_data": False},
        {"contains_no_guest_personal_data": "yes"},
        {"language": "klingon"},
        {"title": "x" * 201},
        {"content": ""},
        {"hotel_id": 1},
        {"hotel_public_id": str(uuid.uuid4())},
        {"status": "active"},
        {"version": 7},
    ],
)
def test_a_malformed_upload_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(PydanticValidationError):
        DocumentCreate.model_validate(body(**overrides))


def test_the_attestation_is_required() -> None:
    raw = body()
    del raw["contains_no_guest_personal_data"]
    with pytest.raises(PydanticValidationError):
        DocumentCreate.model_validate(raw)


def test_the_schema_vocabularies_match_the_database_vocabularies() -> None:
    assert set(typing.get_args(LanguageName)) == set(DocumentLanguage.values())
    assert set(typing.get_args(StatusName)) == set(DocumentStatus.values())


def test_a_search_result_carries_what_a_citation_needs() -> None:
    """Stage 7.10 cites chunk and document public ids, title and version (§6.6)."""
    assert set(KnowledgeSearchResult.model_fields) == {
        "chunk_public_id",
        "document_public_id",
        "title",
        "version",
        "language",
        "ordinal",
        "text",
    }
    assert MAX_SEARCH_RESULTS == 20


def test_no_knowledge_response_carries_a_float() -> None:
    """The relevance score is ordered on, never published: comparable only within one search."""
    for model in (KnowledgeSearchResult, DocumentDetail):
        for name, field in model.model_fields.items():
            assert field.annotation is not float, f"{model.__name__}.{name}"


def test_no_knowledge_response_names_an_internal_key() -> None:
    for model in (DocumentDetail, KnowledgeSearchResult):
        for name in model.model_fields:
            assert (
                name == "public_id"
                or name.endswith("_public_id")
                or not (name == "id" or name.endswith("_id"))
            ), (model.__name__, name)


# ======================================================================================
# The retrieval query, compiled
# ======================================================================================


class CapturingSession:
    """Records the statement a repository method builds, and returns no rows."""

    def __init__(self) -> None:
        self.statement: Any = None

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> Any:
        self.statement = statement

        class Empty:
            def all(self) -> list[Any]:
                return []

        return Empty()


def compiled_search_sql() -> str:
    session = CapturingSession()
    KnowledgeRepository(cast(Any, session)).search(42, "pool", 5)
    return str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_the_search_filters_hotel_and_status_in_the_where_clause() -> None:
    """Architecture §6.3: the tenant filter is IN the query, beside the match -- not after it."""
    sql = compiled_search_sql()
    where = sql[sql.index("WHERE") : sql.index("ORDER BY")]

    assert "hotel_documents.hotel_id = 42" in where
    assert "hotel_documents.status = 'active'" in where
    assert "@@ websearch_to_tsquery(CAST(hotel_documents.language AS REGCONFIG), 'pool')" in where
    assert "LIMIT 5" in sql


def test_the_search_order_is_total() -> None:
    """Relevance first, then document, then position: equal scores can never reshuffle."""
    sql = compiled_search_sql()
    order = sql[sql.index("ORDER BY") : sql.index("LIMIT")].strip()
    assert order.startswith(
        "ORDER BY ts_rank_cd(hotel_document_chunks.search_vector, websearch_to_tsquery("
    )
    assert order.endswith("DESC, hotel_documents.id ASC, hotel_document_chunks.ordinal ASC"), order


def test_the_search_selects_no_score() -> None:
    """The score is in ORDER BY only: nothing between SELECT and FROM computes it."""
    sql = compiled_search_sql()
    selected = sql[: sql.index("FROM hotel_document_chunks")]
    assert selected.startswith("SELECT ")
    assert "ts_rank_cd" not in selected


def test_every_read_is_bounded_by_a_hotel() -> None:
    """No repository method reads a document, a chunk or a result without a hotel_id."""
    for name, method in inspect.getmembers(KnowledgeRepository, inspect.isfunction):
        if name.startswith("_") or name.startswith("add_"):
            continue
        parameters = list(inspect.signature(method).parameters)
        assert parameters[1] == "hotel_id", name


def test_the_repository_filters_nothing_in_python() -> None:
    """Retrieve-globally-then-filter would show up as a comprehension over rows with a test."""
    tree = ast.parse(inspect.getsource(repository_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.comprehension):
            assert not node.ifs, "a Python-side filter over query results"


# ======================================================================================
# Structure: roles, vocabularies, the migration, and what 7.9 does not add
# ======================================================================================


def required_role(route: Any) -> str | None:
    for dependency in route.dependencies:
        call = dependency.dependency
        if getattr(call, "__qualname__", "").startswith("require_role."):
            cells = [cell.cell_contents for cell in (call.__closure__ or ())]
            return str(next(cell for cell in cells if hasattr(cell, "value")).value)
    return None


def test_every_write_requires_a_manager_and_every_read_a_member() -> None:
    routes = [cast(APIRoute, route) for route in endpoints.router.routes]
    roles = {(sorted(route.methods or ())[0], route.path): required_role(route) for route in routes}
    prefix = "/hotels/{hotel_public_id}"
    assert roles == {
        ("POST", f"{prefix}/documents"): "manager",
        ("GET", f"{prefix}/documents"): None,
        ("GET", f"{prefix}/documents/{{document_public_id}}"): None,
        ("POST", f"{prefix}/documents/{{document_public_id}}/versions"): "manager",
        ("POST", f"{prefix}/documents/{{document_public_id}}/withdrawal"): "manager",
        ("GET", f"{prefix}/knowledge/search"): None,
    }


def test_the_audit_vocabulary_names_the_three_document_writes() -> None:
    assert {
        AuditAction.DOCUMENT_CREATED.value,
        AuditAction.DOCUMENT_VERSION_CREATED.value,
        AuditAction.DOCUMENT_WITHDRAWN.value,
    } == {"document.created", "document.version_created", "document.withdrawn"}
    assert AuditResourceType.DOCUMENT.value == "document"


def test_the_migration_declares_every_model_constraint_and_the_triggers() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    for table in (DOCUMENTS, CHUNKS):
        for constraint in table.constraints:
            assert str(constraint.name) in migration, constraint.name
        for index in table.indexes:
            assert str(index.name) in migration, index.name
    for trigger in [
        "trg_hotel_documents_set_updated_at",
        "trg_hotel_documents_guard",
        "trg_hotel_document_chunks_append_only",
    ]:
        assert f"CREATE TRIGGER {trigger}" in migration
    assert "USING gin (search_vector)" in migration


def test_the_migration_alters_only_the_two_audit_vocabularies() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    upgrade = migration[migration.index("def upgrade") : migration.index("def downgrade")]
    altered = {line.split()[2] for line in upgrade.splitlines() if "ALTER TABLE" in line}
    assert altered <= {"audit_events"}
    assert "trg_audit_events_append_only" not in upgrade
    assert upgrade.count("CREATE TABLE") == 2


def test_stage_7_9_adds_no_vector_embedding_or_model_machinery() -> None:
    sources = [
        APP / "models" / "knowledge.py",
        APP / "repositories" / "knowledge.py",
        APP / "services" / "knowledge.py",
        APP / "schemas" / "knowledge.py",
        APP / "api" / "v1" / "endpoints" / "knowledge.py",
        MIGRATION,
    ]
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        code = ast.unparse(tree).lower()
        for word in ["pgvector", "embedding", "hnsw", "ivfflat"]:
            assert word not in code, (path.name, word)
        # A vector COLUMN type; `to_tsvector(` is full-text search and is expected.
        assert not re.search(r"(?<![a-z_])vector\(", code), path.name
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("app.llm", "app.copilot")), path.name


def test_the_knowledge_tool_arrived_with_stage_7_10_over_this_service() -> None:
    """Stage 7.9 built the service and registered no tool; Stage 7.10 registered the sixth, which
    delegates to this service's `search` rather than to a second implementation."""
    from app.copilot.registry import build_default_registry

    registry = build_default_registry()
    assert "search_hotel_knowledge" in registry
    assert len(registry.names()) == 7  # Stage 7.12 added `get_hotel_priorities`
    contract = registry.get("search_hotel_knowledge").contract
    assert contract.delegates_to == "KnowledgeService.search"
