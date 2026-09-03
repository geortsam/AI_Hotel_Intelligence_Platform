"""Data-access layer: every statement that reaches the database lives here.

Knows SQL; decides nothing. Authorization and business rules belong one layer up.
"""

from __future__ import annotations

from app.repositories.amenity import AmenityRepository, RoomTypeAmenityRepository
from app.repositories.analytics import AnalyticsRepository
from app.repositories.booking import BookingRepository
from app.repositories.finance import (
    ExpenseCategoryRepository,
    ExpenseRepository,
    RevenueCategoryRepository,
    RevenueRepository,
)
from app.repositories.guest import GuestRepository
from app.repositories.health import HealthRepository
from app.repositories.hotel import HotelRepository
from app.repositories.membership import MembershipRepository
from app.repositories.payment import PaymentRepository
from app.repositories.platform_admin import PlatformAdminRepository
from app.repositories.review import ReviewRepository
from app.repositories.room import RoomRepository
from app.repositories.room_type import RoomTypeRepository
from app.repositories.user import UserRepository

__all__ = [
    "AmenityRepository",
    "AnalyticsRepository",
    "BookingRepository",
    "ExpenseCategoryRepository",
    "ExpenseRepository",
    "GuestRepository",
    "HealthRepository",
    "HotelRepository",
    "MembershipRepository",
    "PaymentRepository",
    "PlatformAdminRepository",
    "RevenueCategoryRepository",
    "RevenueRepository",
    "ReviewRepository",
    "RoomRepository",
    "RoomTypeAmenityRepository",
    "RoomTypeRepository",
    "UserRepository",
]
