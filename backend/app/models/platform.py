"""Platform-level authority: who may maintain the resources that belong to no hotel.

Stage 4.2 established that a role is a **per-hotel grant**, which is why it closed the three
global catalogues to everyone: no per-hotel grant can confer authority over a row that every
property shares. This table is the identity that can.

It is deliberately its own table rather than a column on :class:`~app.models.user.User` or a
row in :class:`~app.models.membership.UserHotel`. ``users`` has been kept free of
authorization since Stage 4.1; ``user_hotels.hotel_id`` is NOT NULL, so "authority over no
particular hotel" cannot be said there without a nullable tenant key that every membership
query would then have to remember to exclude.

**Holding a row here grants nothing hotel-scoped.** There is no foreign key to ``hotels`` and
no relationship to ``user_hotels``; :class:`~app.services.authorization.HotelAccessPolicy`
never consults this table. A platform administrator who is not a member of a hotel meets the
same 404 wall as any other stranger, and a structural test asserts the policy has no path to
here.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import PlatformRole


class PlatformAdmin(TimestampMixin, Base):
    """One user's platform-level role."""

    __tablename__ = "platform_admins"

    #: The primary key, with no surrogate: a user holds at most one platform role, so a
    #: second row could only restate or contradict the first. CASCADE because a grant
    #: without a user is meaningless and is not business data.
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: CHECK-constrained TEXT, as the schema's ten other vocabularies are. Stage 4.3 exposes
    #: no API for granting platform administration, so the real grant path is an out-of-band
    #: INSERT -- and the constraint is what stops that path storing a `superadmin` the
    #: application would never recognise.
    role: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(f"role IN ({PlatformRole.sql_in_list()})", name="role_valid"),
    )


__all__ = ["PlatformAdmin"]
