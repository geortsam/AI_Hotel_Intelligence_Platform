"""Review API contracts.

**There is no ``public_id`` on ``reviews``**, and no migration was created to add one. The
frozen schema supplies two partial unique keys instead, and the API is shaped around the one
it can rely on:

* ``uq_reviews_booking_id`` -- UNIQUE (booking_id) WHERE booking_id IS NOT NULL. One review
  per stay. A review created through this API always carries a booking, so the booking's own
  ``public_id`` *is* the review's address.
* ``uq_reviews_source_external_review_id`` -- UNIQUE (source, external_review_id) WHERE
  external_review_id IS NOT NULL. The identity of a review harvested from an external
  platform. Note it is **global, not per-hotel** (verified live), so it is an ingestion key,
  not an addressing scheme this API hands to clients.

``reviews.id`` is never exposed, and neither is ``guest_id``, ``booking_id`` or ``hotel_id``.

**What a client may write is deliberately narrower than the table.** Creating a review
supplies the guest's content; moderating one touches only ``is_published`` and
``responded_at``, which are the two columns the schema provides for the hotel's own
operational state. The guest's words are not editable through this API.

``rating_normalized`` is a GENERATED ALWAYS column -- PostgreSQL rejects a direct write with
SQLSTATE 428C9 -- so it is response-only.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Mirrors ck_reviews_source_valid.
ReviewSourceLiteral = Literal[
    "direct", "booking_com", "tripadvisor", "google", "expedia", "airbnb", "other"
]
#: Mirrors ck_reviews_rating_scale_valid: rating_scale IN (5, 10). Booking.com scores out of
#: ten, TripAdvisor out of five; the scale is stored so the two are never averaged blindly.
RatingScaleLiteral = Literal[5, 10]

#: NUMERIC(4,2) with ck_reviews_rating_in_scale enforcing 0 <= rating <= rating_scale. The
#: upper half of that rule is cross-field and lives in the model validator below.
#:
#: ``decimal_places=2`` mirrors the column rather than the constraint: NUMERIC(4,2) would
#: silently round 4.567 to 4.57. Rejecting is better than quietly altering a rating.
RatingField = Annotated[decimal.Decimal, Field(ge=0, max_digits=4, decimal_places=2)]
#: ck_reviews_language_format: NULL, or exactly two lower-case letters.
LanguageField = Annotated[str, Field(pattern=r"^[a-z]{2}$")]


class ReviewBase(BaseModel):
    """The guest's content, plus the provenance columns the table carries."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    source: ReviewSourceLiteral = "direct"
    #: Set only for reviews imported from an external platform; the partial unique index on
    #: (source, external_review_id) makes re-import idempotent.
    external_review_id: Annotated[str, Field(max_length=200)] | None = None
    rating: RatingField
    rating_scale: RatingScaleLiteral = 5
    title: Annotated[str, Field(max_length=300)] | None = None
    #: Nullable in the table: rating-only reviews are extremely common.
    body: str | None = None
    language: LanguageField | None = None
    #: The display name shown on the review, which is not necessarily the guest's own name.
    reviewer_name: Annotated[str, Field(max_length=200)] | None = None
    review_date: dt.date

    @model_validator(mode="after")
    def _rating_must_fit_its_scale(self) -> ReviewBase:
        """Mirror ``ck_reviews_rating_in_scale``.

        Caught at the edge so a client gets a field-level message rather than an opaque
        conflict from a constraint it cannot see -- and so a rating far above the scale is
        rejected as invalid input instead of overflowing NUMERIC(4,2) with SQLSTATE 22003.
        """
        if self.rating > self.rating_scale:
            raise ValueError(f"rating must not exceed rating_scale ({self.rating_scale})")
        return self


class ReviewCreate(ReviewBase):
    """Payload for the review of a stay.

    Absent by design, and each for a schema reason rather than a stylistic one:

    * ``hotel_id`` / ``booking_id`` -- both are in the URL.
    * ``guest_id`` -- derived from the booking. ``bookings.guest_id`` is NOT NULL, so the
      author of a stay's review is already known; accepting it would only create a way to
      attribute a review to someone who did not stay.
    * ``rating_normalized`` -- GENERATED ALWAYS; PostgreSQL refuses a direct write.
    * ``responded_at`` -- the hotel's own state, and part of the moderation payload below.
      A review cannot have been answered before it exists.
    """

    #: The table defaults to true. Accepted here so an import can land a review already
    #: hidden, rather than publishing it and hiding it in a second request.
    is_published: bool = True


class ReviewModerationUpdate(BaseModel):
    """Payload for PATCH: the hotel's operational state, and nothing else.

    These are exactly the two columns the frozen schema provides for the property's own use.
    The rating, the title, the body and the reviewer's name are the guest's account of their
    stay; this API does not offer a way to rewrite it, and the table has no version history
    that would make such an edit recoverable.

    Every field is optional. An omitted field is left untouched; an explicit ``null`` clears
    the column, which is how a response is retracted.
    """

    model_config = ConfigDict(extra="forbid")

    is_published: bool | None = None
    responded_at: dt.datetime | None = None


class ReviewResponse(BaseModel):
    """What the API returns.

    ``booking_public_id`` and ``guest_public_id`` are nullable because the columns behind
    them are: the schema permits a review with no guest and no booking, which is how reviews
    harvested from external platforms are stored when the reviewer cannot be matched to a
    guest record. Such a review appears in the hotel's list and has no individual URL --
    stated here rather than papered over.

    No internal BIGINT appears: not ``reviews.id``, not ``hotel_id``, ``guest_id`` or
    ``booking_id``.
    """

    model_config = ConfigDict(from_attributes=True)

    hotel_public_id: uuid.UUID
    #: Null for a review that is not attached to a stay. Present, this is the review's URL.
    booking_public_id: uuid.UUID | None
    #: Null for a review whose reviewer was never matched to a guest record.
    guest_public_id: uuid.UUID | None
    source: ReviewSourceLiteral
    external_review_id: str | None
    rating: decimal.Decimal
    rating_scale: int
    #: GENERATED ALWAYS AS (rating / rating_scale). Read-only, and the only value that can be
    #: compared across sources that score out of five and out of ten.
    rating_normalized: decimal.Decimal
    title: str | None
    body: str | None
    language: str | None
    reviewer_name: str | None
    review_date: dt.date
    is_published: bool
    responded_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


__all__ = [
    "LanguageField",
    "RatingField",
    "RatingScaleLiteral",
    "ReviewBase",
    "ReviewCreate",
    "ReviewModerationUpdate",
    "ReviewResponse",
    "ReviewSourceLiteral",
]
