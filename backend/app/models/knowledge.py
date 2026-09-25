"""Hotel knowledge documents and their chunks (Stage 7.9). Architecture §6.2, Amendment A4.

    hotel_documents          one row per document VERSION, owned by one hotel
         │  supersedes_id ──┘ (same hotel, by composite foreign key)
         ▼
    hotel_document_chunks    one row per chunk of one version; the unit of retrieval

## Versioning is by new row, never by edit

A re-uploaded document is a new ``hotel_documents`` row with ``version + 1`` whose
``supersedes_id`` names the version it replaces; the replaced version becomes ``superseded``.
Nothing else about a version ever changes: a trigger (migration 0014) refuses any UPDATE that
touches a column other than ``status`` / ``updated_at``, allows only ``active -> superseded``
and ``active -> withdrawn``, and refuses DELETE outright. Chunks are append-only, by their own
trigger. So a chunk cited in an answer three months ago still resolves to the same text, the
same version and the same title.

## Withdrawal keeps the rows (Stage 7.9 decision)

Withdrawing a version sets its status; its chunks stay, with their text and ``public_id``. They
leave retrieval because the search query admits only ``active`` versions -- in its WHERE clause,
alongside the hotel -- not because they were deleted. Hard deletion is a separate, audited
operation that no stage has built yet.

## Tenancy

``hotel_id`` lives on the document. A chunk belongs to exactly one document, so its hotel is the
document's; the retrieval query joins the two and filters ``hotel_id`` in SQL. A version can
supersede only a version of the SAME hotel: ``supersedes_id`` is a composite foreign key over
``(supersedes_id, hotel_id)``, the pattern every cross-row reference in this schema uses to make
a cross-tenant reference structurally impossible rather than merely unlikely.

## The search column

``search_vector`` is computed by the repository at insert, as
``to_tsvector(<document language>::regconfig, text)``. Not a generated column: PostgreSQL requires
a generation expression to be immutable, and casting a text column to ``regconfig`` is not. The
chunk is append-only, so the vector can never drift from the text it was computed from.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, pk_column
from app.models.enums import DocumentLanguage, DocumentStatus

#: Bounds the schema enforces, and the service validates against first so a client sees a 422
#: rather than a constraint violation.
MAX_TITLE_LENGTH = 200
MAX_SOURCE_LENGTH = 200
MAX_CHUNK_LENGTH = 4000


class HotelDocument(TimestampMixin, Base):
    """One version of one hotel document."""

    __tablename__ = "hotel_documents"

    id: Mapped[int] = pk_column()
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=sql_text("gen_random_uuid()")
    )
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: Where the text came from, in the uploader's words ("Staff handbook, March 2026").
    source: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    #: SHA-256 of the normalised content this version was chunked from.
    content_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_hotel_documents_public_id"),
        # The composite-FK target below. Redundant as a business rule (id is already unique).
        UniqueConstraint("id", "hotel_id", name="uq_hotel_documents_id_hotel_id"),
        # A version is superseded at most once: two concurrent re-uploads cannot both win.
        UniqueConstraint("supersedes_id", name="uq_hotel_documents_supersedes_id"),
        ForeignKeyConstraint(
            ["supersedes_id", "hotel_id"],
            ["hotel_documents.id", "hotel_documents.hotel_id"],
            name="fk_hotel_documents_supersedes_id_hotel_id_hotel_documents",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"char_length(title) BETWEEN 1 AND {MAX_TITLE_LENGTH}", name="title_bounded"
        ),
        CheckConstraint(
            f"source IS NULL OR char_length(source) BETWEEN 1 AND {MAX_SOURCE_LENGTH}",
            name="source_bounded",
        ),
        CheckConstraint(f"language IN ({DocumentLanguage.sql_in_list()})", name="language_valid"),
        CheckConstraint("content_checksum ~ '^[0-9a-f]{64}$'", name="content_checksum_shape"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(version = 1) = (supersedes_id IS NULL)", name="first_version_supersedes_nothing"
        ),
        CheckConstraint(f"status IN ({DocumentStatus.sql_in_list()})", name="status_valid"),
        # The listing and the retrieval join: one hotel's documents in one status.
        Index("ix_hotel_documents_hotel_id_status", "hotel_id", "status"),
    )


class HotelDocumentChunk(Base):
    """One retrievable passage of one document version. Append-only."""

    __tablename__ = "hotel_document_chunks"

    id: Mapped[int] = pk_column()
    #: What a citation names (Stage 7.10). Stable for the life of the row, which is forever.
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=sql_text("gen_random_uuid()")
    )
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotel_documents.id", ondelete="RESTRICT"), nullable=False
    )
    #: Position within the document version, from 0. Deterministic given the content.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Whitespace-separated words, not model tokens: no tokenizer is a dependency here.
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    search_vector: Mapped[str] = mapped_column(TSVECTOR, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_hotel_document_chunks_public_id"),
        UniqueConstraint(
            "document_id", "ordinal", name="uq_hotel_document_chunks_document_id_ordinal"
        ),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint(f"char_length(text) BETWEEN 1 AND {MAX_CHUNK_LENGTH}", name="text_bounded"),
        CheckConstraint("token_count >= 1", name="token_count_positive"),
        # The retrieval index: full-text search over one hotel's active chunks.
        Index(
            "ix_hotel_document_chunks_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
    )


__all__ = [
    "MAX_CHUNK_LENGTH",
    "MAX_SOURCE_LENGTH",
    "MAX_TITLE_LENGTH",
    "HotelDocument",
    "HotelDocumentChunk",
]
