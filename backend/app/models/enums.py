"""Status vocabularies.

Approved decision 3: statuses are stored as ``TEXT`` guarded by a ``CHECK`` constraint rather
than as native PostgreSQL ``ENUM`` types. Adding a value is then a one-line constraint change
in a migration, where altering a native enum -- especially removing or reordering values --
is considerably more painful.

These classes are the single source of truth for both the Python side and the generated CHECK
constraints, so the two can never drift apart.
"""

from __future__ import annotations

from enum import StrEnum


class _Vocabulary(StrEnum):
    """A string enum that can render itself as a SQL ``IN`` list."""

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(member.value for member in cls)

    @classmethod
    def sql_in_list(cls) -> str:
        """``'a', 'b', 'c'`` -- for embedding in a CHECK constraint."""
        return ", ".join(f"'{value}'" for value in cls.values())


class BookingStatus(_Vocabulary):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


#: The statuses that hold physical room inventory (approved decisions 6-8).
#:
#: ``pending`` does NOT hold inventory, so an abandoned checkout never blocks a room.
#: ``cancelled``, ``no_show`` and ``checked_out`` all release it. This tuple is the single
#: definition used by the exclusion constraint's WHERE clause; changing it here changes the
#: constraint in the next migration.
INVENTORY_HOLDING_STATUSES: tuple[str, ...] = (
    BookingStatus.CONFIRMED.value,
    BookingStatus.CHECKED_IN.value,
)

#: Statuses that mean a room was physically occupied, for occupancy analytics.
#:
#: Deliberately WIDER than INVENTORY_HOLDING_STATUSES: a completed stay no longer blocks
#: future bookings, but it certainly counted as occupied on the nights it covered. Conflating
#: the two would erase every completed stay from historical occupancy.
OCCUPANCY_STATUSES: tuple[str, ...] = (
    BookingStatus.CONFIRMED.value,
    BookingStatus.CHECKED_IN.value,
    BookingStatus.CHECKED_OUT.value,
)


class RoomStatus(_Vocabulary):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    CLEANING = "cleaning"
    MAINTENANCE = "maintenance"
    OUT_OF_ORDER = "out_of_order"


class BookingSource(_Vocabulary):
    DIRECT = "direct"
    WEBSITE = "website"
    PHONE = "phone"
    WALK_IN = "walk_in"
    BOOKING_COM = "booking_com"
    EXPEDIA = "expedia"
    AIRBNB = "airbnb"
    AGODA = "agoda"
    OTHER = "other"


class PaymentKind(_Vocabulary):
    CHARGE = "charge"
    REFUND = "refund"


class PaymentMethod(_Vocabulary):
    CARD = "card"
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    ONLINE_GATEWAY = "online_gateway"
    OTA_COLLECT = "ota_collect"
    VOUCHER = "voucher"


class PaymentStatus(_Vocabulary):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    CANCELLED = "cancelled"


class ReviewSource(_Vocabulary):
    DIRECT = "direct"
    BOOKING_COM = "booking_com"
    TRIPADVISOR = "tripadvisor"
    GOOGLE = "google"
    EXPEDIA = "expedia"
    AIRBNB = "airbnb"
    OTHER = "other"


class HotelRole(_Vocabulary):
    """What a member may do at one hotel (Stage 4.2).

    Totally ordered, which is what makes authorization a rank comparison rather than a
    permission matrix. Four roles over 71 operations is a model a person can hold in their
    head and audit; an explicit permission set would be far more surface to get wrong.

    The ordering is the design's main assumption. If a genuinely non-hierarchical need
    appears -- a night auditor who reads finance but not guests -- the ordering breaks, and
    that is the moment to introduce permissions rather than bend a role into place.
    """

    VIEWER = "viewer"
    STAFF = "staff"
    MANAGER = "manager"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        """Position in the hierarchy. Higher outranks lower."""
        return ROLE_RANK[self]

    def outranks_or_equals(self, required: HotelRole) -> bool:
        return self.rank >= required.rank


#: Declared once, beside the enum, so a new role cannot be added without deciding where it
#: sits. `HotelRole.rank` reads through this rather than relying on declaration order, which
#: would be an invisible dependency.
ROLE_RANK: dict[HotelRole, int] = {
    HotelRole.VIEWER: 0,
    HotelRole.STAFF: 1,
    HotelRole.MANAGER: 2,
    HotelRole.OWNER: 3,
}


class PlatformRole(_Vocabulary):
    """Authority over the resources that belong to no hotel (Stage 4.3).

    Deliberately NOT part of :class:`HotelRole`, and deliberately not ranked against it. The
    two answer different questions -- "what may you do at this property?" against "may you
    maintain what every property shares?" -- and there is no ordering between them: a
    platform administrator is not a very senior owner, and an owner is not a junior platform
    administrator. Putting both in one ordered enum would invite exactly that comparison, and
    the first `>=` written against it would silently grant cross-tenant access.

    One member, because one capability exists. A second would need a decision about whether
    platform roles are ordered at all, which is the moment to make it rather than now.
    """

    PLATFORM_ADMIN = "platform_admin"


class RecurrenceInterval(_Vocabulary):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
