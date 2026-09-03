"""Guests. Hotel-scoped per approved decision 2."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, pk_column

if TYPE_CHECKING:
    from app.models.booking import Booking
    from app.models.hotel import Hotel
    from app.models.review import Review


class Guest(TimestampMixin, Base):
    """A person, stored once per property and referenced by every booking they make.

    No passport, national ID, document or payment-card fields exist here, by design
    (approved decision 13). Card data never enters this database at all -- ``payments``
    stores a processor reference and at most a last-four fragment.
    """

    __tablename__ = "guests"

    id: Mapped[int] = pk_column()
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )

    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional: walk-ins and phone bookings genuinely have neither.
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)

    country_code: Mapped[str | None] = mapped_column(String(2))
    preferred_language: Mapped[str | None] = mapped_column(String(2))
    date_of_birth: Mapped[dt.date | None] = mapped_column(Date)

    marketing_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    notes: Mapped[str | None] = mapped_column(Text)

    hotel: Mapped[Hotel] = relationship(back_populates="guests")
    bookings: Mapped[list[Booking]] = relationship(
        back_populates="guest",
        primaryjoin="Guest.id == foreign(Booking.guest_id)",
    )
    reviews: Mapped[list[Review]] = relationship(
        back_populates="guest",
        primaryjoin="Guest.id == foreign(Review.guest_id)",
    )

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_guests_public_id"),
        UniqueConstraint("id", "hotel_id", name="uq_guests_id_hotel_id"),
        # Partial unique: an address appears once per property, but many guests have none.
        Index(
            "uq_guests_hotel_id_email",
            "hotel_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        CheckConstraint(
            "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'", name="country_code_format"
        ),
        Index("ix_guests_hotel_id_last_name", "hotel_id", "last_name"),
    )


__all__ = ["Guest"]
