"""Hotel endpoints.

HTTP concerns only: paths, status codes, query-parameter limits and response models. Every
line of business logic is in ``HotelService``; every query is in ``HotelRepository``. The
404 and 409 responses here are raised by the service as domain errors and rendered by the
Stage 3A handlers -- this module never builds an error payload itself.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, status

from app.api.deps import HotelServiceDep
from app.schemas.common import ErrorResponse, Page
from app.schemas.hotel import HotelCreate, HotelResponse, HotelUpdate
from app.services.hotel import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels", tags=["hotels"])

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "Hotel not found."}
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The request conflicts with existing data.",
    }
}


@router.post(
    "",
    response_model=HotelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a hotel",
    responses=CONFLICT_RESPONSE,
)
def create_hotel(payload: HotelCreate, service: HotelServiceDep) -> HotelResponse:
    """201 with the created resource; 409 when the slug is taken."""
    return service.create(payload)


@router.get(
    "",
    response_model=Page[HotelResponse],
    summary="List hotels",
    description="Returns one page of hotels ordered by name, then by internal id as a "
    "tiebreaker so paging is stable when names repeat.",
)
def list_hotels(
    service: HotelServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[HotelResponse]:
    """The upper bound on `page_size` is enforced by FastAPI, so a client cannot ask for
    the whole table in one request and exhaust the server's memory."""
    return service.list(page=page, page_size=page_size)


@router.get(
    "/{public_id}",
    response_model=HotelResponse,
    summary="Get a hotel",
    responses=NOT_FOUND_RESPONSE,
)
def get_hotel(public_id: uuid.UUID, service: HotelServiceDep) -> HotelResponse:
    """`public_id` is a UUID; FastAPI rejects a malformed one with 422 before the service
    is reached, so no lookup is wasted on input that cannot match."""
    return service.get(public_id)


@router.patch(
    "/{public_id}",
    response_model=HotelResponse,
    summary="Partially update a hotel",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
)
def update_hotel(
    public_id: uuid.UUID, payload: HotelUpdate, service: HotelServiceDep
) -> HotelResponse:
    """Only the fields present in the body are written; the rest are preserved."""
    return service.update(public_id, payload)


@router.delete(
    "/{public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a hotel",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
)
def delete_hotel(public_id: uuid.UUID, service: HotelServiceDep) -> None:
    """204 on success. 409 when the database's RESTRICT policy refuses because dependent
    records exist -- no cascade is performed."""
    service.delete(public_id)
