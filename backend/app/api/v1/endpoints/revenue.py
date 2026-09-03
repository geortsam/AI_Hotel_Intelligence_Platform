"""Revenue endpoints -- a hotel's earnings journal.

``/hotels/{hotel_public_id}/revenue``. The collection is a journal, and it is **append-only**:
POST and GET, no PATCH, no DELETE, and **no single-entry URL**.

That is a consequence of the frozen schema, not a policy choice. ``revenue`` has no
``public_id`` and no unique constraint beyond its primary key -- two byte-identical lines
coexist -- so there is no identifier a single-entry URL could be built from, and exposing the
sequential BIGINT is not an acceptable substitute. The schema's own correction mechanism is a
compensating line: ``amount`` carries no positivity CHECK while ``tax_amount`` does.

Filters follow the reporting index ``ix_revenue_hotel_id_revenue_date_category_id``, plus the
partial ``ix_revenue_booking_id`` for the booking filter.

A revenue line is not a payment. Posting one creates no payment, touches no payment, and
neither reads nor adjusts ``bookings.total_amount``.

HTTP concerns only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import RevenueServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse, Page
from app.schemas.finance import RevenueCreate, RevenueResponse
from app.services.finance import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}/revenue", tags=["revenue"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel, the revenue category, or the booking does not exist.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The values violate a revenue constraint.",
    }
}


@router.post(
    "",
    response_model=RevenueResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Post a revenue line",
    description="booking_public_id is optional: revenue.booking_id is nullable, and a "
    "non-resident eating in the restaurant belongs to no stay.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def create_revenue(
    hotel_public_id: HotelPath, payload: RevenueCreate, service: RevenueServiceDep
) -> RevenueResponse:
    """201 on success; 404 for an unknown hotel, category or booking.

    The response carries no identifier for the line itself, because the table provides none.
    """
    return service.create(hotel_public_id, payload)


@router.get(
    "",
    response_model=Page[RevenueResponse],
    summary="List a hotel's revenue",
    description="Newest first. Filters match the reporting index: date range, then category.",
    responses=NOT_FOUND_RESPONSE,
)
def list_revenue(
    hotel_public_id: HotelPath,
    service: RevenueServiceDep,
    category_code: str | None = Query(default=None, description="Restrict to one category."),
    date_from: dt.date | None = Query(default=None, description="Inclusive lower bound."),
    date_to: dt.date | None = Query(default=None, description="Inclusive upper bound."),
    booking_public_id: uuid.UUID | None = Query(
        default=None, description="Restrict to revenue attached to one stay."
    ),
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[RevenueResponse]:
    """404 for an unknown hotel, category or booking -- distinct from a real filter that
    matched nothing, which is an empty page."""
    return service.list(
        hotel_public_id,
        page=page,
        page_size=page_size,
        category_code=category_code.upper() if category_code else None,
        date_from=date_from,
        date_to=date_to,
        booking_public_id=booking_public_id,
    )
