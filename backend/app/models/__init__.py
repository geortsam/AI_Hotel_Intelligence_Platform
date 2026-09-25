"""SQLAlchemy ORM models -- the approved 23-table schema (Stage 7.7 added `llm_invocations`).

Importing this package registers every model on ``Base.metadata``, which is what Alembic's
autogenerate and the test fixtures rely on. Import order matters only in that all models must
be imported before mappers are configured; the explicit re-exports below guarantee that.
"""

from __future__ import annotations

from app.db.base import Base
from app.models.audit import AuditEvent, AuditEventArchive
from app.models.booking import Booking, BookingRoom, BookingRoomNight
from app.models.finance import Expense, ExpenseCategory, Revenue, RevenueCategory
from app.models.guest import Guest
from app.models.hotel import Hotel
from app.models.llm_invocation import LlmInvocation
from app.models.membership import UserHotel
from app.models.metrics import DailyHotelMetric
from app.models.payment import Payment
from app.models.platform import PlatformAdmin
from app.models.prediction import DemandPrediction
from app.models.review import Review
from app.models.room import Amenity, Room, RoomType, RoomTypeAmenity
from app.models.user import User

__all__ = [
    "Amenity",
    "AuditEvent",
    "AuditEventArchive",
    "Base",
    "Booking",
    "BookingRoom",
    "BookingRoomNight",
    "DailyHotelMetric",
    "DemandPrediction",
    "Expense",
    "ExpenseCategory",
    "Guest",
    "Hotel",
    "LlmInvocation",
    "Payment",
    "PlatformAdmin",
    "Revenue",
    "RevenueCategory",
    "Review",
    "Room",
    "RoomType",
    "RoomTypeAmenity",
    "User",
    "UserHotel",
]
