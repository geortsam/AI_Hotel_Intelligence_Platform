"""Room types, physical rooms, and the amenity catalogue."""

from __future__ import annotations

import decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, pk_column
from app.models.enums import RoomStatus

if TYPE_CHECKING:
    from app.models.booking import BookingRoom
    from app.models.hotel import Hotel


class Amenity(Base):
    """A global amenity catalogue.

    Deliberately NOT hotel-scoped: "sea view" must mean the same thing across the portfolio,
    or cross-hotel comparison is meaningless.
    """

    __tablename__ = "amenities"

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)

    room_types: Mapped[list[RoomType]] = relationship(
        secondary="room_type_amenities", back_populates="amenities"
    )

    __table_args__ = (UniqueConstraint("code", name="uq_amenities_code"),)


class RoomTypeAmenity(Base):
    """Junction table. The pair is the identity -- no surrogate key."""

    __tablename__ = "room_type_amenities"

    room_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("room_types.id", ondelete="CASCADE"), primary_key=True
    )
    amenity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("amenities.id", ondelete="RESTRICT"), primary_key=True
    )

    __table_args__ = (
        # The composite PK already serves lookup by room type; this serves the reverse
        # question, "which room types have a sea view?".
        Index("ix_room_type_amenities_amenity_id", "amenity_id"),
    )


class RoomType(TimestampMixin, Base):
    """The sellable product. Pricing and capacity live here, not on physical rooms."""

    __tablename__ = "room_types"

    id: Mapped[int] = pk_column()
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    max_occupancy: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    standard_occupancy: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    bed_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    bed_configuration: Mapped[str | None] = mapped_column(Text)
    size_sqm: Mapped[decimal.Decimal | None] = mapped_column(Numeric(6, 2))

    # The published rack rate: the INPUT to pricing, never what a guest actually paid.
    # The charged amount lives in booking_room_nights.rate, which is why editing this is
    # safe and cannot rewrite history.
    base_price: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    hotel: Mapped[Hotel] = relationship(back_populates="room_types")
    rooms: Mapped[list[Room]] = relationship(
        back_populates="room_type",
        primaryjoin="RoomType.id == foreign(Room.room_type_id)",
    )
    amenities: Mapped[list[Amenity]] = relationship(
        secondary="room_type_amenities", back_populates="room_types"
    )

    __table_args__ = (
        UniqueConstraint("hotel_id", "code", name="uq_room_types_hotel_id_code"),
        # Not a business rule -- the composite-FK target that stops a room from being
        # assigned to another hotel's room type.
        UniqueConstraint("id", "hotel_id", name="uq_room_types_id_hotel_id"),
        CheckConstraint("max_occupancy > 0", name="max_occupancy_positive"),
        CheckConstraint(
            "standard_occupancy > 0 AND standard_occupancy <= max_occupancy",
            name="standard_occupancy_range",
        ),
        CheckConstraint("bed_count > 0", name="bed_count_positive"),
        CheckConstraint("size_sqm IS NULL OR size_sqm > 0", name="size_sqm_positive"),
        CheckConstraint("base_price >= 0", name="base_price_non_negative"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
    )


class Room(TimestampMixin, Base):
    """A physical, allocatable unit. This is what the exclusion constraint protects."""

    __tablename__ = "rooms"

    id: Mapped[int] = pk_column()
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )
    room_type_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # TEXT, not INTEGER: "12A" and "P-3" are real room numbers.
    room_number: Mapped[str] = mapped_column(Text, nullable=False)
    floor: Mapped[int | None] = mapped_column(SmallInteger)

    # Current operational state, NOT availability over time. A room being 'available' today
    # says nothing about next Tuesday -- that question is answered by booking_rooms.
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{RoomStatus.AVAILABLE.value}'")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    hotel: Mapped[Hotel] = relationship(back_populates="rooms")
    # Composite FKs carry hotel_id for tenant isolation, which makes the join path ambiguous
    # to the ORM. primaryjoin + foreign() names the identifying column explicitly; the
    # mirrored columns are set by the writer, not inferred by the relationship.
    room_type: Mapped[RoomType] = relationship(
        back_populates="rooms",
        primaryjoin="RoomType.id == foreign(Room.room_type_id)",
    )
    booking_rooms: Mapped[list[BookingRoom]] = relationship(
        back_populates="room",
        primaryjoin="Room.id == foreign(BookingRoom.room_id)",
    )

    __table_args__ = (
        # The multi-hotel guarantee: a room cannot take a room type from another hotel.
        ForeignKeyConstraint(
            ["room_type_id", "hotel_id"],
            ["room_types.id", "room_types.hotel_id"],
            name="fk_rooms_room_type_id_hotel_id_room_types",
            ondelete="RESTRICT",
        ),
        # Room numbers are unique within a property, not globally.
        UniqueConstraint("hotel_id", "room_number", name="uq_rooms_hotel_id_room_number"),
        UniqueConstraint("id", "hotel_id", name="uq_rooms_id_hotel_id"),
        CheckConstraint(f"status IN ({RoomStatus.sql_in_list()})", name="status_valid"),
        Index("ix_rooms_hotel_id_room_type_id", "hotel_id", "room_type_id"),
    )


__all__ = ["Amenity", "Room", "RoomType", "RoomTypeAmenity"]
