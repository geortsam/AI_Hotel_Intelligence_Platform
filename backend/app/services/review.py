"""Review business logic and unit-of-work boundaries.

**What this service enforces, and where each rule comes from:**

* One review per stay -- ``uq_reviews_booking_id``. Not re-implemented as a check-then-insert;
  the insert is attempted and the unique violation is translated. Two concurrent submissions
  cannot both win.
* A review's guest is the booking's guest. ``bookings.guest_id`` is NOT NULL, so this is a
  derivation from the row already resolved, not an invented rule -- and it removes the only
  way a client could have attributed a review to someone who did not stay.
* Everything is reached through the hotel. The composite foreign keys already refuse a review
  whose guest or booking belongs to another property (verified live, SQLSTATE 23503), and the
  repository's scoping means such a row is never constructed in the first place.

**What it deliberately does NOT do**, because the frozen schema does not say it:

* It does not require ``review_date`` to fall after the stay, or indeed anywhere near it. No
  constraint relates the two; a review dated years before its booking is accepted by the
  database.
* It does not link ``guest_id`` to ``booking_id`` beyond the derivation above. The schema
  permits a review naming a guest who did not make the booking, provided both belong to the
  hotel.
* It does not treat ``is_published`` or ``responded_at`` as a workflow. They are two
  independent columns with no CHECK relating them to each other or to anything else, and no
  state machine is imposed on them.

**PII.** A review carries ``reviewer_name``, ``title`` and ``body`` -- free text written by a
guest about their stay. None of it reaches a log line or an error message. ``_translate``
logs the SQLSTATE and the constraint name only, and never ``exc_info``, because the driver
message quotes the offending row.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    GENERIC_CONFLICT_MESSAGE,
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    NotFoundError,
    constraint_name_of,
    sqlstate_of,
)
from app.models.booking import Booking
from app.models.hotel import Hotel
from app.models.review import Review
from app.repositories.booking import BookingRepository
from app.repositories.review import ReviewRepository
from app.schemas.common import Page
from app.schemas.review import ReviewCreate, ReviewModerationUpdate, ReviewResponse
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: UNIQUE (booking_id) WHERE booking_id IS NOT NULL -- the schema's "one review per stay".
ONE_PER_BOOKING_CONSTRAINT = "uq_reviews_booking_id"
#: UNIQUE (source, external_review_id) WHERE external_review_id IS NOT NULL. Global rather
#: than per-hotel, verified live: the same pair at a different property still collides.
EXTERNAL_ID_CONSTRAINT = "uq_reviews_source_external_review_id"

#: Declared at module scope on purpose: inside the class, ``list`` is the name of a method,
#: so ``list[Review]`` there resolves to that method rather than to the builtin.
type ReviewRows = list[Review]
type ReviewResponses = list[ReviewResponse]


class ReviewService:
    """Domain operations on reviews, always within one hotel."""

    def __init__(
        self,
        session: Session,
        repository: ReviewRepository,
        bookings: BookingRepository,
        scope: HotelScopeResolver,
    ) -> None:
        self._session = session
        self._repository = repository
        self._bookings = bookings
        self._scope = scope

    # --- reads --------------------------------------------------------------------------

    def list(
        self,
        hotel_public_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
        source: str | None = None,
        is_published: bool | None = None,
    ) -> Page[ReviewResponse]:
        """One page of a hotel's reviews, newest first.

        This is the only place a review with no booking is visible: such a review has no
        individual URL, because the schema gives it no identifier that could form one.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        total = self._repository.count_for_hotel(hotel.id, source=source, is_published=is_published)
        rows = self._repository.list_page_for_hotel(
            hotel.id,
            limit=page_size,
            offset=(page - 1) * page_size,
            source=source,
            is_published=is_published,
        )
        return Page.build(
            items=self._to_responses(rows, hotel),
            total=total,
            page=page,
            page_size=page_size,
        )

    def get(self, hotel_public_id: uuid.UUID, booking_public_id: uuid.UUID) -> ReviewResponse:
        """The review of one stay, or raise 404."""
        hotel, booking = self._require_booking(hotel_public_id, booking_public_id)
        review = self._require_review(hotel, booking)
        return self._to_response(review, hotel, booking.public_id)

    # --- writes -------------------------------------------------------------------------

    def create(
        self,
        hotel_public_id: uuid.UUID,
        booking_public_id: uuid.UUID,
        payload: ReviewCreate,
    ) -> ReviewResponse:
        """Record the review of a stay and commit.

        ``guest_id`` comes from the booking. No check for an existing review is performed
        first: ``uq_reviews_booking_id`` is authoritative, and a check-then-insert would
        leave a race between the two statements.
        """
        hotel, booking = self._require_booking(hotel_public_id, booking_public_id)

        review = Review(
            hotel_id=hotel.id,
            booking_id=booking.id,
            guest_id=booking.guest_id,
            **payload.model_dump(),
        )
        try:
            created = self._repository.add(review)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._to_response(created, hotel, booking.public_id)

    def moderate(
        self,
        hotel_public_id: uuid.UUID,
        booking_public_id: uuid.UUID,
        payload: ReviewModerationUpdate,
    ) -> ReviewResponse:
        """Publish, unpublish or mark a review answered, and commit.

        Only ``is_published`` and ``responded_at`` can be reached from here -- the schema
        offers no other column for the property's own state, and the guest's content is not
        this endpoint's to rewrite. Omitted fields are untouched; an explicit null clears.
        """
        hotel, booking = self._require_booking(hotel_public_id, booking_public_id)
        review = self._require_review(hotel, booking)
        changes: dict[str, Any] = payload.model_dump(exclude_unset=True)

        if not changes:
            return self._to_response(review, hotel, booking.public_id)

        try:
            updated = self._repository.apply_changes(review, changes)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._to_response(updated, hotel, booking.public_id)

    # --- internals ----------------------------------------------------------------------

    def _require_booking(
        self, hotel_public_id: uuid.UUID, booking_public_id: uuid.UUID
    ) -> tuple[Hotel, Booking]:
        """Resolve hotel then booking, in that order.

        Order matters for the error a client sees: an unknown hotel reports the hotel, so a
        caller can tell which part of the path is wrong.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        booking = self._bookings.get_by_hotel_and_public_id(hotel.id, booking_public_id)
        if booking is None:
            raise NotFoundError("Booking not found for this hotel.")
        return hotel, booking

    def _require_review(self, hotel: Hotel, booking: Booking) -> Review:
        """Resolve the stay's review, scoped by hotel *and* booking, or raise 404."""
        review = self._repository.get_by_hotel_and_booking(hotel.id, booking.id)
        if review is None:
            raise NotFoundError("This booking has no review.")
        return review

    def _to_response(
        self, review: Review, hotel: Hotel, booking_public_id: uuid.UUID | None
    ) -> ReviewResponse:
        """Build one response when the booking is already known."""
        guest_public_id = None
        if review.guest_id is not None:
            guest_public_id = self._repository.guest_public_ids_for([review.guest_id]).get(
                review.guest_id
            )
        return self._render(review, hotel, booking_public_id, guest_public_id)

    def _to_responses(self, reviews: ReviewRows, hotel: Hotel) -> ReviewResponses:
        """Build a whole page, resolving the parents' public identifiers in two queries
        rather than two per row."""
        bookings = self._repository.booking_public_ids_for(
            [r.booking_id for r in reviews if r.booking_id is not None]
        )
        guests = self._repository.guest_public_ids_for(
            [r.guest_id for r in reviews if r.guest_id is not None]
        )
        return [
            self._render(
                review,
                hotel,
                bookings.get(review.booking_id) if review.booking_id is not None else None,
                guests.get(review.guest_id) if review.guest_id is not None else None,
            )
            for review in reviews
        ]

    @staticmethod
    def _render(
        review: Review,
        hotel: Hotel,
        booking_public_id: uuid.UUID | None,
        guest_public_id: uuid.UUID | None,
    ) -> ReviewResponse:
        """The one place a Review row becomes a response. No internal key is copied across."""
        return ReviewResponse.model_validate(
            {
                "hotel_public_id": hotel.public_id,
                "booking_public_id": booking_public_id,
                "guest_public_id": guest_public_id,
                "source": review.source,
                "external_review_id": review.external_review_id,
                "rating": review.rating,
                "rating_scale": review.rating_scale,
                "rating_normalized": review.rating_normalized,
                "title": review.title,
                "body": review.body,
                "language": review.language,
                "reviewer_name": review.reviewer_name,
                "review_date": review.review_date,
                "is_published": review.is_published,
                "responded_at": review.responded_at,
                "created_at": review.created_at,
                "updated_at": review.updated_at,
            }
        )

    def _translate(self, exc: IntegrityError) -> Exception:
        """Turn a database integrity error into a domain error, leaking nothing.

        No ``exc_info``: the driver message for a failed review insert quotes the row, which
        means the guest's name and the text of their review. The SQLSTATE and the constraint
        name are enough to decide what to say, and are the only things recorded.
        """
        state = sqlstate_of(exc)
        constraint = constraint_name_of(exc)
        logger.warning("Review integrity error (sqlstate=%s, constraint=%s)", state, constraint)

        if state == SQLSTATE_UNIQUE_VIOLATION:
            if constraint == ONE_PER_BOOKING_CONSTRAINT:
                return ConflictError("This booking already has a review.")
            if constraint == EXTERNAL_ID_CONSTRAINT:
                # The identifier is not quoted back; the client already sent it.
                return ConflictError(
                    "A review with this external identifier has already been recorded for "
                    "this source."
                )
            return ConflictError("That value is already taken by another review.")
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a review constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("The review refers to a record that does not exist here.")
        return ConflictError(GENERIC_CONFLICT_MESSAGE)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "EXTERNAL_ID_CONSTRAINT",
    "MAX_PAGE_SIZE",
    "ONE_PER_BOOKING_CONSTRAINT",
    "ReviewService",
]
