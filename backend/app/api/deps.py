"""FastAPI dependencies.

The database session is provided here and nowhere else, so no endpoint ever constructs its own
engine, and the pool built in ``app.db.session`` is the only one in the process.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Generator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import InvalidTokenError, RateLimitExceededError
from app.core.rate_limit import FixedWindowRateLimiter, RateLimit
from app.db.session import get_session_factory
from app.models.enums import HotelRole
from app.models.user import User
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
from app.services.amenity import AmenityService, RoomTypeAmenityService
from app.services.analytics import AnalyticsService
from app.services.auth import AuthService
from app.services.authorization import HotelAccessPolicy, PlatformAccessPolicy
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
from app.services.membership import MembershipService
from app.services.payment import PaymentService
from app.services.review import ReviewService
from app.services.room import RoomService
from app.services.room_type import RoomTypeService
from app.services.scope import HotelScopeResolver


def get_app_settings(request: Request) -> Settings:
    """Return the settings the running application was built with.

    Reads from ``app.state`` rather than calling ``get_settings()``, whose ``lru_cache``
    returns the process-wide singleton. Those differ whenever ``create_app`` is handed
    explicit settings -- as every test does -- and an endpoint that consulted the global
    would silently report a different environment from the app it belongs to.
    """
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped session, rolling back on failure and always closing.

    **This dependency does not commit.** Transaction boundaries belong to the service that
    knows what constitutes a complete unit of work; committing here would silently persist
    half-finished writes whenever a request happened to reach its end without raising.

    On any exception the session is rolled back before the error propagates, so a failed
    request can never leave a partial transaction holding locks. ``close()`` runs in a
    ``finally`` and therefore returns the connection to the pool on every path.
    """
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


#: Annotated alias so endpoints read as `db: DbSession` rather than repeating Depends().
DbSession = Annotated[Session, Depends(get_db)]


def get_health_service(db: DbSession) -> HealthService:
    """Assemble the health service over a request-scoped session."""
    return HealthService(HealthRepository(db))


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


def get_hotel_service(db: DbSession, policy: HotelAccessPolicyDep) -> HotelService:
    """Assemble the hotel service over a request-scoped session.

    The session is passed to the service as well as to the repository: the service owns the
    unit of work and must be able to commit and roll back it, which the repository must not.
    """
    return HotelService(db, HotelRepository(db), MembershipRepository(db), policy)


HotelServiceDep = Annotated[HotelService, Depends(get_hotel_service)]


def get_scope_resolver(db: DbSession, policy: HotelAccessPolicyDep) -> HotelScopeResolver:
    """The shared parent-chain resolver: hotel, then room type within it.

    One seam used by every nested domain, so "a child is reached only through its own
    parents" has a single implementation rather than a copy per service.
    """
    return HotelScopeResolver(HotelRepository(db), RoomTypeRepository(db), policy)


ScopeResolverDep = Annotated[HotelScopeResolver, Depends(get_scope_resolver)]


def get_room_type_service(db: DbSession, scope: ScopeResolverDep) -> RoomTypeService:
    """Assemble the room type service over a request-scoped session."""
    return RoomTypeService(db, RoomTypeRepository(db), scope)


RoomTypeServiceDep = Annotated[RoomTypeService, Depends(get_room_type_service)]


def get_room_service(db: DbSession, scope: ScopeResolverDep) -> RoomService:
    """Assemble the room service over a request-scoped session."""
    return RoomService(db, RoomRepository(db), scope)


RoomServiceDep = Annotated[RoomService, Depends(get_room_service)]


def get_amenity_service(db: DbSession) -> AmenityService:
    """Assemble the amenity catalogue service.

    No scope resolver: the catalogue is global, and pretending otherwise by threading a
    hotel through would imply an ownership the schema does not model.
    """
    return AmenityService(db, AmenityRepository(db))


AmenityServiceDep = Annotated[AmenityService, Depends(get_amenity_service)]


def get_guest_service(db: DbSession, scope: ScopeResolverDep) -> GuestService:
    """Assemble the guest service over a request-scoped session.

    Guests hang off a hotel only -- no room type is involved -- so the resolver is used for
    its ``require_hotel`` half alone.
    """
    return GuestService(db, GuestRepository(db), scope)


GuestServiceDep = Annotated[GuestService, Depends(get_guest_service)]


def get_booking_service(db: DbSession, scope: ScopeResolverDep) -> BookingService:
    """Assemble the booking service over a request-scoped session.

    It receives the guest repository because a booking names its guest by public id and must
    resolve it *within the same hotel* -- reusing the existing scoped lookup rather than
    duplicating it.
    """
    return BookingService(db, BookingRepository(db), GuestRepository(db), scope)


BookingServiceDep = Annotated[BookingService, Depends(get_booking_service)]


def get_payment_service(db: DbSession, scope: ScopeResolverDep) -> PaymentService:
    """Assemble the payment service over a request-scoped session.

    It reuses the booking repository: a payment is reached through its booking, and that
    scoped lookup already exists rather than being duplicated here.
    """
    return PaymentService(db, PaymentRepository(db), BookingRepository(db), scope)


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]


def get_review_service(db: DbSession, scope: ScopeResolverDep) -> ReviewService:
    """Assemble the review service over a request-scoped session.

    It reuses the booking repository: a review is addressed by its booking, and that scoped
    lookup already exists rather than being duplicated here.
    """
    return ReviewService(db, ReviewRepository(db), BookingRepository(db), scope)


ReviewServiceDep = Annotated[ReviewService, Depends(get_review_service)]


def get_revenue_category_service(db: DbSession) -> RevenueCategoryService:
    """The revenue-stream vocabulary. Global -- no hotel scope resolver is needed or wanted."""
    return RevenueCategoryService(db, RevenueCategoryRepository(db))


RevenueCategoryServiceDep = Annotated[RevenueCategoryService, Depends(get_revenue_category_service)]


def get_expense_category_service(db: DbSession) -> ExpenseCategoryService:
    """The cost vocabulary. Also global, and a separate code space from revenue categories."""
    return ExpenseCategoryService(db, ExpenseCategoryRepository(db))


ExpenseCategoryServiceDep = Annotated[ExpenseCategoryService, Depends(get_expense_category_service)]


def get_revenue_service(db: DbSession, scope: ScopeResolverDep) -> RevenueService:
    """Assemble the revenue journal over a request-scoped session.

    It reuses the category and booking repositories rather than duplicating their lookups:
    a revenue line names a category by code and, optionally, a stay by public id.
    """
    return RevenueService(
        db,
        RevenueRepository(db),
        RevenueCategoryRepository(db),
        BookingRepository(db),
        scope,
    )


RevenueServiceDep = Annotated[RevenueService, Depends(get_revenue_service)]


def get_expense_service(db: DbSession, scope: ScopeResolverDep) -> ExpenseService:
    """Assemble the cost journal. No booking repository: expenses have no booking column."""
    return ExpenseService(db, ExpenseRepository(db), ExpenseCategoryRepository(db), scope)


ExpenseServiceDep = Annotated[ExpenseService, Depends(get_expense_service)]


def get_analytics_service(db: DbSession, scope: ScopeResolverDep) -> AnalyticsService:
    """Assemble the read-only analytics service.

    It is handed the session only so its repository can query; the service itself owns no
    unit of work, because analytics never writes.
    """
    return AnalyticsService(AnalyticsRepository(db), scope)


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]


def get_intelligence_service(db: DbSession, scope: ScopeResolverDep) -> IntelligenceService:
    """Assemble the read-only intelligence service.

    It is handed the ANALYTICS repository, not one of its own. Occupancy, room revenue and
    booking counts keep exactly one definition in this codebase; a forecasting layer with its
    own copy would drift from the dashboard within a release.
    """
    return IntelligenceService(AnalyticsRepository(db), scope)


IntelligenceServiceDep = Annotated[IntelligenceService, Depends(get_intelligence_service)]


def get_auth_service(db: DbSession, settings: SettingsDep) -> AuthService:
    """Assemble the authentication service.

    It takes Settings because the signing secret lives there and must not be read from a
    module-level global -- the app factory can be constructed with different settings, and a
    cached global would ignore them.
    """
    return AuthService(db, UserRepository(db), settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]

#: Extracts `Authorization: Bearer <token>`. auto_error=False so a MISSING header reaches our
#: own handler and produces the shared error envelope with our own message, rather than
#: Starlette's default body -- one error shape across the whole API.
_bearer = HTTPBearer(auto_error=False, description="A bearer token from POST /auth/login.")


def get_current_user(
    service: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """The authenticated caller, or 401.

    **This establishes identity only.** It does not decide which hotels the user may reach:
    that is Stage 4.2, and this dependency deliberately takes no hotel argument so it cannot
    quietly grow one.
    """
    if credentials is None or not credentials.credentials:
        raise InvalidTokenError
    return service.resolve_token(credentials.credentials)


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def client_address(request: Request) -> str:
    """The address a rate-limit bucket is keyed by.

    **The direct peer address, and nothing else.** ``X-Forwarded-For`` and ``Forwarded`` are
    client-controlled: honouring either without a configured, trusted proxy in front would let
    an attacker mint a fresh bucket per request by varying a header, which is worse than no
    limiter at all because it looks like protection. This application has no trusted-proxy
    configuration -- Stage 4.5.3 deliberately did not invent one -- so the peer address is the
    only source that cannot be forged from outside.

    The consequence, stated rather than hidden: deployed behind a reverse proxy without
    ``--forwarded-allow-ips``, every request appears to come from the proxy and the limit
    becomes global rather than per-client. Turning on proxy handling is a deployment decision
    that has to come with a trusted-proxy list, and that is the change to make first.

    ``request.client`` is None only for a transport with no peer; those share one bucket
    rather than escaping the limiter entirely.
    """
    return request.client.host if request.client else "unknown"


def rate_limited(scope: str, policy_of: Callable[[Settings], RateLimit]) -> Callable[..., None]:
    """Declare a rate limit on a route.

    Returned as a dependency so the limit sits on the route where a reader can see it, exactly
    as ``require_role`` puts a role there. The POLICY comes from settings rather than from the
    call site: the numbers live in one place and a deployment can change them.

    *scope* separates the buckets, so exhausting the login limit does not silently disable
    password changes as well.
    """

    def dependency(request: Request, settings: SettingsDep) -> None:
        limiter: FixedWindowRateLimiter = request.app.state.rate_limiter
        verdict = limiter.check(f"{scope}:{client_address(request)}", policy_of(settings))
        if not verdict.allowed:
            raise RateLimitExceededError(verdict.retry_after)

    return dependency


#: The two protected authentication routes. Declared here so a router names a policy rather
#: than a number, and so both read from settings through the same path.
login_rate_limit = rate_limited(
    "auth:login",
    lambda s: RateLimit(s.auth_login_rate_limit, s.auth_login_rate_limit_window_seconds),
)
change_password_rate_limit = rate_limited(
    "auth:change-password",
    lambda s: RateLimit(
        s.auth_change_password_rate_limit,
        s.auth_change_password_rate_limit_window_seconds,
    ),
)


def get_platform_access_policy(db: DbSession, current_user: CurrentUserDep) -> PlatformAccessPolicy:
    """The request's PLATFORM authorization policy, carrying the authenticated caller.

    Separate from :func:`get_hotel_access_policy` and never composed with it: the two answer
    different questions from different tables, and a dependency that returned both would be
    the natural place for someone to later write "or platform admin" into a hotel check.
    """
    return PlatformAccessPolicy(current_user, PlatformAdminRepository(db))


PlatformAccessPolicyDep = Annotated[PlatformAccessPolicy, Depends(get_platform_access_policy)]


def require_platform_admin(policy: PlatformAccessPolicyDep) -> None:
    """Guard a write to a global catalogue.

    ``amenities``, ``revenue_categories`` and ``expense_categories`` have no ``hotel_id`` --
    one row is shared by every property. A role is a per-hotel grant, so no role can confer
    authority over a resource that belongs to no hotel: letting an owner at one property
    rename or deactivate a category would reach across every other tenant through a resource
    that looks harmless. Stage 4.2 therefore closed these routes to everyone; Stage 4.3
    reopens them to exactly one identity, held in ``platform_admins`` and read per request.

    Depending on the policy -- which depends on CurrentUserDep -- is what keeps an anonymous
    caller on 401 rather than 403: the two failures must stay distinguishable in that
    direction, so a stranger is told to authenticate rather than told what they lack.

    Reads are NOT guarded by this. Every authenticated caller may read the catalogues,
    because every hotel has to reference the vocabulary it is required to use.
    """
    policy.require_platform_admin()


def get_hotel_access_policy(db: DbSession, current_user: CurrentUserDep) -> HotelAccessPolicy:
    """The request's authorization policy, carrying the authenticated caller.

    Depending on CurrentUserDep is what makes every hotel-scoped route authenticated: the
    scope resolver needs a policy, the policy needs a user, and a request without a valid
    token never gets past that chain. There is no path to a domain service that skips it.
    """
    return HotelAccessPolicy(current_user, MembershipRepository(db))


HotelAccessPolicyDep = Annotated[HotelAccessPolicy, Depends(get_hotel_access_policy)]


def require_role(required: HotelRole) -> Callable[..., None]:
    """Declare the minimum role a route needs.

    Returned as a dependency so the requirement sits on the route where a reader can see it.
    Which role a verb needs is a fact about the HTTP surface, not about the hotel, so this is
    the one authorization concern that legitimately lives at the router.
    """

    def dependency(
        hotel_public_id: uuid.UUID,
        scope: ScopeResolverDep,
    ) -> None:
        scope.require_hotel_with_role(hotel_public_id, required)

    return dependency


def get_membership_service(db: DbSession, scope: ScopeResolverDep) -> MembershipService:
    """Assemble the membership administration service over a request-scoped session.

    It reuses the SAME MembershipRepository the authorization policy reads through, so
    "who may act here?" and "who has access here?" can never drift into two answers, and
    the UserRepository authentication already owns -- identity is global, and a second way
    to look up an account would be a second place for the lookup to differ.
    """
    return MembershipService(db, MembershipRepository(db), UserRepository(db), scope)


MembershipServiceDep = Annotated[MembershipService, Depends(get_membership_service)]


def get_room_type_amenity_service(db: DbSession, scope: ScopeResolverDep) -> RoomTypeAmenityService:
    """Assemble the assignment service: associations ARE hierarchy-scoped."""
    return RoomTypeAmenityService(db, RoomTypeAmenityRepository(db), AmenityRepository(db), scope)


RoomTypeAmenityServiceDep = Annotated[
    RoomTypeAmenityService, Depends(get_room_type_amenity_service)
]

__all__ = [
    "AmenityServiceDep",
    "AnalyticsServiceDep",
    "AuthServiceDep",
    "BookingServiceDep",
    "CurrentUserDep",
    "DbSession",
    "ExpenseCategoryServiceDep",
    "ExpenseServiceDep",
    "GuestServiceDep",
    "HealthServiceDep",
    "HotelAccessPolicyDep",
    "HotelServiceDep",
    "IntelligenceServiceDep",
    "MembershipServiceDep",
    "PaymentServiceDep",
    "PlatformAccessPolicyDep",
    "RevenueCategoryServiceDep",
    "RevenueServiceDep",
    "ReviewServiceDep",
    "RoomServiceDep",
    "RoomTypeAmenityServiceDep",
    "RoomTypeServiceDep",
    "ScopeResolverDep",
    "SettingsDep",
    "change_password_rate_limit",
    "client_address",
    "get_amenity_service",
    "get_analytics_service",
    "get_app_settings",
    "get_auth_service",
    "get_booking_service",
    "get_current_user",
    "get_db",
    "get_expense_category_service",
    "get_expense_service",
    "get_guest_service",
    "get_health_service",
    "get_hotel_access_policy",
    "get_hotel_service",
    "get_intelligence_service",
    "get_membership_service",
    "get_payment_service",
    "get_platform_access_policy",
    "get_revenue_category_service",
    "get_revenue_service",
    "get_review_service",
    "get_room_service",
    "get_room_type_amenity_service",
    "get_room_type_service",
    "get_scope_resolver",
    "login_rate_limit",
    "rate_limited",
    "require_platform_admin",
    "require_role",
]
