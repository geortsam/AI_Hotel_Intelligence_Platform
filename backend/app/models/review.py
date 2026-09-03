"""Guest reviews from any channel.

No sentiment columns exist here, deliberately (approved decision 12). A sentiment score is a
model output that is only meaningful alongside the model version that produced it; a column
on this table has nowhere to record that, retraining would mass-UPDATE transactional rows,
and two models scoring the same review cannot both fit in one column. Scores will live in a
separate versioned table when the sentiment stage arrives.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, pk_column
from app.models.enums import ReviewSource

if TYPE_CHECKING:
    from app.models.booking import Booking
    from app.models.guest import Guest
    from app.models.hotel import Hotel


class Review(TimestampMixin, Base):
    """A review. The hotel is always known; the guest and booking often are not."""

    __tablename__ = "reviews"

    id: Mapped[int] = pk_column()
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    # Null for reviews harvested from external platforms, where the reviewer cannot be
    # matched to a guest record and the stay cannot be matched to a booking.
    guest_id: Mapped[int | None] = mapped_column(BigInteger)
    booking_id: Mapped[int | None] = mapped_column(BigInteger)

    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'direct'"))
    # Necessary for idempotent re-import: without a stable per-source identifier there is no
    # way to tell a new review from the same review fetched again, and duplicates skew every
    # aggregate and every model trained on them.
    external_review_id: Mapped[str | None] = mapped_column(Text)

    rating: Mapped[decimal.Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    # Booking.com uses 10, TripAdvisor uses 5. Averaging 8/10 with 4/5 is meaningless, so the
    # scale is stored alongside the raw value and a comparable ratio is generated from both.
    rating_scale: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("5")
    )
    rating_normalized: Mapped[decimal.Decimal] = mapped_column(
        Numeric(5, 4), Computed("rating / rating_scale", persisted=True)
    )

    title: Mapped[str | None] = mapped_column(Text)
    # Nullable: rating-only reviews are extremely common.
    body: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(2))
    reviewer_name: Mapped[str | None] = mapped_column(Text)

    review_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    hotel: Mapped[Hotel] = relationship(back_populates="reviews")
    guest: Mapped[Guest | None] = relationship(
        back_populates="reviews",
        primaryjoin="foreign(Review.guest_id) == Guest.id",
    )
    booking: Mapped[Booking | None] = relationship(
        back_populates="reviews",
        primaryjoin="foreign(Review.booking_id) == Booking.id",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["guest_id", "hotel_id"],
            ["guests.id", "guests.hotel_id"],
            name="fk_reviews_guest_id_hotel_id_guests",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["booking_id", "hotel_id"],
            ["bookings.id", "bookings.hotel_id"],
            name="fk_reviews_booking_id_hotel_id_bookings",
            ondelete="SET NULL",
        ),
        CheckConstraint(f"source IN ({ReviewSource.sql_in_list()})", name="source_valid"),
        CheckConstraint("rating_scale IN (5, 10)", name="rating_scale_valid"),
        CheckConstraint("rating >= 0 AND rating <= rating_scale", name="rating_in_scale"),
        CheckConstraint("language IS NULL OR language ~ '^[a-z]{2}$'", name="language_format"),
        # Idempotent ingestion from external platforms.
        Index(
            "uq_reviews_source_external_review_id",
            "source",
            "external_review_id",
            unique=True,
            postgresql_where=text("external_review_id IS NOT NULL"),
        ),
        # One review per stay.
        Index(
            "uq_reviews_booking_id",
            "booking_id",
            unique=True,
            postgresql_where=text("booking_id IS NOT NULL"),
        ),
        Index("ix_reviews_hotel_id_review_date", "hotel_id", text("review_date DESC")),
        Index("ix_reviews_hotel_id_source", "hotel_id", "source"),
    )


__all__ = ["Review"]
