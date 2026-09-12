"""Reads the pricing configuration a stay is priced from (Stage 4.5.23).

**One query, whatever the booking's shape.** A four-room, seven-night booking asks this
repository once and gets every room's configuration back; the engine above then prices
28 nights without touching the database again. Pricing that queried per night would be an
N+1 in the one place the platform can least afford one -- the write path of every booking.

**Two hotel predicates, not one.** The room is scoped, and so is its room type. Either alone
would be enough today, because ``fk_rooms_room_type_id_hotel_id_room_types`` already makes a
cross-hotel pairing unrepresentable; both are stated because this is a tenant boundary and it
should hold on its own terms rather than by appeal to a constraint declared elsewhere.

Returns primitives. The domain value objects live in :mod:`app.services.pricing`, and a
repository may not import a service -- so the rows are mapped there rather than here.
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.room import Room, RoomType


class PricingRepository:
    """Data access for pricing configuration. Reads only; never writes, never commits."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def room_type_rates_for_rooms(
        self, hotel_id: int, room_ids: Sequence[int]
    ) -> dict[int, tuple[str, decimal.Decimal, str]]:
        """Map room id -> (room type code, base price, currency) for one hotel.

        A room absent from the result is a room this hotel does not have. The caller decides
        what that means; this layer does not raise domain errors.
        """
        if not room_ids:
            return {}

        rows = (
            self._session.execute(
                select(Room.id, RoomType.code, RoomType.base_price, RoomType.currency)
                .join(RoomType, RoomType.id == Room.room_type_id)
                .where(
                    Room.hotel_id == hotel_id,
                    RoomType.hotel_id == hotel_id,
                    Room.id.in_(room_ids),
                )
            )
            .tuples()
            .all()
        )
        return {
            room_id: (code, base_price, currency) for room_id, code, base_price, currency in rows
        }


__all__ = ["PricingRepository"]
