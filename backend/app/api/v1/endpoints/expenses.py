"""Expense endpoints -- a hotel's cost journal.

``/hotels/{hotel_public_id}/expenses``. Append-only for the same structural reason as
revenue: ``expenses`` has no ``public_id`` and no unique constraint beyond its primary key, so
no single-entry URL can be built. A credit note is a negative line.

**There is no booking filter and no booking field.** ``expenses`` has no ``booking_id`` column
at all -- a cost is incurred by the property, not by a stay. That asymmetry with revenue is
the schema's, and is preserved rather than smoothed over.

HTTP concerns only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import ExpenseServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse, Page
from app.schemas.finance import ExpenseCreate, ExpenseResponse
from app.services.finance import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}/expenses", tags=["expenses"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel or the expense category does not exist.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The values violate an expense constraint.",
    }
}


@router.post(
    "",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Post an expense line",
    description="is_recurring and recurrence_interval move together, mirroring "
    "ck_expenses_recurrence_consistent.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def create_expense(
    hotel_public_id: HotelPath, payload: ExpenseCreate, service: ExpenseServiceDep
) -> ExpenseResponse:
    """201 on success; 404 for an unknown hotel or category.

    Posting a cost creates no payment: the schema declares no relationship between the two.
    """
    return service.create(hotel_public_id, payload)


@router.get(
    "",
    response_model=Page[ExpenseResponse],
    summary="List a hotel's expenses",
    description="Newest first. Filters match ix_expenses_hotel_id_expense_date_category_id.",
    responses=NOT_FOUND_RESPONSE,
)
def list_expenses(
    hotel_public_id: HotelPath,
    service: ExpenseServiceDep,
    category_code: str | None = Query(default=None, description="Restrict to one category."),
    date_from: dt.date | None = Query(default=None, description="Inclusive lower bound."),
    date_to: dt.date | None = Query(default=None, description="Inclusive upper bound."),
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[ExpenseResponse]:
    """404 for an unknown hotel or category."""
    return service.list(
        hotel_public_id,
        page=page,
        page_size=page_size,
        category_code=category_code.upper() if category_code else None,
        date_from=date_from,
        date_to=date_to,
    )
