"""The booking chain: Booking -> BookingRoom -> BookingRoomNight.

This module carries the two most important correctness properties in the schema:

* **Overlap protection** -- a physical room can never be held by two active bookings whose
  date ranges overlap. Enforced by a partial GiST ``EXCLUDE`` constraint on ``booking_rooms``,
  never by application-level check-then-insert, which races under READ COMMITTED.
* **Mirror integrity** -- ``booking_rooms`` and ``booking_room_nights`` both copy stay dates
  from their parent so that constraints can reference them. Every copy is anchored by a
  composite foreign key with ``ON UPDATE CASCADE``, so the copies *cannot* drift.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
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
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, pk_column
from app.models.enums import INVENTORY_HOLDING_STATUSES, BookingSource, BookingStatus

if TYPE_CHECKING:
    from app.models.finance import Revenue
    from app.models.guest import Guest
    from app.models.hotel import Hotel
    from app.models.payment import Payment
    from app.models.review import Review
    from app.models.room import Room

_HOLDING_SQL = ", ".join(f"'{status}'" for status in INVENTORY_HOLDING_STATUSES)


class Booking(TimestampMixin, Base):
    """The reservation agreement: the commercial and temporal envelope of a stay."""

    __tablename__ = "bookings"

    id: Mapped[int] = pk_column()
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )
    guest_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    reference: Mapped[str] = mapped_column(Text, nullable=False)

    # DATE, not timestamp: a hotel night is a calendar concept in the property's local terms.
    # The stay is the half-open interval [check_in_date, check_out_date).
    check_in_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    check_out_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))

    adults: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    children: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))

    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'direct'"))
    channel_reference: Mapped[str | None] = mapped_column(Text)

    # The contracted total. NOT defined as SUM(booking_room_nights.rate): discounts,
    # packages, taxes and fees legitimately break that equality (approved decision 10).
    total_amount: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    special_requests: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)

    # When the guest actually reserved -- distinct from created_at, which is when the row
    # entered THIS database. For migrated or OTA-imported bookings they differ by months,
    # and lead time (check_in_date - booked_at) is a core demand-forecasting feature.
    booked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    hotel: Mapped[Hotel] = relationship(back_populates="bookings")
    # Composite FKs carry hotel_id for tenant isolation, which makes the join path ambiguous
    # to the ORM. primaryjoin + foreign() names the identifying column explicitly; the
    # mirrored columns are set by the writer, not inferred by the relationship.
    guest: Mapped[Guest] = relationship(
        back_populates="bookings",
        primaryjoin="foreign(Booking.guest_id) == Guest.id",
    )
    booking_rooms: Mapped[list[BookingRoom]] = relationship(
        back_populates="booking",
        cascade="all, delete-orphan",
        primaryjoin="Booking.id == foreign(BookingRoom.booking_id)",
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="booking",
        primaryjoin="Booking.id == foreign(Payment.booking_id)",
    )
    reviews: Mapped[list[Review]] = relationship(
        back_populates="booking",
        primaryjoin="Booking.id == foreign(Review.booking_id)",
    )
    revenue_entries: Mapped[list[Revenue]] = relationship(
        back_populates="booking",
        primaryjoin="Booking.id == foreign(Revenue.booking_id)",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["guest_id", "hotel_id"],
            ["guests.id", "guests.hotel_id"],
            name="fk_bookings_guest_id_hotel_id_guests",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_bookings_public_id"),
        UniqueConstraint("hotel_id", "reference", name="uq_bookings_hotel_id_reference"),
        UniqueConstraint("id", "hotel_id", name="uq_bookings_id_hotel_id"),
        # The anchor for the booking_rooms mirror. Redundant as a business rule (id is
        # already unique) but required as a composite-FK target.
        UniqueConstraint(
            "id",
            "check_in_date",
            "check_out_date",
            "status",
            name="uq_bookings_id_stay_status",
        ),
        CheckConstraint("check_out_date > check_in_date", name="stay_dates_ordered"),
        CheckConstraint("adults >= 1", name="adults_positive"),
        CheckConstraint("children >= 0", name="children_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        CheckConstraint(f"status IN ({BookingStatus.sql_in_list()})", name="status_valid"),
        CheckConstraint(f"source IN ({BookingSource.sql_in_list()})", name="source_valid"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        # Biconditional, not one-way: a cancelled booking must carry a timestamp AND a
        # timestamp implies cancellation. The half-updated state is unrepresentable.
        CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL)",
            name="cancellation_consistent",
        ),
        Index("ix_bookings_hotel_id_check_in_date", "hotel_id", "check_in_date"),
        Index(
            "ix_bookings_hotel_id_status_check_in_date",
            "hotel_id",
            "status",
            "check_in_date",
        ),
        Index("ix_bookings_hotel_id_booked_at", "hotel_id", "booked_at"),
        Index("ix_bookings_guest_id", "guest_id"),
    )


class BookingRoom(TimestampMixin, Base):
    """Allocates one physical room to a booking for the whole stay.

    Inventory grain, not money grain: after the per-night pricing revision this table holds
    no price at all. Rates live one level down, in ``booking_room_nights``.
    """

    __tablename__ = "booking_rooms"

    id: Mapped[int] = pk_column()
    booking_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    room_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hotel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Mirrors of bookings. An EXCLUDE constraint can only reference columns in its own
    # table, so the stay window and status must be present here. The composite FK below
    # makes these copies provably non-divergent.
    check_in_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    check_out_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    booking_status: Mapped[str] = mapped_column(Text, nullable=False)

    # The authoritative expected count of booking_room_nights rows. Generated, so it can
    # never disagree with the dates it is derived from.
    nights: Mapped[int] = mapped_column(
        Integer, Computed("check_out_date - check_in_date", persisted=True)
    )

    adults: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    children: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    guest_name: Mapped[str | None] = mapped_column(Text)

    booking: Mapped[Booking] = relationship(
        back_populates="booking_rooms",
        primaryjoin="foreign(BookingRoom.booking_id) == Booking.id",
    )
    room: Mapped[Room] = relationship(
        back_populates="booking_rooms",
        primaryjoin="foreign(BookingRoom.room_id) == Room.id",
    )
    nights_rows: Mapped[list[BookingRoomNight]] = relationship(
        back_populates="booking_room",
        cascade="all, delete-orphan",
        primaryjoin="BookingRoom.id == foreign(BookingRoomNight.booking_room_id)",
    )

    __table_args__ = (
        # The mirror anchor. ON UPDATE CASCADE means a date or status change on the parent
        # propagates here automatically and the exclusion constraint is re-evaluated in the
        # same statement.
        ForeignKeyConstraint(
            ["booking_id", "check_in_date", "check_out_date", "booking_status"],
            [
                "bookings.id",
                "bookings.check_in_date",
                "bookings.check_out_date",
                "bookings.status",
            ],
            name="fk_booking_rooms_booking_stay_status_bookings",
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        # Multi-hotel guarantee: the room must belong to the booking's hotel.
        # RESTRICT, never CASCADE -- deleting a room must not erase stay history.
        ForeignKeyConstraint(
            ["room_id", "hotel_id"],
            ["rooms.id", "rooms.hotel_id"],
            name="fk_booking_rooms_room_id_hotel_id_rooms",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("booking_id", "room_id", name="uq_booking_rooms_booking_id_room_id"),
        # Composite-FK targets for booking_room_nights.
        UniqueConstraint("id", "check_in_date", "check_out_date", name="uq_booking_rooms_id_stay"),
        UniqueConstraint("id", "hotel_id", name="uq_booking_rooms_id_hotel_id"),
        CheckConstraint("check_out_date > check_in_date", name="stay_dates_ordered"),
        CheckConstraint("adults >= 1", name="adults_positive"),
        CheckConstraint("children >= 0", name="children_non_negative"),
        # THE overlap guarantee. Half-open so a checkout and a same-day check-in do not
        # collide -- that is how hotels turn rooms over, and a closed range would reject a
        # large fraction of legitimate bookings. Partial: only inventory-holding statuses.
        ExcludeConstraint(
            ("room_id", "="),
            (text("daterange(check_in_date, check_out_date, '[)')"), "&&"),
            name="excl_booking_rooms_room_no_overlap",
            using="gist",
            where=text(f"booking_status IN ({_HOLDING_SQL})"),
        ),
        Index("ix_booking_rooms_booking_id", "booking_id"),
        Index("ix_booking_rooms_room_id_check_in_date", "room_id", "check_in_date"),
    )


class BookingRoomNight(TimestampMixin, Base):
    """One row per booked room per night: the atomic financial unit of the platform.

    Every rate, room-revenue figure, ADR calculation and future pricing model resolves to
    rows in this table. Check-out day has no row, consistent with the half-open interval.
    """

    __tablename__ = "booking_room_nights"

    id: Mapped[int] = pk_column()
    booking_room_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Mirrored so the daily rollup is one indexed scan rather than a two-level join.
    hotel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    check_in_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    check_out_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    stay_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    rate: Mapped[decimal.Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    # Free text, no FK, no rate_plans table (approved decision 19). Captures why a rate was
    # what it was, which is unrecoverable after the fact and would otherwise confound every
    # pricing model trained on historical bookings.
    rate_plan_code: Mapped[str | None] = mapped_column(Text)

    # Distinguishes a genuine zero rate from a row nobody priced. Comped nights are
    # conventionally excluded from ADR but counted in occupancy.
    is_complimentary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    booking_room: Mapped[BookingRoom] = relationship(
        back_populates="nights_rows",
        primaryjoin="foreign(BookingRoomNight.booking_room_id) == BookingRoom.id",
    )

    __table_args__ = (
        # Same mirror technique, one level down: cascades from booking_rooms, which itself
        # cascades from bookings, so a date change propagates two levels in one statement.
        ForeignKeyConstraint(
            ["booking_room_id", "check_in_date", "check_out_date"],
            [
                "booking_rooms.id",
                "booking_rooms.check_in_date",
                "booking_rooms.check_out_date",
            ],
            name="fk_brn_booking_room_stay_booking_rooms",
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["booking_room_id", "hotel_id"],
            ["booking_rooms.id", "booking_rooms.hotel_id"],
            name="fk_brn_booking_room_id_hotel_id_booking_rooms",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "booking_room_id", "stay_date", name="uq_booking_room_nights_room_stay_date"
        ),
        # A night priced outside its own stay window is unrepresentable.
        CheckConstraint(
            "stay_date >= check_in_date AND stay_date < check_out_date",
            name="stay_date_within_stay",
        ),
        CheckConstraint("rate >= 0", name="rate_non_negative"),
        # The daily room-revenue / ADR rollup path, and the reason hotel_id is mirrored.
        Index("ix_booking_room_nights_hotel_id_stay_date", "hotel_id", "stay_date"),
    )


__all__ = ["Booking", "BookingRoom", "BookingRoomNight"]
