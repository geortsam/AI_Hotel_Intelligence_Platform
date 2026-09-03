"""Room-type amenity assignment endpoints.

Nested under the existing hierarchy:
``/hotels/{hotel_public_id}/room-types/{room_type_code}/amenities``.

The hotel and room type come from the path and are resolved in that order, so a room type at
hotel A is never reachable through hotel B. The client supplies only an amenity ``code``;
no internal id -- hotel, room type or amenity -- is ever accepted or returned.

HTTP concerns only; errors are raised by the service and rendered by the Stage 3A handlers.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import RoomTypeAmenityServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.amenity import AmenityAssignment, AmenityResponse
from app.schemas.common import ErrorResponse, Page
from app.services.amenity import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(
    prefix="/hotels/{hotel_public_id}/room-types/{room_type_code}/amenities",
    tags=["room-type-amenities"],
)

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]
RoomTypeCodePath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=20,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
        description="Room type code, unique within the hotel.",
    ),
]
AmenityCodePath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=50,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
        description="Globally unique amenity code.",
    ),
]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel, the room type within it, or the amenity does not exist.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The amenity is already assigned to this room type.",
    }
}


@router.get(
    "",
    response_model=Page[AmenityResponse],
    summary="List a room type's amenities",
    description="One page of the amenities assigned to this room type, ordered by code.",
    responses=NOT_FOUND_RESPONSE,
)
def list_room_type_amenities(
    hotel_public_id: HotelPath,
    room_type_code: RoomTypeCodePath,
    service: RoomTypeAmenityServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[AmenityResponse]:
    return service.list(hotel_public_id, room_type_code.upper(), page=page, page_size=page_size)


@router.post(
    "",
    response_model=AmenityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Assign an existing amenity to this room type",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def assign_amenity(
    hotel_public_id: HotelPath,
    room_type_code: RoomTypeCodePath,
    payload: AmenityAssignment,
    service: RoomTypeAmenityServiceDep,
) -> AmenityResponse:
    """201 on success; 404 if the hotel, room type or amenity is unknown; 409 if already
    assigned. The amenity must already exist -- assignment references the catalogue, it does
    not extend it."""
    return service.assign(hotel_public_id, room_type_code.upper(), payload)


@router.delete(
    "/{amenity_code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an amenity from this room type",
    responses=NOT_FOUND_RESPONSE,
    # Stage 4.2: manager or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def unassign_amenity(
    hotel_public_id: HotelPath,
    room_type_code: RoomTypeCodePath,
    amenity_code: AmenityCodePath,
    service: RoomTypeAmenityServiceDep,
) -> None:
    """204 on success. Removes the association only -- the amenity stays in the catalogue,
    still available to every other room type."""
    service.unassign(hotel_public_id, room_type_code.upper(), amenity_code.upper())
