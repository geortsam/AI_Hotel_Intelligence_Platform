"""Daily hotel metrics: one immutable snapshot row per hotel per calendar day.

This is a derived analytical table, not a transactional source of truth. It exists because a
forecasting target must be a stable, gap-free, reproducible series -- computing occupancy on
demand would let a backdated cancellation silently rewrite last month's history, making
backtests irreproducible.

Ratios are GENERATED ALWAYS ... STORED so a stored figure can never contradict its own inputs.
NULLIF guards division by zero: a hotel with no available rooms has an *undefined* ADR, not
one of zero, and recording zero would drag every average down and poison model training.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, pk_column

if TYPE_CHECKING:
    from app.models.hotel import Hotel


class DailyHotelMetric(TimestampMixin, Base):
    """One snapshot per hotel per business date."""

    __tablename__ = "daily_hotel_metrics"

    id: Mapped[int] = pk_column()
    # CASCADE is safe here, and only here among the hotel children: this data is derived and
    # can be recomputed, so it carries no independent historical value.
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    metric_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    available_rooms: Mapped[int] = mapped_column(Integer, nullable=False)
    occupied_rooms: Mapped[int] = mapped_column(Integer, nullable=False)
    out_of_order_rooms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    occupancy_rate: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(5, 4),
        Computed("occupied_rooms::numeric / NULLIF(available_rooms, 0)", persisted=True),
    )

    # Sourced from booking_room_nights, NOT from the revenue ledger (approved decision 21).
    room_revenue: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    other_revenue: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    total_revenue: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), Computed("room_revenue + other_revenue", persisted=True)
    )
    adr: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(14, 2), Computed("room_revenue / NULLIF(occupied_rooms, 0)", persisted=True)
    )
    revpar: Mapped[decimal.Decimal | None] = mapped_column(
        Numeric(14, 2), Computed("room_revenue / NULLIF(available_rooms, 0)", persisted=True)
    )
    total_expenses: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )

    bookings_created: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cancellations: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    no_shows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    arrivals: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    departures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Denormalized from the hotel so the row keeps its meaning even if the property later
    # changes base currency.
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # When this snapshot was calculated -- the audit hook that distinguishes "the world
    # changed" from "the job re-ran".
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    hotel: Mapped[Hotel] = relationship(back_populates="daily_metrics")

    __table_args__ = (
        # The identity of the row, the guard against double-computation, and the index that
        # serves every forecasting range scan.
        UniqueConstraint(
            "hotel_id", "metric_date", name="uq_daily_hotel_metrics_hotel_id_metric_date"
        ),
        CheckConstraint("available_rooms >= 0", name="available_rooms_non_negative"),
        CheckConstraint(
            "occupied_rooms >= 0 AND occupied_rooms <= available_rooms",
            name="occupied_rooms_within_available",
        ),
        CheckConstraint("out_of_order_rooms >= 0", name="out_of_order_rooms_non_negative"),
        CheckConstraint("room_revenue >= 0", name="room_revenue_non_negative"),
        CheckConstraint("bookings_created >= 0", name="bookings_created_non_negative"),
        CheckConstraint("cancellations >= 0", name="cancellations_non_negative"),
        CheckConstraint("no_shows >= 0", name="no_shows_non_negative"),
        CheckConstraint("arrivals >= 0", name="arrivals_non_negative"),
        CheckConstraint("departures >= 0", name="departures_non_negative"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
    )


__all__ = ["DailyHotelMetric"]
