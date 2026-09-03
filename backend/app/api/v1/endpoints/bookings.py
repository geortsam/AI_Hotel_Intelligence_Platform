"""Booking endpoints, nested under their owning hotel.

``/hotels/{hotel_public_id}/bookings/{booking_public_id}``.

**No sub-resources for allocations or nights, and that is the schema's decision.** The
deferred night-completeness trigger requires an allocation and its nights to exist together
at COMMIT; since each request is one transaction, a route that created an allocation on its
own could never commit. Allocations and their nightly rates are therefore part of the booking
payload, not independently writable resources -- which also keeps the hierarchy at the depth
the existing guards enforce.

HTTP concerns only. The 404s, 409s and 422s are raised by the service and schemas and rendered
by the Stage 3A handlers.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import BookingServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.booking import BookingCreate, BookingResponse, BookingUpdate
from app.schemas.common import ErrorResponse, Page
from app.services.booking import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}/bookings", tags=["bookings"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]
BookingPath = Annotated[uuid.UUID, Path(description="Public identifier of the booking.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel, the guest, a named room, or the booking does not exist.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "A room is already booked for overlapping dates, the reference is "
        "taken, or the booking is still referenced by other records.",
    }
}


@router.post(
    "",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a booking with its rooms and nightly rates",
    description="Creates the booking, its room allocations and every priced night in one "
    "transaction. Room availability is decided by the database's exclusion constraint, not "
    "by a prior check.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def create_booking(
    hotel_public_id: HotelPath, payload: BookingCreate, service: BookingServiceDep
) -> BookingResponse:
    """201 on success; 404 for an unknown hotel, guest or room; 409 when a room is already
    held for overlapping dates by an active booking."""
    return service.create(hotel_public_id, payload)


@router.get(
    "",
    response_model=Page[BookingResponse],
    summary="List a hotel's bookings",
    description="One page of bookings, most recent arrival date first.",
    responses=NOT_FOUND_RESPONSE,
)
def list_bookings(
    hotel_public_id: HotelPath,
    service: BookingServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[BookingResponse]:
    """404 for an unknown hotel -- distinct from a known hotel with no bookings."""
    return service.list(hotel_public_id, page=page, page_size=page_size)


@router.get(
    "/{booking_public_id}",
    response_model=BookingResponse,
    summary="Get a booking",
    responses=NOT_FOUND_RESPONSE,
)
def get_booking(
    hotel_public_id: HotelPath, booking_public_id: BookingPath, service: BookingServiceDep
) -> BookingResponse:
    """Resolved by hotel AND public id, so another property's booking is unreachable."""
    return service.get(hotel_public_id, booking_public_id)


@router.patch(
    "/{booking_public_id}",
    response_model=BookingResponse,
    summary="Partially update a booking",
    description="Status, occupancy, source and commercial fields only. Dates, guest, "
    "reference and room allocation are not editable here -- changing them is a re-pricing "
    "operation the deferred night-completeness trigger will not accept as a field edit.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def update_booking(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    payload: BookingUpdate,
    service: BookingServiceDep,
) -> BookingResponse:
    """Confirming a booking whose room has since been taken returns 409: the status change
    cascades to the allocations and re-triggers the exclusion constraint."""
    return service.update(hotel_public_id, booking_public_id, payload)


@router.delete(
    "/{booking_public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a booking",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def delete_booking(
    hotel_public_id: HotelPath, booking_public_id: BookingPath, service: BookingServiceDep
) -> None:
    """204 on success -- allocations and nights cascade away with it. 409 when payments,
    revenue or reviews still reference it; cancelling is the usual alternative."""
    service.delete(hotel_public_id, booking_public_id)
