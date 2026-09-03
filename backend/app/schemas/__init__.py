"""Pydantic request and response schemas -- the API's input/output contracts.

Kept separate from the SQLAlchemy models on purpose: a change to how something is stored must
not automatically become a change to the public API.
"""

from __future__ import annotations

from app.schemas.amenity import (
    AmenityAssignment,
    AmenityCreate,
    AmenityResponse,
    AmenityUpdate,
)
from app.schemas.analytics import (
    DailySeriesResponse,
    ExpenseBreakdownResponse,
    OverviewResponse,
    RevenueBreakdownResponse,
    ReviewAnalyticsResponse,
)
from app.schemas.auth import (
    LoginRequest,
    RegistrationRequest,
    TokenResponse,
    UserResponse,
)
from app.schemas.booking import BookingCreate, BookingResponse, BookingUpdate
from app.schemas.common import ErrorBody, ErrorDetail, ErrorResponse, Page
from app.schemas.finance import (
    ExpenseCategoryCreate,
    ExpenseCategoryResponse,
    ExpenseCategoryUpdate,
    ExpenseCreate,
    ExpenseResponse,
    RevenueCategoryCreate,
    RevenueCategoryResponse,
    RevenueCategoryUpdate,
    RevenueCreate,
    RevenueResponse,
)
from app.schemas.guest import GuestCreate, GuestResponse, GuestUpdate
from app.schemas.health import DatabaseHealthResponse, HealthResponse
from app.schemas.hotel import HotelCreate, HotelResponse, HotelUpdate
from app.schemas.intelligence import (
    AnomalyResponse,
    DemandTrendResponse,
    InsightsResponse,
    OccupancyForecastResponse,
    RevenueForecastResponse,
)
from app.schemas.meta import ApiMetaResponse
from app.schemas.payment import ChargeCreate, PaymentResponse, RefundCreate
from app.schemas.review import ReviewCreate, ReviewModerationUpdate, ReviewResponse
from app.schemas.room import RoomCreate, RoomResponse, RoomUpdate
from app.schemas.room_type import RoomTypeCreate, RoomTypeResponse, RoomTypeUpdate

__all__ = [
    "AmenityAssignment",
    "AmenityCreate",
    "AmenityResponse",
    "AmenityUpdate",
    "AnomalyResponse",
    "ApiMetaResponse",
    "BookingCreate",
    "BookingResponse",
    "BookingUpdate",
    "ChargeCreate",
    "DailySeriesResponse",
    "DatabaseHealthResponse",
    "DemandTrendResponse",
    "ErrorBody",
    "ErrorDetail",
    "ErrorResponse",
    "ExpenseBreakdownResponse",
    "ExpenseCategoryCreate",
    "ExpenseCategoryResponse",
    "ExpenseCategoryUpdate",
    "ExpenseCreate",
    "ExpenseResponse",
    "GuestCreate",
    "GuestResponse",
    "GuestUpdate",
    "HealthResponse",
    "HotelCreate",
    "HotelResponse",
    "HotelUpdate",
    "InsightsResponse",
    "LoginRequest",
    "OccupancyForecastResponse",
    "OverviewResponse",
    "Page",
    "PaymentResponse",
    "RefundCreate",
    "RegistrationRequest",
    "RevenueBreakdownResponse",
    "RevenueCategoryCreate",
    "RevenueCategoryResponse",
    "RevenueCategoryUpdate",
    "RevenueCreate",
    "RevenueForecastResponse",
    "RevenueResponse",
    "ReviewAnalyticsResponse",
    "ReviewCreate",
    "ReviewModerationUpdate",
    "ReviewResponse",
    "RoomCreate",
    "RoomResponse",
    "RoomTypeCreate",
    "RoomTypeResponse",
    "RoomTypeUpdate",
    "RoomUpdate",
    "TokenResponse",
    "UserResponse",
]
