"""Availability search.

Stage 4.5.10. Read-only: no insert, no update, no delete, no flush, no commit. It has no
transaction boundary for the same reason ``analytics`` and ``intelligence`` have none.

**There is one availability authority and it is the database.** The write side is
``excl_booking_rooms_room_no_overlap``, a GiST exclusion constraint over
``daterange(check_in_date, check_out_date, '[)')`` filtered to
:data:`INVENTORY_HOLDING_STATUSES`. This service does not re-implement that rule; it asks
:meth:`RoomRepository.available_in_hotel` for the same predicate, written against the same
``daterange`` and the same status constant, and does nothing to the answer but group it.

**A result is not a reservation.** Nothing is locked and nothing is held. Between this read
and a later booking, another transaction may take any room named here -- and when it does, the
exclusion constraint refuses the loser at write time, which is exactly where that decision
belongs. The search reduces failed booking attempts; it does not promise they cannot happen.
Adding a lock here would turn a browse into a reservation and would hold it for as long as a
user stared at a screen.

**What makes a room available**, in the order the query applies it:

* it belongs to the requested hotel -- asserted on the ROOM, since the room is what gets
  allocated, rather than inferred from its type;
* the room is ``is_active`` and its type is ``is_active``;
* the room's housekeeping status is not one of :data:`OUT_OF_SERVICE_ROOM_STATUSES`;
* and no allocation in an inventory-holding status overlaps the requested half-open range.

Pending, cancelled, no-show and checked-out allocations hold nothing, which is not a rule this
module states -- it is what ``INVENTORY_HOLDING_STATUSES`` already means, and Stage 4.5.7's
state machine is what moves a booking in and out of it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.models.hotel import Hotel
from app.models.room import Room, RoomType
from app.repositories.room import RoomRepository
from app.schemas.availability import (
    AvailabilityResult,
    AvailableRoom,
    AvailableRoomType,
)
from app.services.scope import HotelScopeResolver

#: An upper bound on the stay a single search may ask about, mirroring the spirit of the
#: analytics range cap: an unbounded window is an unbounded scan, and no real stay is longer.
MAX_STAY_NIGHTS = 366

#: The most rooms one search may ask for (Stage 4.5.25).
#:
#: Bounded for the same reason a page size is: the number is a client's, and an
#: unbounded one turns a cheap comparison into an invitation to ask nonsense. The
#: value is generous next to any real party -- a hotel that cannot seat a hundred
#: rooms of one type from its own inventory answers false, which is still an answer.
MAX_ROOMS_REQUIRED = 100

#: The most distinct room types one request may name (Stage 4.5.26).
#:
#: Bounded like every other client-supplied number here. A hotel with more types than
#: this in a single party is not a case this endpoint needs to answer, and an unbounded
#: list is an unbounded number of lookups.
MAX_ROOM_TYPE_DEMANDS = 20

#: How a mixed request names one type and its count: ``CODE:COUNT``.
DEMAND_SEPARATOR = ":"


@dataclass(frozen=True, slots=True)
class RoomTypeDemand:
    """How many rooms of one type a mixed request asks for.

    Stage 4.5.26. The code is the client's own vocabulary -- the same identifier the room
    type endpoints put in their paths -- and is resolved against THIS hotel, so a code
    belonging to another property is simply not a code here.
    """

    code: str
    count: int


def parse_demands(entries: Sequence[str]) -> list[RoomTypeDemand]:
    """Read ``["DLX:2", "SUI:1"]`` into demands, refusing anything else.

    Parsing lives here rather than at the edge because every refusal it can make is a
    domain refusal with a message a caller can act on -- a malformed entry, a repeated
    type, a count outside the bound -- and those belong with the other validation this
    service already does rather than in a router that is supposed to hold no rules.

    Note what is NOT checked here: whether the code names a real room type. That is a
    question about this hotel's catalogue, and it is asked -- and answered with the
    hotel's own 404 -- only after the hotel wall has admitted the caller.
    """
    if len(entries) > MAX_ROOM_TYPE_DEMANDS:
        raise ValidationError(
            f"A request may name at most {MAX_ROOM_TYPE_DEMANDS} room types; "
            f"this one names {len(entries)}."
        )

    demands: list[RoomTypeDemand] = []
    seen: set[str] = set()
    for entry in entries:
        code, separator, raw_count = entry.partition(DEMAND_SEPARATOR)
        code = code.strip().upper()
        if not separator or not code or not raw_count.strip().isdigit():
            # The offending entry is not echoed: it is client input, and the shared
            # handler drops rejected values everywhere else for the same reason.
            raise ValidationError("Each entry must be CODE:COUNT, for example DLX:2.")

        count = int(raw_count)
        if count < 1 or count > MAX_ROOMS_REQUIRED:
            raise ValidationError(f"Each room count must be between 1 and {MAX_ROOMS_REQUIRED}.")
        if code in seen:
            raise ValidationError("Each room type may be named only once.")

        seen.add(code)
        demands.append(RoomTypeDemand(code=code, count=count))
    return demands


class AvailabilitySearchService:
    """Answers which rooms a hotel could sell for a requested stay."""

    def __init__(
        self,
        session: Session,
        rooms: RoomRepository,
        scope: HotelScopeResolver,
    ) -> None:
        self._session = session
        self._rooms = rooms
        self._scope = scope

    def search(
        self,
        hotel_public_id: uuid.UUID,
        check_in: dt.date,
        check_out: dt.date,
        *,
        room_type_code: str | None = None,
        guests: int | None = None,
        rooms_required: int = 1,
        demands: Sequence[RoomTypeDemand] | None = None,
    ) -> AvailabilityResult:
        """Search one hotel's inventory for a stay.

        Resolution order is the project's usual one, and it is load-bearing: the hotel wall
        answers first, so an unknown or unreachable property is *not found* rather than the
        subject of an availability answer. The room-type filter is resolved through the same
        resolver, so a code belonging to another hotel is simply not a code here.

        Stage 4.5.25. ``rooms_required`` asks whether that many rooms OF ONE TYPE
        are free for the whole stay. It changes no predicate in the query: the
        rooms that come back are the same rooms, and the requirement is a
        comparison against how many of each type there are. Filtering after the
        grouping rather than in SQL is deliberate -- the row set is already bounded
        by the search, so no second query, no HAVING clause and no second
        definition of 'available' is introduced.

        Stage 4.5.26. ``demands`` asks the mixed question -- two Doubles AND one Suite --
        and it needs no allocation algorithm, because a room belongs to exactly one type.
        The types partition the inventory, so the demands are independent and the whole
        request is satisfiable exactly when each type can supply its own share. There is
        no packing to do and no room to assign twice; anything cleverer here would be
        solving a problem the schema has already solved.

        The two forms are alternatives, not layers: ``demands`` names the types, so it
        cannot be combined with ``room_type_code`` or ``rooms_required``, which are the
        single-type way of saying the same kind of thing.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        nights = self._require_stay(check_in, check_out)
        self._require_room_count(rooms_required)
        if demands:
            self._require_single_form(room_type_code, rooms_required)

        room_type_id: int | None = None
        if room_type_code is not None:
            room_type_id = self._scope.require_room_type(hotel, room_type_code).id

        rows = self._rooms.available_in_hotel(
            hotel.id,
            check_in,
            check_out,
            room_type_id=room_type_id,
            min_occupancy=guests,
        )

        grouped = self._group(rows)
        if demands:
            room_types = self._answer_mixed(hotel, grouped, demands)
            sufficient = all(
                entry.available_count >= (entry.requested_count or 0) for entry in room_types
            )
        else:
            room_types = [entry for entry in grouped if entry.available_count >= rooms_required]
            sufficient = bool(room_types)

        return AvailabilityResult(
            hotel_public_id=hotel.public_id,
            check_in=check_in,
            check_out=check_out,
            nights=nights,
            rooms_required=rooms_required,
            sufficient=sufficient,
            available_rooms=sum(entry.available_count for entry in room_types),
            room_types=room_types,
        )

    # --- internals ----------------------------------------------------------------------

    @staticmethod
    def _require_stay(check_in: dt.date, check_out: dt.date) -> int:
        """Validate the stay and return its night count.

        A zero-night stay is refused rather than answered. ``check_in == check_out`` is an
        empty ``daterange`` -- it overlaps nothing at all, so every room would come back
        available for a stay nobody can book, which is a confidently wrong answer rather than
        a harmless one. A reversed range is refused rather than swapped, exactly as the
        analytics range is: swapping answers a question the caller did not ask.
        """
        if check_out <= check_in:
            raise ValidationError("check_out must be later than check_in.")
        nights = (check_out - check_in).days
        if nights > MAX_STAY_NIGHTS:
            raise ValidationError(
                f"The requested stay spans {nights} nights; the maximum is {MAX_STAY_NIGHTS}."
            )
        return nights

    def _answer_mixed(
        self,
        hotel: Hotel,
        grouped: list[AvailableRoomType],
        demands: Sequence[RoomTypeDemand],
    ) -> list[AvailableRoomType]:
        """Report every NAMED type, whether or not it can supply its share.

        Deliberately not the single-type behaviour of omitting what cannot answer. There,
        the caller is searching and a type that falls short is noise; here the caller named
        the type, and dropping it would hide WHICH part of the request the hotel cannot
        meet -- the one thing a mixed answer is for.

        Every code is resolved through the scope resolver, so an unknown one is the hotel's
        own 404 rather than a silently empty line in the answer. A named type with nothing
        free is reported with a count of zero, which is a fact rather than an absence.
        """
        available = {entry.code: entry for entry in grouped}
        answered: list[AvailableRoomType] = []
        for demand in demands:
            room_type = self._scope.require_room_type(hotel, demand.code)
            entry = available.get(room_type.code)
            if entry is None:
                entry = AvailableRoomType(
                    code=room_type.code,
                    name=room_type.name,
                    max_occupancy=room_type.max_occupancy,
                    standard_occupancy=room_type.standard_occupancy,
                    base_price=room_type.base_price,
                    currency=room_type.currency,
                    available_count=0,
                    rooms=[],
                )
            entry.requested_count = demand.count
            answered.append(entry)
        return answered

    @staticmethod
    def _require_single_form(room_type_code: str | None, rooms_required: int) -> None:
        """A request names its types once, in one way.

        Refused rather than resolved by precedence: a caller who sent both meant something,
        and quietly honouring one of them would answer a question they did not ask.
        """
        if room_type_code is not None:
            raise ValidationError(
                "rooms and room_type_code cannot be combined: rooms already names the types."
            )
        if rooms_required != 1:
            raise ValidationError(
                "rooms and rooms_required cannot be combined: rooms already carries a "
                "count for each type."
            )

    @staticmethod
    def _require_room_count(rooms_required: int) -> None:
        """Validate the requested room count.

        Zero is refused rather than answered: a search for no rooms has no true
        answer, and returning every room for it would be confidently wrong in the
        same way a zero-night stay would be.
        """
        if rooms_required < 1:
            raise ValidationError("rooms_required must be at least 1.")
        if rooms_required > MAX_ROOMS_REQUIRED:
            raise ValidationError(
                f"rooms_required is {rooms_required}; the maximum is {MAX_ROOMS_REQUIRED}."
            )

    @staticmethod
    def _group(rows: list[tuple[Room, RoomType]]) -> list[AvailableRoomType]:
        """Group the rooms by their type, preserving the query's ordering.

        Grouped in Python from rows the database already ordered and joined -- not with a
        second query per type, and not by re-reading the type through a lazy relationship.
        """
        grouped: dict[str, AvailableRoomType] = {}
        for room, room_type in rows:
            entry = grouped.get(room_type.code)
            if entry is None:
                entry = AvailableRoomType(
                    code=room_type.code,
                    name=room_type.name,
                    max_occupancy=room_type.max_occupancy,
                    standard_occupancy=room_type.standard_occupancy,
                    base_price=room_type.base_price,
                    currency=room_type.currency,
                    available_count=0,
                    rooms=[],
                )
                grouped[room_type.code] = entry
            entry.rooms.append(AvailableRoom(room_number=room.room_number, floor=room.floor))
            entry.available_count = len(entry.rooms)
        return list(grouped.values())


__all__ = [
    "MAX_ROOMS_REQUIRED",
    "MAX_ROOM_TYPE_DEMANDS",
    "MAX_STAY_NIGHTS",
    "AvailabilitySearchService",
    "RoomTypeDemand",
    "parse_demands",
]
