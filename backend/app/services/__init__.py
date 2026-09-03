"""Business logic layer.

Called by the API, calls repositories. Knows the domain; knows no HTTP and no SQL.
"""

from __future__ import annotations

from app.services.amenity import AmenityService, RoomTypeAmenityService
from app.services.analytics import AnalyticsService
from app.services.auth import AuthService
from app.services.authorization import HotelAccessPolicy
from app.services.booking import BookingService
from app.services.finance import (
    ExpenseCategoryService,
    ExpenseService,
    RevenueCategoryService,
    RevenueService,
)
from app.services.guest import GuestService
from app.services.health import HealthService
from app.services.hotel import HotelService
from app.services.intelligence import IntelligenceService
from app.services.payment import PaymentService
from app.services.review import ReviewService
from app.services.room import RoomService
from app.services.room_type import RoomTypeService
from app.services.scope import HotelScopeResolver

__all__ = [
    "AmenityService",
    "AnalyticsService",
    "AuthService",
    "BookingService",
    "ExpenseCategoryService",
    "ExpenseService",
    "GuestService",
    "HealthService",
    "HotelAccessPolicy",
    "HotelScopeResolver",
    "HotelService",
    "IntelligenceService",
    "PaymentService",
    "RevenueCategoryService",
    "RevenueService",
    "ReviewService",
    "RoomService",
    "RoomTypeAmenityService",
    "RoomTypeService",
]
