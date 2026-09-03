"""Room type endpoints, nested under their owning hotel.

The URL states the ownership: ``/hotels/{hotel_public_id}/room-types/{code}``. That is not
decoration -- ``room_types.code`` is unique only within a hotel, so the hotel segment is part
of the identifier, not context around it. ``room_types.id`` never appears in a URL.

HTTP concerns only. The 404s and 409s below are raised by the service as domain errors and
rendered by the Stage 3A handlers; this module builds no error payloads.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import RoomTypeServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse, Page
from app.schemas.room_type import RoomTypeCreate, RoomTypeResponse, RoomTypeUpdate
from app.services.room_type import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}/room-types", tags=["room-types"])

#: Mirrors the schema's pattern so a malformed code is a 422 at the edge rather than a
#: fruitless lookup.
CodePath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=20,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
        description="Room type code, unique within the hotel.",
    ),
]
HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel, or the room type within it, does not exist.",
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
    response_model=RoomTypeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a room type for a hotel",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def create_room_type(
    hotel_public_id: HotelPath, payload: RoomTypeCreate, service: RoomTypeServiceDep
) -> RoomTypeResponse:
    """201 on success; 404 if the hotel does not exist; 409 if the code is already used."""
    return service.create(hotel_public_id, payload)


@router.get(
    "",
    response_model=Page[RoomTypeResponse],
    summary="List a hotel's room types",
    description="One page of room types belonging to this hotel, ordered by code. The hotel "
    "path segment is the filter: there is no cross-hotel listing.",
    responses=NOT_FOUND_RESPONSE,
)
def list_room_types(
    hotel_public_id: HotelPath,
    service: RoomTypeServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[RoomTypeResponse]:
    """404 for an unknown hotel -- distinct from an existing hotel with no room types."""
    return service.list(hotel_public_id, page=page, page_size=page_size)


@router.get(
    "/{code}",
    response_model=RoomTypeResponse,
    summary="Get a room type",
    responses=NOT_FOUND_RESPONSE,
)
def get_room_type(
    hotel_public_id: HotelPath, code: CodePath, service: RoomTypeServiceDep
) -> RoomTypeResponse:
    """Resolved by hotel AND code, so another hotel's identical code is not reachable."""
    return service.get(hotel_public_id, code.upper())


@router.patch(
    "/{code}",
    response_model=RoomTypeResponse,
    summary="Partially update a room type",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def update_room_type(
    hotel_public_id: HotelPath,
    code: CodePath,
    payload: RoomTypeUpdate,
    service: RoomTypeServiceDep,
) -> RoomTypeResponse:
    """Only the fields present in the body are written; the rest are preserved."""
    return service.update(hotel_public_id, code.upper(), payload)


@router.delete(
    "/{code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a room type",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def delete_room_type(
    hotel_public_id: HotelPath, code: CodePath, service: RoomTypeServiceDep
) -> None:
    """204 on success. 409 when rooms still reference it -- no cascade is performed."""
    service.delete(hotel_public_id, code.upper())
