"""SQLAlchemy ORM models -- the approved 27-table schema (7.7: `llm_invocations`; 7.9: documents;
7.11: copilot conversations).

Importing this package registers every model on ``Base.metadata``, which is what Alembic's
autogenerate and the test fixtures rely on. Import order matters only in that all models must
be imported before mappers are configured; the explicit re-exports below guarantee that.
"""

from __future__ import annotations

from app.db.base import Base
from app.models.audit import AuditEvent, AuditEventArchive
from app.models.booking import Booking, BookingRoom, BookingRoomNight
from app.models.copilot_conversation import CopilotConversation, CopilotMessage
from app.models.finance import Expense, ExpenseCategory, Revenue, RevenueCategory
from app.models.guest import Guest
from app.models.hotel import Hotel
from app.models.knowledge import HotelDocument, HotelDocumentChunk
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
    "CopilotConversation",
    "CopilotMessage",
    "DailyHotelMetric",
    "DemandPrediction",
    "Expense",
    "ExpenseCategory",
    "Guest",
    "Hotel",
    "HotelDocument",
    "HotelDocumentChunk",
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
