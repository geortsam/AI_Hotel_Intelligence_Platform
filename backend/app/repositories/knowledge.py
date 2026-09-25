"""Data access for hotel knowledge documents and their chunks (Stage 7.9).

**Every query here takes a `hotel_id` and applies it in SQL.** There is no method that reads a
document, a chunk or a search result without one, and none that filters by hotel after reading:
`search` puts `hotel_documents.hotel_id = :hotel_id` and `status = 'active'` in the same WHERE
clause as the full-text match, so another hotel's rows are never read, let alone returned. That is
architecture §6.3 ("a WHERE clause the caller cannot influence -- not a post-filter on a global
search"), and an architecture test pins it by reading this module.

The `hotel_id` is always the internal key of a hotel the scope resolver has already resolved for
the caller. Nothing that reaches this module from a request or a model can name one.

## Retrieval, precisely

    matches  : chunk.search_vector @@ websearch_to_tsquery(document.language::regconfig, :query)
    scope    : document.hotel_id = :hotel_id AND document.status = 'active'
    order    : ts_rank_cd(chunk.search_vector, <the same tsquery>) DESC,
               document.id ASC, chunk.ordinal ASC   (a total order: results never reshuffle)
    bound    : LIMIT :limit

The relevance score is ordered on and never selected. It is comparable only within one search,
so it carries no meaning a caller could use beyond the order it already gives -- and a float
never enters this layer, the rule every repository here keeps.

`websearch_to_tsquery` accepts any user text without raising -- quotes, `-exclusions`, `or` --
and each chunk is matched under its own document's language, so an English and a Greek document
are both searchable by the same request. The query string is a bound parameter; it is never
formatted into SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import cast, func, literal, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.orm import Session

from app.knowledge.chunking import ChunkDraft
from app.models.enums import DocumentStatus
from app.models.knowledge import HotelDocument, HotelDocumentChunk


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One matching chunk and the version it belongs to. Internal: the service projects it."""

    chunk: HotelDocumentChunk
    document: HotelDocument


class KnowledgeRepository:
    """Documents, their chunks, and full-text retrieval -- always within one hotel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- writes ------------------------------------------------------------------------------

    def add_document(self, document: HotelDocument) -> HotelDocument:
        self._session.add(document)
        self._session.flush()
        return document

    def add_chunks(self, document: HotelDocument, drafts: Sequence[ChunkDraft]) -> None:
        """Insert the chunks of *document*, computing each search vector in the database.

        `to_tsvector(<language>::regconfig, text)` in the INSERT itself, so the vector is always
        the one PostgreSQL computes for exactly this text under exactly this configuration.
        """
        config = cast(literal(document.language), REGCONFIG)
        for draft in drafts:
            self._session.add(
                HotelDocumentChunk(
                    document_id=document.id,
                    ordinal=draft.ordinal,
                    text=draft.text,
                    token_count=draft.token_count,
                    search_vector=func.to_tsvector(config, draft.text),
                )
            )
        self._session.flush()

    # --- reads, each bounded by hotel ---------------------------------------------------------

    def get(
        self, hotel_id: int, public_id: uuid.UUID, *, for_update: bool = False
    ) -> HotelDocument | None:
        statement = select(HotelDocument).where(
            HotelDocument.hotel_id == hotel_id, HotelDocument.public_id == public_id
        )
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalars(statement).one_or_none()

    def get_by_id(self, hotel_id: int, document_id: int) -> HotelDocument | None:
        return self._session.scalars(
            select(HotelDocument).where(
                HotelDocument.hotel_id == hotel_id, HotelDocument.id == document_id
            )
        ).one_or_none()

    def list_documents(
        self, hotel_id: int, *, status: str | None, offset: int, limit: int
    ) -> tuple[list[HotelDocument], int]:
        conditions = [HotelDocument.hotel_id == hotel_id]
        if status is not None:
            conditions.append(HotelDocument.status == status)
        total = self._session.scalar(
            select(func.count()).select_from(HotelDocument).where(*conditions)
        )
        rows = self._session.scalars(
            select(HotelDocument)
            .where(*conditions)
            .order_by(HotelDocument.created_at.desc(), HotelDocument.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return list(rows), int(total or 0)

    def chunks(self, hotel_id: int, document_id: int) -> list[HotelDocumentChunk]:
        """A version's chunks in order, joined to the document so the hotel bound holds."""
        return list(
            self._session.scalars(
                select(HotelDocumentChunk)
                .join(HotelDocument, HotelDocument.id == HotelDocumentChunk.document_id)
                .where(
                    HotelDocument.hotel_id == hotel_id,
                    HotelDocumentChunk.document_id == document_id,
                )
                .order_by(HotelDocumentChunk.ordinal)
            ).all()
        )

    def chunk_counts(self, hotel_id: int, document_ids: Sequence[int]) -> dict[int, int]:
        if not document_ids:
            return {}
        rows = self._session.execute(
            select(HotelDocumentChunk.document_id, func.count())
            .join(HotelDocument, HotelDocument.id == HotelDocumentChunk.document_id)
            .where(
                HotelDocument.hotel_id == hotel_id,
                HotelDocumentChunk.document_id.in_(document_ids),
            )
            .group_by(HotelDocumentChunk.document_id)
        ).all()
        return {int(document_id): int(count) for document_id, count in rows}

    def search(self, hotel_id: int, query: str, limit: int) -> list[SearchHit]:
        """Rank one hotel's ACTIVE chunks against *query*. See the module docstring."""
        tsquery = func.websearch_to_tsquery(cast(HotelDocument.language, REGCONFIG), query)
        relevance = func.ts_rank_cd(HotelDocumentChunk.search_vector, tsquery)
        rows = self._session.execute(
            select(HotelDocumentChunk, HotelDocument)
            .join(HotelDocument, HotelDocument.id == HotelDocumentChunk.document_id)
            .where(
                HotelDocument.hotel_id == hotel_id,
                HotelDocument.status == DocumentStatus.ACTIVE.value,
                HotelDocumentChunk.search_vector.op("@@")(tsquery),
            )
            .order_by(relevance.desc(), HotelDocument.id.asc(), HotelDocumentChunk.ordinal.asc())
            .limit(limit)
        ).all()
        return [SearchHit(chunk=chunk, document=document) for chunk, document in rows]


__all__ = ["ChunkDraft", "KnowledgeRepository", "SearchHit"]
