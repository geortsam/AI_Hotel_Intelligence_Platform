"""Physical room API contracts.

``rooms`` has no ``public_id`` and none is being added. ``room_number`` is the natural
identifier, backed by ``UNIQUE (hotel_id, room_number)``.

**That uniqueness is per HOTEL, not per room type.** The ``room-types`` segment in the URL is
therefore an assertion the room must satisfy, not part of its key: number ``101`` identifies
exactly one room in a hotel, whatever its type. A consequence worth stating plainly -- two
room types at one property cannot both own a room numbered ``101``, even though their URLs
read like separate collections.

``status`` is restricted to the five values the database CHECK constraint already allows. No
new state is invented, and none is booking-driven: ``status`` is the room's *current
operational* state, not its availability over time.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Mirrors RoomStatus in app.models.enums, which generates the database CHECK constraint.
#: Spelled out as a Literal so FastAPI documents the allowed values and rejects the rest at
#: the edge with a field-level message.
RoomStatusLiteral = Literal["available", "occupied", "cleaning", "maintenance", "out_of_order"]

#: "12A" and "P-3" are real room numbers, so this is TEXT, not an integer. Constrained to a
#: URL-safe shape because the value appears in the path.
RoomNumberField = Annotated[
    str, Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9][A-Z0-9._-]*$")
]
FloorField = Annotated[int, Field(ge=-10, le=200)]


class RoomCreate(BaseModel):
    """Payload for POST .../rooms.

    Neither the hotel nor the room type appears in the body: both come from the URL path.
    Accepting them here as well would let the two disagree, and would hand the client a way
    to name a room type the path never authorised.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    room_number: RoomNumberField
    floor: FloorField | None = None
    status: RoomStatusLiteral = "available"
    notes: str | None = None
    is_active: bool = True

    @field_validator("room_number", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        """Canonicalise to upper case.

        PostgreSQL's unique constraint is case-sensitive, so without this ``101a`` and
        ``101A`` would be two different rooms in the same corridor.
        """
        return value.upper() if isinstance(value, str) else value


class RoomUpdate(BaseModel):
    """Payload for PATCH. Every field optional; omitted fields are left untouched.

    ``room_number`` is absent -- it is the URL identity, exactly as ``slug`` is for a hotel
    and ``code`` for a room type. The room type is absent too: moving a room between types
    would change its parent path, and the API deliberately never exposes ``room_type_id``.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    floor: FloorField | None = None
    status: RoomStatusLiteral | None = None
    notes: str | None = None
    is_active: bool | None = None


class RoomResponse(BaseModel):
    """What the API returns.

    Carries ``hotel_public_id`` and ``room_type_code`` so a client can rebuild the room's own
    URL from the payload alone. No internal key appears: not ``rooms.id``, not ``hotel_id``,
    not ``room_type_id``.
    """

    model_config = ConfigDict(from_attributes=True)

    hotel_public_id: uuid.UUID
    room_type_code: str
    room_number: str
    floor: int | None
    status: RoomStatusLiteral
    notes: str | None
    is_active: bool
    created_at: dt.datetime
    updated_at: dt.datetime


__all__ = ["RoomCreate", "RoomResponse", "RoomStatusLiteral", "RoomUpdate"]
