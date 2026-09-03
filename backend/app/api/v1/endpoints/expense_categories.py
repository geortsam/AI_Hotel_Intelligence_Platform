"""Expense category endpoints -- a global lookup, addressed by ``code``.

A separate table from ``revenue_categories`` with a **separate code space**: the same code may
exist in both, because the two unique constraints are independent (verified live). They are
not collapsed into one "category" resource for that reason, and because their flags differ --
``is_room_revenue`` on one, ``is_fixed_cost`` on the other.

As with revenue categories: no ``hotel_id``, so the collection sits outside the hotel
hierarchy; ``code`` is the identity and PATCH cannot reach it; DELETE refuses with 409 while
``expenses.category_id``'s RESTRICT still binds.

HTTP concerns only.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import ExpenseCategoryServiceDep, get_current_user, require_platform_admin
from app.schemas.common import ErrorResponse, Page
from app.schemas.finance import (
    ExpenseCategoryCreate,
    ExpenseCategoryResponse,
    ExpenseCategoryUpdate,
)
from app.services.finance import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(
    prefix="/expense-categories",
    tags=["expense categories"],
    # Stage 4.2 established the read policy: the shared vocabularies are readable by any
    # authenticated caller, because every hotel must reference the vocabulary it is required
    # to use. Stage 4.3 left that untouched and reserved the WRITES, per-route below.
    dependencies=[Depends(get_current_user)],
)

CodePath = Annotated[
    str,
    Path(description="Code of the expense category, e.g. UTILITIES.", pattern=r"^[A-Za-z0-9_-]+$"),
]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No expense category with this code exists.",
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
    response_model=ExpenseCategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an expense category",
    responses=CONFLICT_RESPONSE,
    # Stage 4.3: a global catalogue belongs to no hotel, so no per-hotel role can grant
    # authority over it. Reserved to the platform administrator; see require_platform_admin.
    dependencies=[Depends(require_platform_admin)],
)
def create_expense_category(
    payload: ExpenseCategoryCreate, service: ExpenseCategoryServiceDep
) -> ExpenseCategoryResponse:
    return service.create(payload)


@router.get(
    "",
    response_model=Page[ExpenseCategoryResponse],
    summary="List expense categories",
    description="Alphabetical by code.",
)
def list_expense_categories(
    service: ExpenseCategoryServiceDep,
    is_active: bool | None = Query(default=None, description="Restrict to active or retired."),
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[ExpenseCategoryResponse]:
    return service.list(page=page, page_size=page_size, is_active=is_active)


@router.get(
    "/{code}",
    response_model=ExpenseCategoryResponse,
    summary="Get an expense category",
    responses=NOT_FOUND_RESPONSE,
)
def get_expense_category(
    code: CodePath, service: ExpenseCategoryServiceDep
) -> ExpenseCategoryResponse:
    return service.get(code.upper())


@router.patch(
    "/{code}",
    response_model=ExpenseCategoryResponse,
    summary="Update an expense category",
    description="Accepts name, is_fixed_cost and is_active. The code is the URL identity and "
    "is not editable.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.3: a global catalogue belongs to no hotel, so no per-hotel role can grant
    # authority over it. Reserved to the platform administrator; see require_platform_admin.
    dependencies=[Depends(require_platform_admin)],
)
def update_expense_category(
    code: CodePath, payload: ExpenseCategoryUpdate, service: ExpenseCategoryServiceDep
) -> ExpenseCategoryResponse:
    return service.update(code.upper(), payload)


@router.delete(
    "/{code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an unused expense category",
    description="409 when expenses still reference it; deactivate it instead.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.3: a global catalogue belongs to no hotel, so no per-hotel role can grant
    # authority over it. Reserved to the platform administrator; see require_platform_admin.
    dependencies=[Depends(require_platform_admin)],
)
def delete_expense_category(code: CodePath, service: ExpenseCategoryServiceDep) -> None:
    service.delete(code.upper())
