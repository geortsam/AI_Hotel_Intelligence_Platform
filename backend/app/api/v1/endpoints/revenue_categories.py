"""Revenue category endpoints -- a global lookup, addressed by ``code``.

``revenue_categories`` has no ``hotel_id``: the table is one vocabulary for the whole
installation, exactly like ``amenities``, so "F&B" means the same thing at every property.
The collection therefore sits outside the hotel hierarchy rather than under it.

``code`` is the URL identity (``uq_revenue_categories_code``); ``revenue_categories.id`` is a
sequential BIGINT and stays internal. PATCH cannot reach ``code`` -- renaming it would break
every link already pointing at the category.

DELETE is offered because the schema supports it, and refuses with 409 when the category has
entries: ``revenue.category_id`` is ON DELETE RESTRICT and that is the database's decision,
not this router's. ``is_active: false`` retires a stream without losing its history.

HTTP concerns only.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import RevenueCategoryServiceDep, get_current_user, require_platform_admin
from app.schemas.common import ErrorResponse, Page
from app.schemas.finance import (
    RevenueCategoryCreate,
    RevenueCategoryResponse,
    RevenueCategoryUpdate,
)
from app.services.finance import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(
    prefix="/revenue-categories",
    tags=["revenue categories"],
    # Stage 4.2 established the read policy: the shared vocabularies are readable by any
    # authenticated caller, because every hotel must reference the vocabulary it is required
    # to use. Stage 4.3 left that untouched and reserved the WRITES, per-route below.
    dependencies=[Depends(get_current_user)],
)

CodePath = Annotated[
    str, Path(description="Code of the revenue category, e.g. FB.", pattern=r"^[A-Za-z0-9_-]+$")
]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No revenue category with this code exists.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The code is already taken, or the category still has entries.",
    }
}


@router.post(
    "",
    response_model=RevenueCategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a revenue category",
    responses=CONFLICT_RESPONSE,
    # Stage 4.3: a global catalogue belongs to no hotel, so no per-hotel role can grant
    # authority over it. Reserved to the platform administrator; see require_platform_admin.
    dependencies=[Depends(require_platform_admin)],
)
def create_revenue_category(
    payload: RevenueCategoryCreate, service: RevenueCategoryServiceDep
) -> RevenueCategoryResponse:
    """409 on a duplicate code -- the unique constraint decides, not a prior lookup."""
    return service.create(payload)


@router.get(
    "",
    response_model=Page[RevenueCategoryResponse],
    summary="List revenue categories",
    description="Alphabetical by code: a lookup table reads as a catalogue, not a feed.",
)
def list_revenue_categories(
    service: RevenueCategoryServiceDep,
    is_active: bool | None = Query(default=None, description="Restrict to active or retired."),
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[RevenueCategoryResponse]:
    return service.list(page=page, page_size=page_size, is_active=is_active)


@router.get(
    "/{code}",
    response_model=RevenueCategoryResponse,
    summary="Get a revenue category",
    responses=NOT_FOUND_RESPONSE,
)
def get_revenue_category(
    code: CodePath, service: RevenueCategoryServiceDep
) -> RevenueCategoryResponse:
    return service.get(code.upper())


@router.patch(
    "/{code}",
    response_model=RevenueCategoryResponse,
    summary="Update a revenue category",
    description="Accepts name, is_room_revenue and is_active. The code is the URL identity "
    "and is not editable.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.3: a global catalogue belongs to no hotel, so no per-hotel role can grant
    # authority over it. Reserved to the platform administrator; see require_platform_admin.
    dependencies=[Depends(require_platform_admin)],
)
def update_revenue_category(
    code: CodePath, payload: RevenueCategoryUpdate, service: RevenueCategoryServiceDep
) -> RevenueCategoryResponse:
    return service.update(code.upper(), payload)


@router.delete(
    "/{code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an unused revenue category",
    description="409 when revenue entries still reference it; deactivate it instead.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.3: a global catalogue belongs to no hotel, so no per-hotel role can grant
    # authority over it. Reserved to the platform administrator; see require_platform_admin.
    dependencies=[Depends(require_platform_admin)],
)
def delete_revenue_category(code: CodePath, service: RevenueCategoryServiceDep) -> None:
    """The RESTRICT is PostgreSQL's; no cascade is invented here."""
    service.delete(code.upper())
