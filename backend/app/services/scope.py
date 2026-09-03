"""Resolution of the parent hierarchy: hotel -> room type.

Every nested domain begins the same way -- turn the public identifiers in the URL into the
internal rows they name, refusing anything that does not exist. That logic lives here once
rather than in each service, so the rule "a child is reached only through its own parents"
has a single implementation to audit.

The resolver returns ORM rows and raises :class:`NotFoundError`. It performs no writes and
owns no transaction; services do both.
"""

from __future__ import annotations

import uuid

from app.core.errors import NotFoundError
from app.models.enums import HotelRole
from app.models.hotel import Hotel
from app.models.room import RoomType
from app.repositories.hotel import HotelRepository
from app.repositories.room_type import RoomTypeRepository
from app.services.authorization import HotelAccessPolicy


class HotelScopeResolver:
    """Resolves the hotel, and the room type within it, from their URL identifiers."""

    def __init__(
        self,
        hotels: HotelRepository,
        room_types: RoomTypeRepository,
        policy: HotelAccessPolicy,
    ) -> None:
        self._hotels = hotels
        self._room_types = room_types
        # Authorization is DELEGATED, not implemented here. The resolver still only resolves;
        # it consults one collaborator that owns the decision, which is why forty-eight call
        # sites and ten service signatures needed no change at all.
        self._policy = policy

    def require_hotel(self, hotel_public_id: uuid.UUID) -> Hotel:
        """Resolve the hotel and require the caller to be a member of it.

        Two steps, in this order: resolution then authorization. A hotel that does not exist
        and a hotel the caller has nothing to do with raise the SAME error with the same
        message, so the response cannot be used to discover which properties exist.
        """
        hotel = self._hotels.get_by_public_id(hotel_public_id)
        if hotel is None:
            raise NotFoundError("Hotel not found.")
        self._policy.authorize(hotel)
        return hotel

    def require_hotel_with_role(self, hotel_public_id: uuid.UUID, required: HotelRole) -> Hotel:
        """Resolve, require membership, and require a role of at least *required*.

        Used by the writing paths. The required level is declared by the router, because
        which role a given verb needs is a fact about the HTTP surface rather than about the
        hotel.
        """
        hotel = self.require_hotel(hotel_public_id)
        self._policy.require_role(hotel, required)
        return hotel

    @property
    def policy(self) -> HotelAccessPolicy:
        """The request's authorization policy, for services that resolve a hotel themselves."""
        return self._policy

    def require_room_type(self, hotel: Hotel, code: str) -> RoomType:
        """Resolve a room type **within this hotel**, or raise 404.

        The lookup is composite by construction, which is what makes hotel A's room type
        unreachable through hotel B's URL even when the codes are identical.
        """
        room_type = self._room_types.get_by_hotel_and_code(hotel.id, code)
        if room_type is None:
            raise NotFoundError("Room type not found for this hotel.")
        return room_type

    def require_hotel_and_room_type(
        self, hotel_public_id: uuid.UUID, room_type_code: str
    ) -> tuple[Hotel, RoomType]:
        """Resolve the whole parent chain in order.

        Order matters for the error a client sees: an unknown hotel reports the hotel, not
        the room type, so a caller can tell which half of the path is wrong.
        """
        hotel = self.require_hotel(hotel_public_id)
        return hotel, self.require_room_type(hotel, room_type_code)


__all__ = ["HotelScopeResolver"]
