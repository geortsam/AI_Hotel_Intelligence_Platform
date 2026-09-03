"""Amenity catalogue endpoints.

Mounted at ``/api/v1/amenities``, **not** under a hotel. That is the schema's shape, not a
convenience: ``amenities`` has no ``hotel_id``, and its ``code`` is globally unique. Nesting
a shared catalogue under one hotel would imply an ownership the database does not model.

Identified by ``code``. ``amenities.id`` never appears in a URL.

HTTP concerns only; errors are raised by the service and rendered by the Stage 3A handlers.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import AmenityServiceDep, get_current_user, require_platform_admin
from app.schemas.amenity import AmenityCreate, AmenityResponse, AmenityUpdate
from app.schemas.common import ErrorResponse, Page
from app.services.amenity import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(
    prefix="/amenities",
    tags=["amenities"],
    # Stage 4.2 established the read policy: the shared vocabularies are readable by any
    # authenticated caller, because every hotel must reference the vocabulary it is required
    # to use. Stage 4.3 left that untouched and reserved the WRITES, per-route below.
    dependencies=[Depends(get_current_user)],
)

CodePath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=50,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
        description="Globally unique amenity code.",
    ),
]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "Amenity not found."}
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The request conflicts with existing data.",
    }
}


@router.post(
    "",
    response_model=AmenityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an amenity",
    responses=CONFLICT_RESPONSE,
    # Stage 4.3: a global catalogue belongs to no hotel, so no per-hotel role can grant
    # authority over it. Reserved to the platform administrator; see require_platform_admin.
    dependencies=[Depends(require_platform_admin)],
)
def create_amenity(payload: AmenityCreate, service: AmenityServiceDep) -> AmenityResponse:
    """201 with the created entry; 409 when the code is already taken."""
    return service.create(payload)


@router.get(
    "",
    response_model=Page[AmenityResponse],
    summary="List amenities",
    description="One page of the global catalogue, ordered by code.",
)
def list_amenities(
    service: AmenityServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[AmenityResponse]:
    return service.list(page=page, page_size=page_size)


@router.get(
    "/{code}",
    response_model=AmenityResponse,
    summary="Get an amenity",
    responses=NOT_FOUND_RESPONSE,
)
def get_amenity(code: CodePath, service: AmenityServiceDep) -> AmenityResponse:
    return service.get(code.upper())


@router.patch(
    "/{code}",
    response_model=AmenityResponse,
    summary="Partially update an amenity",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.3: a global catalogue belongs to no hotel, so no per-hotel role can grant
    # authority over it. Reserved to the platform administrator; see require_platform_admin.
    dependencies=[Depends(require_platform_admin)],
)
def update_amenity(
    code: CodePath, payload: AmenityUpdate, service: AmenityServiceDep
) -> AmenityResponse:
    """Only the fields present in the body are written; the rest are preserved."""
    return service.update(code.upper(), payload)


@router.delete(
    "/{code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an amenity",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.3: a global catalogue belongs to no hotel, so no per-hotel role can grant
    # authority over it. Reserved to the platform administrator; see require_platform_admin.
    dependencies=[Depends(require_platform_admin)],
)
def delete_amenity(code: CodePath, service: AmenityServiceDep) -> None:
    """204 on success. 409 when the amenity is still assigned to any room type -- the
    database's RESTRICT policy is honoured, not worked around."""
    service.delete(code.upper())
