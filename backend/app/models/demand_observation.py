"""Declared observation of a hotel's demand: the spans whose complete booking record is held here.

Migration 0016. A row is an operator's declaration that, for one hotel, every date from
``observed_from`` to ``observed_to`` -- both inclusive -- is represented in full by the booking
tables. It is the only evidence of observation the schema carries: a date with no occupied nights
is a zero only inside a declared span, and outside every span a date's demand is unknown, whatever
rows exist for it. See ``app.ml.dataset.ObservationPeriod`` for how the spans are read.

**Closed on both ends** (``observed_to`` is NOT NULL) and **never overlapping** within a hotel
(exclusion constraint); see the migration for why each. No ``updated_at``: a span is declared or
withdrawn, never edited.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_column


class DemandObservationPeriod(Base):
    """One declared span, both ends inclusive, for one hotel."""

    __tablename__ = "demand_observation_periods"

    id: Mapped[int] = pk_column()

    #: The hotel the declaration is about. CASCADE: a statement about a hotel's data cannot
    #: outlive the hotel, and it records no event that must survive it.
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )

    #: First observed date, inclusive.
    observed_from: Mapped[dt.date] = mapped_column(Date, nullable=False)

    #: Last observed date, inclusive. Never null: an open end would declare days nobody has seen.
    observed_to: Mapped[dt.date] = mapped_column(Date, nullable=False)

    #: When the declaration was made. Defaulted by the database.
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("observed_to >= observed_from", name="ordered"),
        # Two spans of one hotel never share a date, so "is this date observed?" has one answer.
        # Closed ranges: [a, b] and [b + 1, c] are adjacent and allowed.
        ExcludeConstraint(
            ("hotel_id", "="),
            (text("daterange(observed_from, observed_to, '[]')"), "&&"),
            name="excl_demand_observation_periods_no_overlap",
            using="gist",
        ),
    )
