"""Hotel persistence.

Every SQLAlchemy construct for the hotel aggregate lives here. Nothing above this layer
imports ``select``, touches ``Session`` directly, or knows that ``public_id`` is indexed --
which is what makes the query strategy changeable without touching the service or the router.

Nothing here commits. Transaction boundaries belong to the service (see ``HotelService``);
a repository that committed would make it impossible to compose two writes into one unit of
work later.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.hotel import Hotel


class HotelRepository:
    """Data access for the hotel aggregate."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, hotel: Hotel) -> Hotel:
        """Stage a new hotel and flush so the database assigns its keys and defaults.

        ``flush`` -- not ``commit`` -- sends the INSERT so that ``public_id``, ``id`` and the
        server-side timestamps are populated and any constraint violation surfaces here,
        while the enclosing transaction stays open for the service to finish or abandon.
        """
        self._session.add(hotel)
        self._session.flush()
        self._session.refresh(hotel)
        return hotel

    def get_by_public_id(self, public_id: uuid.UUID) -> Hotel | None:
        """Fetch one hotel by its external identifier, or None."""
        return self._session.scalars(
            select(Hotel).where(Hotel.public_id == public_id)
        ).one_or_none()

    def get_by_slug(self, slug: str) -> Hotel | None:
        """Fetch one hotel by slug, or None. Used to pre-empt the unique constraint."""
        return self._session.scalars(select(Hotel).where(Hotel.slug == slug)).one_or_none()

    def slug_exists(self, slug: str) -> bool:
        """Cheaper than fetching the row when only existence matters."""
        return (
            self._session.scalar(select(func.count()).select_from(Hotel).where(Hotel.slug == slug))
            or 0
        ) > 0

    def count(self) -> int:
        """Total hotels, for the pagination envelope."""
        return self._session.scalar(select(func.count()).select_from(Hotel)) or 0

    def list_page(self, *, limit: int, offset: int) -> list[Hotel]:
        """One page of hotels in a stable order.

        Ordered by ``name`` then ``id``. The ``id`` tiebreaker is not decoration: two hotels
        may share a name, and PostgreSQL gives no ordering guarantee among rows that compare
        equal. Without it the same row could appear on page 1 and page 2, or be skipped
        entirely, and the bug would only show up once the data grew.
        """
        return list(
            self._session.scalars(
                select(Hotel).order_by(Hotel.name.asc(), Hotel.id.asc()).limit(limit).offset(offset)
            ).all()
        )

    def apply_changes(self, hotel: Hotel, changes: dict[str, Any]) -> Hotel:
        """Apply a partial update to a managed instance and flush it.

        Only the keys present are written, so a PATCH leaves every other column alone.
        """
        for field, value in changes.items():
            setattr(hotel, field, value)
        self._session.flush()
        self._session.refresh(hotel)
        return hotel

    def delete(self, hotel: Hotel) -> None:
        """Delete with a Core statement, so the database's ON DELETE policy is authoritative.

        Deliberately NOT ``session.delete(hotel)``. The ORM's default cascade for a loaded
        relationship is "nullify": it would first issue ``UPDATE guests SET hotel_id = NULL``
        and only then delete the hotel. Against this schema that trips the NOT NULL
        constraint and reports 23502, so PostgreSQL's ``ON DELETE RESTRICT`` is never
        consulted -- and if that column were ever nullable, the ORM would silently orphan
        the children instead of refusing.

        A Core DELETE emits exactly one statement and lets the database decide, which is the
        whole point of having declared RESTRICT. The flush surfaces the refusal here, while
        the service still controls the transaction.
        """
        self._session.execute(sql_delete(Hotel).where(Hotel.id == hotel.id))
        self._session.flush()
        # The row is gone; detach the instance so the identity map cannot serve a ghost.
        self._session.expunge(hotel)


__all__ = ["HotelRepository"]
