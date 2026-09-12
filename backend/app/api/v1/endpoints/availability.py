"""Availability search endpoint.

One read-only route. HTTP concerns only: no SQL, no session, no transaction, and no part of
the availability rule -- that lives in the service and, ultimately, in the exclusion
constraint the service's query mirrors.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from app.api.deps import AvailabilityServiceDep
from app.schemas.availability import AvailabilityResult
from app.schemas.common import ErrorResponse
from app.services.availability import (
    MAX_ROOM_TYPE_DEMANDS,
    MAX_ROOMS_REQUIRED,
    parse_demands,
)

router = APIRouter(prefix="/hotels/{hotel_public_id}/availability", tags=["availability"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the hotel.")]

RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No such hotel, or no such room type at this hotel.",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "check_out is not later than check_in, the stay is too long, or "
            f"rooms_required is outside 1..{MAX_ROOMS_REQUIRED}."
        ),
    },
}


@router.get(
    "",
    response_model=AvailabilityResult,
    summary="Search a hotel's inventory for a stay",
    description=(
        "Rooms this hotel could sell for the requested half-open stay `[check_in, check_out)`. "
        "A result is not a reservation: nothing is held, and a room named here may be taken "
        "by another request a moment later. The booking transaction and the database's "
        "exclusion constraint remain the final authority. Ask for several rooms "
        "with `rooms_required`: the answer then lists only room types that can "
        "supply that many from their own inventory for the whole stay."
    ),
    responses=RESPONSES,
)
def search_availability(
    hotel_public_id: HotelPath,
    service: AvailabilityServiceDep,
    check_in: Annotated[dt.date, Query(description="First night of the stay, inclusive.")],
    check_out: Annotated[
        dt.date, Query(description="Departure day, exclusive: no night is sold on it.")
    ],
    room_type_code: Annotated[
        str | None, Query(description="Restrict to one room type at this hotel.")
    ] = None,
    guests: Annotated[
        int | None,
        Query(ge=1, description="Only room types whose max_occupancy can seat this many."),
    ] = None,
    rooms_required: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_ROOMS_REQUIRED,
            description=(
                "How many rooms of a single type are needed. Room types that cannot "
                "supply this many for the whole stay are omitted. Counting rooms, "
                "not guests -- see `guests` for occupancy."
            ),
        ),
    ] = 1,
    rooms: Annotated[
        list[str] | None,
        Query(
            description=(
                "A mixed request, as `CODE:COUNT` repeated -- "
                "`rooms=DLX:2&rooms=SUI:1`. Every named type is reported with "
                "what was asked and what is free, and the answer is sufficient only when "
                "each can supply its own share. Cannot be combined with room_type_code "
                "or rooms_required; at most "
                f"{MAX_ROOM_TYPE_DEMANDS} types."
            ),
        ),
    ] = None,
) -> AvailabilityResult:
    """A hotel with nothing free answers 200 with an empty list, not 404.

    Zero availability is a fact about the dates, not a failure of the request.
    """
    return service.search(
        hotel_public_id,
        check_in,
        check_out,
        room_type_code=room_type_code,
        guests=guests,
        rooms_required=rooms_required,
        demands=parse_demands(rooms) if rooms else None,
    )
