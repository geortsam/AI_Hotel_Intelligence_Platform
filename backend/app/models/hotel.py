"""The tenant root."""

from __future__ import annotations

import decimal
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Numeric,
    SmallInteger,
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
    from app.models.finance import Expense, Revenue
    from app.models.guest import Guest
    from app.models.metrics import DailyHotelMetric
    from app.models.review import Review
    from app.models.room import Room, RoomType


class Hotel(TimestampMixin, Base):
    """A property. Every other operational row descends from exactly one hotel."""

    __tablename__ = "hotels"

    id: Mapped[int] = pk_column()
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)

    address_line1: Mapped[str] = mapped_column(Text, nullable=False)
    address_line2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)

    latitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))

    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)

    # IANA name, e.g. "Europe/Athens". Required: it is what converts an event timestamp into
    # a business date, which the daily metrics job cannot do without.
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'UTC'"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    star_rating: Mapped[int | None] = mapped_column(SmallInteger)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    room_types: Mapped[list[RoomType]] = relationship(back_populates="hotel")
    rooms: Mapped[list[Room]] = relationship(back_populates="hotel")
    guests: Mapped[list[Guest]] = relationship(back_populates="hotel")
    bookings: Mapped[list[Booking]] = relationship(back_populates="hotel")
    reviews: Mapped[list[Review]] = relationship(back_populates="hotel")
    revenue_entries: Mapped[list[Revenue]] = relationship(back_populates="hotel")
    expenses: Mapped[list[Expense]] = relationship(back_populates="hotel")
    daily_metrics: Mapped[list[DailyHotelMetric]] = relationship(back_populates="hotel")

    __table_args__ = (
        UniqueConstraint("slug", name="uq_hotels_slug"),
        UniqueConstraint("public_id", name="uq_hotels_public_id"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="country_code_format"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)", name="latitude_range"
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="longitude_range",
        ),
        CheckConstraint(
            "star_rating IS NULL OR (star_rating >= 1 AND star_rating <= 5)",
            name="star_rating_range",
        ),
        Index("ix_hotels_country_code_city", "country_code", "city"),
    )

    def __repr__(self) -> str:
        return f"<Hotel id={self.id} slug={self.slug!r}>"


__all__ = ["Hotel"]
