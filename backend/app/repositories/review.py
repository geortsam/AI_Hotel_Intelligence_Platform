"""Review persistence.

``reviews`` has no ``public_id``, so there is no identifier a lookup could key on by itself.
Every method here therefore takes ``hotel_id``, and the single-row lookup takes the booking
as well. There is no ``get_by_id``: the internal key is not an address, and a method that
accepted one would be the first step towards putting it in a URL.

The hotel filter is not redundant next to the booking filter. ``uq_reviews_booking_id`` makes
``booking_id`` unique on its own, so a lookup by booking alone would succeed across tenants
if the caller ever obtained another property's booking id. Both are required, always.

There is no delete method. ``is_published`` is the schema's own withdrawal mechanism.

Nothing here commits.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.guest import Guest
from app.models.review import Review


class ReviewRepository:
    """Data access for reviews, always scoped to one hotel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, review: Review) -> Review:
        """Stage a review and flush so the database assigns its defaults.

        ``flush`` -- not ``commit`` -- so ``rating_normalized`` is computed and any
        constraint violation surfaces while the service still owns the transaction.
        """
        self._session.add(review)
        self._session.flush()
        self._session.refresh(review)
        return review

    def apply_changes(self, review: Review, changes: dict[str, Any]) -> Review:
        """Apply a partial update to a managed instance and flush it."""
        for field, value in changes.items():
            setattr(review, field, value)
        self._session.flush()
        self._session.refresh(review)
        return review

    def get_by_hotel_and_booking(self, hotel_id: int, booking_id: int) -> Review | None:
        """The stay's review, or None. Both scopes are required; there is no variant that
        takes only one."""
        return self._session.scalars(
            select(Review).where(Review.hotel_id == hotel_id, Review.booking_id == booking_id)
        ).one_or_none()

    def count_for_hotel(
        self, hotel_id: int, *, source: str | None = None, is_published: bool | None = None
    ) -> int:
        """Total reviews at one hotel under the same filters as the page, for the envelope."""
        return (
            self._session.scalar(
                self._filtered(
                    select(func.count()).select_from(Review), hotel_id, source, is_published
                )
            )
            or 0
        )

    def list_page_for_hotel(
        self,
        hotel_id: int,
        *,
        limit: int,
        offset: int,
        source: str | None = None,
        is_published: bool | None = None,
    ) -> list[Review]:
        """One page of a hotel's reviews, newest first.

        ``review_date DESC`` matches ``ix_reviews_hotel_id_review_date``, which the schema
        declares in exactly that direction. ``id`` breaks ties -- a date is not unique, and
        without a total order a row can appear on two pages. It is used for ordering only and
        never leaves.
        """
        statement = self._filtered(select(Review), hotel_id, source, is_published)
        return list(
            self._session.scalars(
                statement.order_by(Review.review_date.desc(), Review.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def booking_public_ids_for(self, booking_ids: list[int]) -> dict[int, uuid.UUID]:
        """Map internal booking id -> public_id, for rendering a page of reviews.

        One query for the whole page rather than a lazy hop per row.
        """
        if not booking_ids:
            return {}
        rows = (
            self._session.execute(
                select(Booking.id, Booking.public_id).where(Booking.id.in_(booking_ids))
            )
            .tuples()
            .all()
        )
        return dict(rows)

    def guest_public_ids_for(self, guest_ids: list[int]) -> dict[int, uuid.UUID]:
        """Map internal guest id -> public_id, for rendering a page of reviews."""
        if not guest_ids:
            return {}
        rows = (
            self._session.execute(select(Guest.id, Guest.public_id).where(Guest.id.in_(guest_ids)))
            .tuples()
            .all()
        )
        return dict(rows)

    @staticmethod
    def _filtered(
        statement: Select[Any], hotel_id: int, source: str | None, is_published: bool | None
    ) -> Select[Any]:
        """Apply the hotel scope and the optional filters to count and page identically.

        Shared so the two can never disagree about what is being counted. ``hotel_id`` is
        applied here and is not optional.
        """
        statement = statement.where(Review.hotel_id == hotel_id)
        if source is not None:
            statement = statement.where(Review.source == source)
        if is_published is not None:
            statement = statement.where(Review.is_published == is_published)
        return statement


__all__ = ["ReviewRepository"]
