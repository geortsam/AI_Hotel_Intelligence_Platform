"""Room type persistence.

**Every lookup is scoped by ``hotel_id``.** There is no ``get_by_code(code)`` in this module
and there must not be: ``code`` is unique only *within* a hotel, so a query on code alone
could return another property's room type. Scoping at the repository makes cross-tenant
addressing impossible rather than merely unlikely -- the router cannot ask the wrong question
because the method does not exist.

Nothing here commits. Transaction boundaries belong to the service.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.booking import BookingRoom
from app.models.enums import INVENTORY_HOLDING_STATUSES
from app.models.room import Room, RoomType


class RoomTypeRepository:
    """Data access for the room-type aggregate, always scoped to one hotel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, room_type: RoomType) -> RoomType:
        """Stage a new room type and flush so defaults and server values are populated.

        ``flush`` -- not ``commit`` -- so a constraint violation surfaces here while the
        enclosing transaction stays open for the service to finish or abandon.
        """
        self._session.add(room_type)
        self._session.flush()
        self._session.refresh(room_type)
        return room_type

    def get_by_hotel_and_code(self, hotel_id: int, code: str) -> RoomType | None:
        """The composite lookup. Both halves are required; there is no code-only variant."""
        return self._session.scalars(
            select(RoomType).where(RoomType.hotel_id == hotel_id, RoomType.code == code)
        ).one_or_none()

    def code_exists(self, hotel_id: int, code: str) -> bool:
        """Whether this hotel already uses this code. Cheaper than fetching the row."""
        return (
            self._session.scalar(
                select(func.count())
                .select_from(RoomType)
                .where(RoomType.hotel_id == hotel_id, RoomType.code == code)
            )
            or 0
        ) > 0

    def count_for_hotel(self, hotel_id: int) -> int:
        """Total room types at one hotel, for the pagination envelope."""
        return (
            self._session.scalar(
                select(func.count()).select_from(RoomType).where(RoomType.hotel_id == hotel_id)
            )
            or 0
        )

    def list_page_for_hotel(self, hotel_id: int, *, limit: int, offset: int) -> list[RoomType]:
        """One page of a hotel's room types in a stable order.

        Ordered by ``code``, which is unique within a hotel and therefore a total order on
        its own -- no tiebreaker is needed here, unlike hotels, where names can repeat.
        """
        return list(
            self._session.scalars(
                select(RoomType)
                .where(RoomType.hotel_id == hotel_id)
                .order_by(RoomType.code.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def lock_for_update(self, room_type: RoomType) -> RoomType:
        """Re-read this room type under ``FOR NO KEY UPDATE``, refreshing it in place (Issue H1).

        Taken before a deactivation is decided. Every booking write holds ``FOR SHARE`` on the
        rooms it sells and their types (``BookingRepository.lock_rooms_for_sale``), and the two
        locks conflict, so a sale and a deactivation serialise. ``NO KEY``: the update changes no
        key, so the foreign-key checks of unrelated writes (``FOR KEY SHARE``) need not wait.
        ``populate_existing``, because the instance is already in the identity map.
        """
        return self._session.scalars(
            select(RoomType)
            .where(RoomType.id == room_type.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        ).one()

    def rooms_hold_stay_after(self, room_type_id: int, day: dt.date) -> bool:
        """Whether any room of this type holds a confirmed or checked-in stay ending after *day*.

        Issue H1: such a room type cannot be deactivated -- deactivating it takes every one of
        its rooms off sale. The same rule as ``RoomRepository.holds_stay_after``, per type.
        """
        return bool(
            self._session.scalar(
                select(
                    select(BookingRoom.id)
                    .join(Room, Room.id == BookingRoom.room_id)
                    .where(
                        Room.room_type_id == room_type_id,
                        BookingRoom.booking_status.in_(INVENTORY_HOLDING_STATUSES),
                        BookingRoom.check_out_date > day,
                    )
                    .exists()
                )
            )
        )

    def apply_changes(self, room_type: RoomType, changes: dict[str, Any]) -> RoomType:
        """Apply a partial update to a managed instance and flush it."""
        for field, value in changes.items():
            setattr(room_type, field, value)
        self._session.flush()
        self._session.refresh(room_type)
        return room_type

    def delete(self, room_type: RoomType) -> None:
        """Delete with a Core statement, so the database's ON DELETE policy is authoritative.

        Deliberately NOT ``session.delete()``. The ORM's default cascade for a loaded
        relationship is "nullify": it would issue ``UPDATE rooms SET room_type_id = NULL``
        first, which trips the NOT NULL constraint and reports 23502 instead of the RESTRICT
        refusal -- and would silently orphan rooms if that column were ever nullable. The
        same trap was found and fixed for hotels in Stage 3B.1.
        """
        self._session.execute(sql_delete(RoomType).where(RoomType.id == room_type.id))
        self._session.flush()
        # The row is gone; detach the instance so the identity map cannot serve a ghost.
        self._session.expunge(room_type)


__all__ = ["RoomTypeRepository"]
