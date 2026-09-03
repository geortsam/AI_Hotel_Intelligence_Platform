"""Which hotels a user may reach, and in what capacity.

The join Stage 4.1 deliberately omitted. ``users`` says who someone is; this says which
properties they work at. Keeping the two apart is what allows one person to be a manager at
one hotel and a viewer at another, and what keeps a login identity from being owned by a
tenant.

There is no ``is_active`` here: revoking access means deleting the row. Two ways to express
"no longer has access" invites the bug where only one of them is checked.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, pk_column
from app.models.enums import HotelRole


class UserHotel(TimestampMixin, Base):
    """One user's membership of one hotel, at exactly one role."""

    __tablename__ = "user_hotels"

    id: Mapped[int] = pk_column()
    #: CASCADE: a membership without a user is meaningless and is not business data.
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: RESTRICT, matching the seven other policies pointing at hotels: a property with staff
    #: attached should fail loudly rather than quietly shed them.
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # One role per hotel. Several roles at one property would need union semantics with
        # no current use case, and would make "what may this person do here?" ambiguous.
        UniqueConstraint("user_id", "hotel_id", name="uq_user_hotels_user_id_hotel_id"),
        CheckConstraint(f"role IN ({HotelRole.sql_in_list()})", name="role_valid"),
        # The unique constraint already indexes user_id as its leading column, covering the
        # per-request lookup. This covers the other direction: who belongs to this hotel.
        Index("ix_user_hotels_hotel_id", "hotel_id"),
    )


__all__ = ["UserHotel"]
