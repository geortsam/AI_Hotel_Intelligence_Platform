"""Hotel knowledge documents and full-text search (Stage 7.9). Six operations, all hotel-scoped.

    POST /hotels/{h}/documents                          upload a document     manager   201
    GET  /hotels/{h}/documents                          list versions         member
    GET  /hotels/{h}/documents/{d}                      one version + chunks  member
    POST /hotels/{h}/documents/{d}/versions             a new version         manager   201
    POST /hotels/{h}/documents/{d}/withdrawal           withdraw current      manager   200
    GET  /hotels/{h}/knowledge/search?q=&limit=         full-text search      member

Thin routes: each declares who may call it and delegates. The hotel is always the path segment
the caller was authorized for -- no body field and no query parameter names a hotel, and the
search takes only a query and a bound. The design, the versioning policy and the deletion
policy are in `docs/knowledge-documents.md`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import KnowledgeServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse, Page
from app.schemas.knowledge import (
    DEFAULT_SEARCH_RESULTS,
    MAX_QUERY_LENGTH,
    MAX_SEARCH_RESULTS,
    PERSONAL_DATA_RULE,
    DocumentCreate,
    DocumentDetail,
    DocumentSummary,
    DocumentVersionCreate,
    KnowledgeSearchResponse,
)
from app.services.knowledge import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}", tags=["knowledge"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the hotel.")]
DocumentPath = Annotated[uuid.UUID, Path(description="Public identifier of one document version.")]

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "No such hotel, the caller is not a member of it, or no such document at it. "
            "Indistinguishable by design."
        ),
    }
}
FORBIDDEN: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "The caller is a member of this hotel but below the manager role.",
    }
}
UNPROCESSABLE: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "The request is malformed, out of bounds, or omits the attestation.",
    }
}
CONFLICT: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": (
            "The target is not the current, active version -- it was superseded or withdrawn, "
            "possibly by a concurrent request -- or the content is identical to it."
        ),
    }
}

UPLOAD_RULES = (
    f"{PERSONAL_DATA_RULE} Content is stored and returned verbatim as data: nothing in a "
    "document is ever treated as an instruction, and it cannot change which hotel is read or "
    "who may read it."
)


@router.post(
    "/documents",
    response_model=DocumentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a hotel document",
    description=(
        "Stores the text as version 1 of a new document, split into citable chunks and indexed "
        f"for full-text search under the chosen language. Manager role. {UPLOAD_RULES}"
    ),
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
    responses={**NOT_FOUND, **FORBIDDEN, **UNPROCESSABLE},
)
def upload_document(
    hotel_public_id: HotelPath, payload: DocumentCreate, service: KnowledgeServiceDep
) -> DocumentDetail:
    return service.create_document(hotel_public_id, payload)


@router.get(
    "/documents",
    response_model=Page[DocumentSummary],
    summary="List this hotel's document versions",
    description=(
        "Every version of every document, newest first, optionally filtered by status. "
        "Superseded and withdrawn versions are listed too: they remain addressable so a past "
        "citation can be explained. Any member."
    ),
    responses={**NOT_FOUND, **UNPROCESSABLE},
)
def list_documents(
    hotel_public_id: HotelPath,
    service: KnowledgeServiceDep,
    status_filter: Literal["active", "superseded", "withdrawn"] | None = Query(
        default=None, alias="status", description="Only versions in this status."
    ),
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[DocumentSummary]:
    return service.list_documents(
        hotel_public_id, status=status_filter, page=page, page_size=page_size
    )


@router.get(
    "/documents/{document_public_id}",
    response_model=DocumentDetail,
    summary="Read one document version and its chunks",
    description=(
        "One version with every chunk in order, whatever its status. Any member. Chunk text is "
        "returned verbatim, as data."
    ),
    responses={**NOT_FOUND, **UNPROCESSABLE},
)
def get_document(
    hotel_public_id: HotelPath, document_public_id: DocumentPath, service: KnowledgeServiceDep
) -> DocumentDetail:
    return service.get_document(hotel_public_id, document_public_id)


@router.post(
    "/documents/{document_public_id}/versions",
    response_model=DocumentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a new version of a document",
    description=(
        "Creates the next version, which supersedes the addressed one; the addressed version must "
        "be the current, active one. The superseded version and its chunks stay addressable but "
        f"leave search. Manager role. {UPLOAD_RULES}"
    ),
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
    responses={**NOT_FOUND, **FORBIDDEN, **UNPROCESSABLE, **CONFLICT},
)
def upload_version(
    hotel_public_id: HotelPath,
    document_public_id: DocumentPath,
    payload: DocumentVersionCreate,
    service: KnowledgeServiceDep,
) -> DocumentDetail:
    return service.create_version(hotel_public_id, document_public_id, payload)


@router.post(
    "/documents/{document_public_id}/withdrawal",
    response_model=DocumentSummary,
    status_code=status.HTTP_200_OK,
    summary="Withdraw the current version of a document",
    description=(
        "Takes the version out of search. Nothing is deleted: the version and its chunks keep "
        "their identifiers so a past citation can still be explained. Manager role."
    ),
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
    responses={**NOT_FOUND, **FORBIDDEN, **UNPROCESSABLE, **CONFLICT},
)
def withdraw_document(
    hotel_public_id: HotelPath, document_public_id: DocumentPath, service: KnowledgeServiceDep
) -> DocumentSummary:
    return service.withdraw(hotel_public_id, document_public_id)


@router.get(
    "/knowledge/search",
    response_model=KnowledgeSearchResponse,
    summary="Search this hotel's documents",
    description=(
        "PostgreSQL full-text search over this hotel's ACTIVE document versions, each chunk "
        "matched under its own document's language. Most relevant first (ts_rank_cd), ties "
        f"broken by document and chunk position, at most {MAX_SEARCH_RESULTS} results; the score "
        "itself is not returned. Only this hotel's "
        "documents can match: the hotel is applied in the query itself. Any member. Result "
        "text is untrusted document content, returned as data."
    ),
    responses={**NOT_FOUND, **UNPROCESSABLE},
)
def search_knowledge(
    hotel_public_id: HotelPath,
    service: KnowledgeServiceDep,
    q: str = Query(
        min_length=1,
        max_length=MAX_QUERY_LENGTH,
        description="Search text. Quoted phrases, `or` and `-exclusion` are understood.",
    ),
    limit: int = Query(
        default=DEFAULT_SEARCH_RESULTS,
        ge=1,
        le=MAX_SEARCH_RESULTS,
        description=f"Most results to return (max {MAX_SEARCH_RESULTS}).",
    ),
) -> KnowledgeSearchResponse:
    return service.search(hotel_public_id, q, limit)
