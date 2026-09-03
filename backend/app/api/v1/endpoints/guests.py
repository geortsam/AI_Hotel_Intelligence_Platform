"""Guest endpoints, nested under their owning hotel.

``/hotels/{hotel_public_id}/guests/{guest_public_id}``. Guests are hotel-scoped, so the hotel
segment is part of how a guest is reached, not decoration around it -- the same person at two
properties is two rows with two public ids.

``guests.id`` never appears in a URL or a response.

HTTP concerns only. The 404s and 409s are raised by the service as domain errors and rendered
by the Stage 3A handlers; this module builds no error payloads and formats no PII.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import GuestServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse, Page
from app.schemas.guest import GuestCreate, GuestResponse, GuestUpdate
from app.services.guest import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}/guests", tags=["guests"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]
GuestPath = Annotated[uuid.UUID, Path(description="Public identifier of the guest.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel, or the guest within it, does not exist.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The request conflicts with existing data.",
    }
}


@router.post(
    "",
    response_model=GuestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a guest for a hotel",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def create_guest(
    hotel_public_id: HotelPath, payload: GuestCreate, service: GuestServiceDep
) -> GuestResponse:
    """201 on success; 404 if the hotel does not exist; 409 if another guest at this hotel
    already uses the email address."""
    return service.create(hotel_public_id, payload)


@router.get(
    "",
    response_model=Page[GuestResponse],
    summary="List a hotel's guests",
    description="One page of guests belonging to this hotel, ordered by surname then "
    "forename. The hotel path segment is the scope: there is no cross-hotel listing.",
    responses=NOT_FOUND_RESPONSE,
)
def list_guests(
    hotel_public_id: HotelPath,
    service: GuestServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[GuestResponse]:
    """404 for an unknown hotel -- distinct from a known hotel with no guests."""
    return service.list(hotel_public_id, page=page, page_size=page_size)


@router.get(
    "/{guest_public_id}",
    response_model=GuestResponse,
    summary="Get a guest",
    responses=NOT_FOUND_RESPONSE,
)
def get_guest(
    hotel_public_id: HotelPath, guest_public_id: GuestPath, service: GuestServiceDep
) -> GuestResponse:
    """Resolved by hotel AND public id, so a guest of another property is not reachable."""
    return service.get(hotel_public_id, guest_public_id)


@router.patch(
    "/{guest_public_id}",
    response_model=GuestResponse,
    summary="Partially update a guest",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def update_guest(
    hotel_public_id: HotelPath,
    guest_public_id: GuestPath,
    payload: GuestUpdate,
    service: GuestServiceDep,
) -> GuestResponse:
    """Only the fields present in the body are written; the rest are preserved."""
    return service.update(hotel_public_id, guest_public_id, payload)


@router.delete(
    "/{guest_public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a guest",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def delete_guest(
    hotel_public_id: HotelPath, guest_public_id: GuestPath, service: GuestServiceDep
) -> None:
    """204 on success. 409 when bookings still reference the guest -- reviews, by contrast,
    are detached by the database's SET NULL policy rather than blocking the delete."""
    service.delete(hotel_public_id, guest_public_id)
