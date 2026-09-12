"""What a hotel could sell for a requested stay.

Stage 4.5.10. A read-only projection of rooms that no inventory-holding allocation overlaps.

**Identifiers follow the ones this API already uses.** A room is named by ``room_number``,
which is unique per hotel (``uq_rooms_hotel_id_room_number``) and is what the rooms endpoints
put in their paths; a room type by its ``code``. Neither has a UUID, so none is invented here,
and no internal BIGINT is exposed.

**A request for several rooms is a request for several rooms of ONE type** (Stage
4.5.25). ``rooms_required`` narrows the answer to types that can supply the whole
party from their own inventory; mixing types to make up a number is a different
question -- one about allocation preferences rather than availability -- and is not
answered here.

**A result is still not a reservation.** Nothing is held, nothing is written, and a
room named here may be taken by another request a moment later. The booking
transaction and its exclusion constraint remain the only authority on inventory.

**The stay interval is half-open**, ``[check_in, check_out)``, matching booking creation and
the exclusion constraint exactly: a stay from the 10th to the 14th occupies the nights of the
10th, 11th, 12th and 13th, and a stay beginning on the 14th does not collide with it. Note
that this differs from the analytics date range, whose bounds are both inclusive -- the two
answer different questions and each matches the thing it describes.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class AvailableRoom(BaseModel):
    """One physical room that could be allocated for the requested stay."""

    model_config = ConfigDict(from_attributes=True)

    room_number: str
    floor: int | None = None


class AvailableRoomType(BaseModel):
    """A room type, and the rooms of it that are free for the requested stay.

    ``available_count`` is ``len(rooms)`` -- derived from the actual allocatable rooms rather
    than from a stored inventory figure, because a count kept anywhere else would be a second
    answer to the same question.
    """

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    max_occupancy: int
    standard_occupancy: int
    base_price: decimal.Decimal
    currency: str
    available_count: int
    #: How many of this type the caller asked for, when the request named types and
    #: counts (Stage 4.5.26). Absent for a search that named none, because there was
    #: no per-type request to report.
    requested_count: int | None = None
    rooms: list[AvailableRoom]


class AvailabilityResult(BaseModel):
    """The answer to one availability search.

    An empty ``room_types`` is a successful answer meaning "nothing free", not an error.
    """

    model_config = ConfigDict(from_attributes=True)

    hotel_public_id: uuid.UUID
    check_in: dt.date
    check_out: dt.date
    #: Half-open, so this is ``(check_out - check_in).days`` and never zero.
    nights: int = Field(description="Nights in the requested stay: check_out - check_in.")
    #: How many rooms of a SINGLE type the caller asked for (Stage 4.5.25).
    #: One by default, which is the question this endpoint answered before.
    rooms_required: int = 1
    #: Whether at least one room type can supply ``rooms_required`` rooms for the
    #: whole stay. False is a successful answer meaning the hotel cannot.
    sufficient: bool
    #: Free rooms across the types listed below -- which, when more than one room
    #: is required, are only the types that can supply the whole request.
    available_rooms: int
    room_types: list[AvailableRoomType]


__all__ = ["AvailabilityResult", "AvailableRoom", "AvailableRoomType"]
