"""Physical room endpoints, nested under hotel and room type.

The URL states the full ownership chain:
``/hotels/{hotel_public_id}/room-types/{room_type_code}/rooms/{room_number}``.

``rooms.id`` never appears in a URL. ``room_number`` is the identifier, backed by
``UNIQUE (hotel_id, room_number)``.

HTTP concerns only. The 404s and 409s are raised by the service as domain errors and rendered
by the Stage 3A handlers; this module builds no error payloads.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import RoomServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse, Page
from app.schemas.room import RoomCreate, RoomResponse, RoomUpdate
from app.services.room import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(
    prefix="/hotels/{hotel_public_id}/room-types/{room_type_code}/rooms", tags=["rooms"]
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
RoomNumberPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=20,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Room number, unique within the hotel.",
    ),
]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel, the room type within it, or the room within that type "
        "does not exist.",
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
    response_model=RoomResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a room of this room type",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def create_room(
    hotel_public_id: HotelPath,
    room_type_code: RoomTypeCodePath,
    payload: RoomCreate,
    service: RoomServiceDep,
) -> RoomResponse:
    """201 on success; 404 if the hotel or room type is unknown; 409 if the hotel already
    uses that room number under any type."""
    return service.create(hotel_public_id, room_type_code.upper(), payload)


@router.get(
    "",
    response_model=Page[RoomResponse],
    summary="List this room type's rooms",
    description="One page of rooms of this type at this hotel, ordered by room number.",
    responses=NOT_FOUND_RESPONSE,
)
def list_rooms(
    hotel_public_id: HotelPath,
    room_type_code: RoomTypeCodePath,
    service: RoomServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[RoomResponse]:
    """404 for an unknown hotel or room type -- distinct from a known type with no rooms."""
    return service.list(hotel_public_id, room_type_code.upper(), page=page, page_size=page_size)


@router.get(
    "/{room_number}",
    response_model=RoomResponse,
    summary="Get a room",
    responses=NOT_FOUND_RESPONSE,
)
def get_room(
    hotel_public_id: HotelPath,
    room_type_code: RoomTypeCodePath,
    room_number: RoomNumberPath,
    service: RoomServiceDep,
) -> RoomResponse:
    """Resolved against the whole chain, so neither another hotel's nor another room type's
    room of the same number is reachable here."""
    return service.get(hotel_public_id, room_type_code.upper(), room_number.upper())


@router.patch(
    "/{room_number}",
    response_model=RoomResponse,
    summary="Partially update a room",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def update_room(
    hotel_public_id: HotelPath,
    room_type_code: RoomTypeCodePath,
    room_number: RoomNumberPath,
    payload: RoomUpdate,
    service: RoomServiceDep,
) -> RoomResponse:
    """Only the fields present in the body are written; the rest are preserved."""
    return service.update(hotel_public_id, room_type_code.upper(), room_number.upper(), payload)


@router.delete(
    "/{room_number}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a room",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def delete_room(
    hotel_public_id: HotelPath,
    room_type_code: RoomTypeCodePath,
    room_number: RoomNumberPath,
    service: RoomServiceDep,
) -> None:
    """204 on success. 409 when reservations still reference it -- no cascade is performed."""
    service.delete(hotel_public_id, room_type_code.upper(), room_number.upper())
