"""Hotel knowledge documents: upload, version, withdraw, read and search (Stage 7.9).

Architecture §6 and Amendment A4; `docs/knowledge-documents.md` is the design document. No
language model is involved anywhere in this module -- retrieval is separable from generation and
must be correct on its own before an answer depends on it (Stage 7.10).

## The four rules this service keeps

1. **Every method resolves the hotel first**, through the scope resolver -- the caller's
   membership is established before anything is read, and a hotel they cannot see is a 404.
   The repository is then handed that hotel's internal key and applies it in SQL.
2. **Versioning is by new row.** `create_version` locks the current version, inserts version
   `n + 1` superseding it, and marks the old one superseded -- in one transaction. The database
   refuses a second supersession of the same version, so two concurrent re-uploads cannot both
   win; the loser gets a 409.
3. **Withdrawal keeps the rows.** The current version's status becomes `withdrawn`; its chunks
   stay, with their `public_id`, and leave retrieval because the search admits only `active`.
4. **Every write is audited** on the existing trail, in the same transaction, before the commit:
   `document.created`, `document.version_created`, `document.withdrawn`. The event records the
   version's public id and its version number -- never its title or its text.

## Chunking

Done by `app.knowledge.chunking`, which is pure: text in, checksum and ordered chunks out. This
service only decides when to call it and stores what it returns.

## Content is data

Nothing in this module interprets a document. Its text is stored, indexed and returned verbatim;
it can never change which hotel is read, who may read it, or what a route does.
"""

from __future__ import annotations

import logging
import uuid
from typing import cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    GENERIC_CONFLICT_MESSAGE,
    ConflictError,
    NotFoundError,
    constraint_name_of,
    internal_fault,
    is_audit_integrity_failure,
    sqlstate_of,
)
from app.knowledge.chunking import checksum, chunk_text
from app.models.enums import AuditAction, AuditResourceType, DocumentStatus
from app.models.knowledge import HotelDocument, HotelDocumentChunk
from app.repositories.knowledge import KnowledgeRepository
from app.schemas.common import Page
from app.schemas.knowledge import (
    DocumentChunkResponse,
    DocumentCreate,
    DocumentDetail,
    DocumentSummary,
    DocumentVersionCreate,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    LanguageName,
    StatusName,
)
from app.services.audit import AuditTrail
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

#: The document listing's page sizes.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

DOCUMENT_NOT_FOUND = "No document with this identifier exists at this hotel."
NOT_CURRENT = "Only the current, active version of a document can be superseded or withdrawn."
UNCHANGED = "The content is identical to the current version; no new version was created."


class KnowledgeService:
    """Upload, version, withdraw, read and search one hotel's documents."""

    def __init__(
        self,
        session: Session,
        repository: KnowledgeRepository,
        scope: HotelScopeResolver,
        audit: AuditTrail,
    ) -> None:
        self._session = session
        self._repository = repository
        self._scope = scope
        self._audit = audit

    # --- writes ------------------------------------------------------------------------------

    def create_document(
        self, hotel_public_id: uuid.UUID, payload: DocumentCreate
    ) -> DocumentDetail:
        """Upload a new document as its first version."""
        hotel = self._scope.require_hotel(hotel_public_id)
        drafts = chunk_text(payload.content)
        try:
            document = self._repository.add_document(
                HotelDocument(
                    hotel_id=hotel.id,
                    title=payload.title,
                    source=payload.source,
                    language=payload.language,
                    content_checksum=checksum(payload.content),
                    version=1,
                    supersedes_id=None,
                    status=DocumentStatus.ACTIVE.value,
                )
            )
            self._repository.add_chunks(document, drafts)
            self._audit.record(
                AuditAction.DOCUMENT_CREATED,
                AuditResourceType.DOCUMENT,
                str(document.public_id),
                hotel_id=hotel.id,
                details={"version": document.version},
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        return self._detail(hotel.id, document, supersedes=None)

    def create_version(
        self,
        hotel_public_id: uuid.UUID,
        document_public_id: uuid.UUID,
        payload: DocumentVersionCreate,
    ) -> DocumentDetail:
        """Upload a new version superseding the current one. The old one stays addressable."""
        hotel = self._scope.require_hotel(hotel_public_id)
        drafts = chunk_text(payload.content)
        try:
            current = self._repository.get(hotel.id, document_public_id, for_update=True)
            if current is None:
                raise NotFoundError(DOCUMENT_NOT_FOUND)
            if current.status != DocumentStatus.ACTIVE.value:
                raise ConflictError(NOT_CURRENT)
            new_checksum = checksum(payload.content)
            if new_checksum == current.content_checksum:
                raise ConflictError(UNCHANGED)

            current.status = DocumentStatus.SUPERSEDED.value
            self._session.flush()
            document = self._repository.add_document(
                HotelDocument(
                    hotel_id=hotel.id,
                    title=payload.title,
                    source=payload.source,
                    language=payload.language,
                    content_checksum=new_checksum,
                    version=current.version + 1,
                    supersedes_id=current.id,
                    status=DocumentStatus.ACTIVE.value,
                )
            )
            self._repository.add_chunks(document, drafts)
            self._audit.record(
                AuditAction.DOCUMENT_VERSION_CREATED,
                AuditResourceType.DOCUMENT,
                str(document.public_id),
                hotel_id=hotel.id,
                details={"version": document.version},
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        except (NotFoundError, ConflictError):
            self._session.rollback()
            raise
        return self._detail(hotel.id, document, supersedes=current)

    def withdraw(
        self, hotel_public_id: uuid.UUID, document_public_id: uuid.UUID
    ) -> DocumentSummary:
        """Take the current version out of retrieval. Its rows and identifiers remain."""
        hotel = self._scope.require_hotel(hotel_public_id)
        try:
            document = self._repository.get(hotel.id, document_public_id, for_update=True)
            if document is None:
                raise NotFoundError(DOCUMENT_NOT_FOUND)
            if document.status != DocumentStatus.ACTIVE.value:
                raise ConflictError(NOT_CURRENT)
            document.status = DocumentStatus.WITHDRAWN.value
            self._session.flush()
            self._audit.record(
                AuditAction.DOCUMENT_WITHDRAWN,
                AuditResourceType.DOCUMENT,
                str(document.public_id),
                hotel_id=hotel.id,
                details={"version": document.version},
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        except (NotFoundError, ConflictError):
            self._session.rollback()
            raise
        return self._summary(hotel.id, document)

    # --- reads -------------------------------------------------------------------------------

    def list_documents(
        self,
        hotel_public_id: uuid.UUID,
        *,
        status: str | None,
        page: int,
        page_size: int,
    ) -> Page[DocumentSummary]:
        hotel = self._scope.require_hotel(hotel_public_id)
        documents, total = self._repository.list_documents(
            hotel.id, status=status, offset=(page - 1) * page_size, limit=page_size
        )
        counts = self._repository.chunk_counts(hotel.id, [d.id for d in documents])
        return Page.build(
            [self._summary(hotel.id, d, chunk_count=counts.get(d.id, 0)) for d in documents],
            total,
            page,
            page_size,
        )

    def get_document(
        self, hotel_public_id: uuid.UUID, document_public_id: uuid.UUID
    ) -> DocumentDetail:
        """One version and its chunks, whatever its status -- so a citation can be explained."""
        hotel = self._scope.require_hotel(hotel_public_id)
        document = self._repository.get(hotel.id, document_public_id)
        if document is None:
            raise NotFoundError(DOCUMENT_NOT_FOUND)
        supersedes = (
            self._repository.get_by_id(hotel.id, document.supersedes_id)
            if document.supersedes_id is not None
            else None
        )
        return self._detail(hotel.id, document, supersedes=supersedes)

    def search(self, hotel_public_id: uuid.UUID, query: str, limit: int) -> KnowledgeSearchResponse:
        """Full-text search over this hotel's active chunks. Bounded, ordered, deterministic."""
        hotel = self._scope.require_hotel(hotel_public_id)
        hits = self._repository.search(hotel.id, query, limit)
        return KnowledgeSearchResponse(
            query=query,
            limit=limit,
            results=[
                KnowledgeSearchResult(
                    chunk_public_id=hit.chunk.public_id,
                    document_public_id=hit.document.public_id,
                    title=hit.document.title,
                    version=hit.document.version,
                    language=cast(LanguageName, hit.document.language),
                    ordinal=hit.chunk.ordinal,
                    text=hit.chunk.text,
                )
                for hit in hits
            ],
        )

    # --- projection --------------------------------------------------------------------------

    def _summary(
        self,
        hotel_id: int,
        document: HotelDocument,
        *,
        chunk_count: int | None = None,
        supersedes: HotelDocument | None = None,
    ) -> DocumentSummary:
        if chunk_count is None:
            chunk_count = self._repository.chunk_counts(hotel_id, [document.id]).get(document.id, 0)
        if supersedes is None and document.supersedes_id is not None:
            supersedes = self._repository.get_by_id(hotel_id, document.supersedes_id)
        return DocumentSummary(
            public_id=document.public_id,
            title=document.title,
            source=document.source,
            language=cast(LanguageName, document.language),
            version=document.version,
            status=cast(StatusName, document.status),
            content_checksum=document.content_checksum,
            supersedes_public_id=supersedes.public_id if supersedes is not None else None,
            chunk_count=chunk_count,
            created_at=document.created_at,
        )

    def _detail(
        self, hotel_id: int, document: HotelDocument, *, supersedes: HotelDocument | None
    ) -> DocumentDetail:
        chunks: list[HotelDocumentChunk] = self._repository.chunks(hotel_id, document.id)
        summary = self._summary(hotel_id, document, chunk_count=len(chunks), supersedes=supersedes)
        return DocumentDetail(
            **summary.model_dump(),
            chunks=[
                DocumentChunkResponse(
                    public_id=chunk.public_id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    token_count=chunk.token_count,
                )
                for chunk in chunks
            ],
        )

    def _translate(self, exc: IntegrityError) -> Exception:
        """An integrity failure, classified the way every audit writer classifies one.

        The audit guard runs first (Stage 4.5.16). A violation of the one-supersession rule is a
        concurrent re-upload that lost the race: a conflict the client can retry against the
        new current version. Anything else is the generic conflict.
        """
        logger.warning("Knowledge integrity error (sqlstate=%s)", sqlstate_of(exc))

        if is_audit_integrity_failure(exc):
            return internal_fault(exc)

        if constraint_name_of(exc) == "uq_hotel_documents_supersedes_id":
            return ConflictError(NOT_CURRENT)
        return ConflictError(GENERIC_CONFLICT_MESSAGE)


__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "KnowledgeService"]
