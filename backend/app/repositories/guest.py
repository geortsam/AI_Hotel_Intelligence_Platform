"""Guest persistence.

**Every lookup is scoped by ``hotel_id``.** There is deliberately no
``get_by_public_id(public_id)`` here, even though ``public_id`` is globally unique and such a
method would "work": guests are hotel-scoped, and a query that can reach across properties is
a query that will eventually be called from the wrong place. The safest way to prevent that
is for the method not to exist.

The consequence is exactly what the domain wants -- a guest of hotel A is *not found* through
hotel B's URL, rather than found and then rejected.

Nothing here commits. Transaction boundaries belong to the service.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.guest import Guest


class GuestRepository:
    """Data access for the guest aggregate, always scoped to one hotel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, guest: Guest) -> Guest:
        """Stage a new guest and flush so the database assigns its keys and defaults.

        ``flush`` -- not ``commit`` -- so ``public_id`` and the server timestamps are
        populated and any constraint violation surfaces here, while the enclosing
        transaction stays open for the service to finish or abandon.
        """
        self._session.add(guest)
        self._session.flush()
        self._session.refresh(guest)
        return guest

    def get_by_hotel_and_public_id(self, hotel_id: int, public_id: uuid.UUID) -> Guest | None:
        """The scoped lookup. Both halves are required; there is no public-id-only variant."""
        return self._session.scalars(
            select(Guest).where(Guest.hotel_id == hotel_id, Guest.public_id == public_id)
        ).one_or_none()

    def count_for_hotel(self, hotel_id: int) -> int:
        """Total guests at one hotel, for the pagination envelope."""
        return (
            self._session.scalar(
                select(func.count()).select_from(Guest).where(Guest.hotel_id == hotel_id)
            )
            or 0
        )

    def list_page_for_hotel(self, hotel_id: int, *, limit: int, offset: int) -> list[Guest]:
        """One page of a hotel's guests in a stable order.

        Ordered by surname, then forename, then the internal id. Neither name is unique --
        two guests may share both -- so the id tiebreaker is what makes paging stable. It is
        used for ordering only and never leaves the database.
        """
        return list(
            self._session.scalars(
                select(Guest)
                .where(Guest.hotel_id == hotel_id)
                .order_by(Guest.last_name.asc(), Guest.first_name.asc(), Guest.id.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def apply_changes(self, guest: Guest, changes: dict[str, Any]) -> Guest:
        """Apply a partial update to a managed instance and flush it."""
        for field, value in changes.items():
            setattr(guest, field, value)
        self._session.flush()
        self._session.refresh(guest)
        return guest

    def delete(self, guest: Guest) -> None:
        """Delete with a Core statement, so the database's ON DELETE policies are authoritative.

        Two different policies reference guests, and both must be the database's decision:

        * ``bookings`` -- ON DELETE RESTRICT: a guest with bookings must not be deletable.
        * ``reviews``  -- ON DELETE SET NULL **that cannot fire**. The foreign key is
          composite, ``(guest_id, hotel_id)``, and PostgreSQL nulls *every* referencing
          column; ``reviews.hotel_id`` is NOT NULL, so the attempt raises 23502 and the
          delete is refused. Verified live in Stage 3B.8: a guest with a review is NOT
          deletable, contrary to the declared intent. ``GuestService.delete`` already
          reports it as a dependency conflict; the schema correction is a later stage.

        ``session.delete()`` would let the ORM apply its own nullify cascade first and
        pre-empt both. A Core DELETE emits one statement and lets PostgreSQL decide.
        """
        self._session.execute(sql_delete(Guest).where(Guest.id == guest.id))
        self._session.flush()
        # The row is gone; detach the instance so the identity map cannot serve a ghost.
        self._session.expunge(guest)


__all__ = ["GuestRepository"]
