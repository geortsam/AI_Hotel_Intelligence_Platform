"""FastAPI dependencies.

The database session is provided here and nowhere else, so no endpoint ever constructs its own
engine, and the pool built in ``app.db.session`` is the only one in the process.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Callable, Generator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.copilot.contracts import ToolServices
from app.copilot.registry import ToolRegistry, build_default_registry
from app.core.client_address import (
    FORWARDED_FOR_HEADER,
    resolve_client_ip,
    trusted_networks,
)
from app.core.config import Settings
from app.core.errors import InvalidTokenError, RateLimitExceededError
from app.core.rate_limit import FixedWindowRateLimiter, RateLimit
from app.db.session import get_session_factory
from app.llm.base import Budget, ChatModel
from app.llm.errors import LlmBudgetExhaustedError
from app.llm.factory import build_chat_model
from app.models.enums import HotelRole
from app.models.user import User
from app.repositories.amenity import AmenityRepository, RoomTypeAmenityRepository
from app.repositories.analytics import AnalyticsRepository
from app.repositories.audit import AuditRepository
from app.repositories.booking import BookingRepository
from app.repositories.copilot_conversation import CopilotConversationRepository
from app.repositories.finance import (
    ExpenseCategoryRepository,
    ExpenseRepository,
    RevenueCategoryRepository,
    RevenueRepository,
)
from app.repositories.guest import GuestRepository
from app.repositories.health import HealthRepository
from app.repositories.hotel import HotelRepository
from app.repositories.knowledge import KnowledgeRepository
from app.repositories.llm_invocation import LlmInvocationRepository
from app.repositories.membership import MembershipRepository
from app.repositories.ml_demand import MlDemandRepository
from app.repositories.ml_prediction import MlPredictionRepository
from app.repositories.payment import PaymentRepository
from app.repositories.platform_admin import PlatformAdminRepository
from app.repositories.pricing import PricingRepository
from app.repositories.review import ReviewRepository
from app.repositories.room import RoomRepository
from app.repositories.room_type import RoomTypeRepository
from app.repositories.user import UserRepository
from app.services.amenity import AmenityService, RoomTypeAmenityService
from app.services.analytics import AnalyticsService
from app.services.audit import AuditQueryService, AuditTrail, PlatformAuditQueryService
from app.services.auth import AuthService
from app.services.authorization import HotelAccessPolicy, PlatformAccessPolicy
from app.services.availability import AvailabilitySearchService
from app.services.booking import BookingService
from app.services.copilot import CopilotService
from app.services.copilot_conversation import CopilotConversationService
from app.services.finance import (
    ExpenseCategoryService,
    ExpenseService,
    RevenueCategoryService,
    RevenueService,
)
from app.services.guest import GuestService
from app.services.health import HealthService
from app.services.hotel import HotelService
from app.services.insight import InsightService
from app.services.intelligence import IntelligenceService
from app.services.knowledge import KnowledgeService
from app.services.llm_invocation_log import LlmInvocationLog
from app.services.membership import MembershipService
from app.services.ml_accuracy import DemandAccuracyService
from app.services.ml_drift import DemandDistributionService
from app.services.ml_performance import ForecastPerformanceService
from app.services.ml_prediction_read import DemandPredictionReadService
from app.services.ml_serving import DemandPredictionService
from app.services.payment import PaymentService
from app.services.pricing import PricingService
from app.services.reconciliation import ReconciliationService
from app.services.review import ReviewService
from app.services.room import RoomService
from app.services.room_type import RoomTypeService
from app.services.scope import HotelScopeResolver
from app.services.tool_invocation import ToolInvocationService


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


def get_audit_trail(db: DbSession, current_user: CurrentUserDep) -> AuditTrail:
    """The request's audit trail, already carrying the authenticated caller (Stage 4.5.12).

    Bound at construction rather than passed per call, so a service records an event without
    naming an identity and therefore cannot name the wrong one -- the same reason
    ``HotelAccessPolicy`` is built with its user.

    It shares the request's session, which is what makes an audit row part of the mutation's
    own transaction rather than a second write that could succeed or fail independently.

    Depending on ``CurrentUserDep`` adds no work to a hotel-scoped route: that dependency is
    already resolved for it, by the chain the access policy sits on, and FastAPI resolves each
    dependency once per request.
    """
    return AuditTrail(AuditRepository(db), current_user)


AuditTrailDep = Annotated[AuditTrail, Depends(get_audit_trail)]


def get_unbound_audit_trail(db: DbSession) -> AuditTrail:
    """An audit trail with no actor bound yet.

    For :class:`~app.services.auth.AuthService` alone, and unavoidable there: that service is
    what RESOLVES a token, so the authenticated user cannot also be one of its dependencies.
    ``change_password`` is handed the caller directly by ``CurrentUserDep`` and binds it with
    ``AuditTrail.for_actor``; no other method on that service records anything.
    """
    return AuditTrail(AuditRepository(db))


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


def get_amenity_service(db: DbSession, audit: AuditTrailDep) -> AmenityService:
    """Assemble the amenity catalogue service.

    No scope resolver: the catalogue is global, and pretending otherwise by threading a
    hotel through would imply an ownership the schema does not model.
    """
    return AmenityService(db, AmenityRepository(db), audit)


AmenityServiceDep = Annotated[AmenityService, Depends(get_amenity_service)]


def get_guest_service(db: DbSession, scope: ScopeResolverDep) -> GuestService:
    """Assemble the guest service over a request-scoped session.

    Guests hang off a hotel only -- no room type is involved -- so the resolver is used for
    its ``require_hotel`` half alone.
    """
    return GuestService(db, GuestRepository(db), scope)


GuestServiceDep = Annotated[GuestService, Depends(get_guest_service)]


def get_booking_service(
    db: DbSession, scope: ScopeResolverDep, audit: AuditTrailDep
) -> BookingService:
    """Assemble the booking service over a request-scoped session.

    It receives the guest repository because a booking names its guest by public id and must
    resolve it *within the same hotel* -- reusing the existing scoped lookup rather than
    duplicating it.

    Stage 4.5.23 adds the pricing service, over the same session: a new booking's
    nightly rates are calculated here rather than accepted from the caller.
    """
    return BookingService(
        db,
        BookingRepository(db),
        GuestRepository(db),
        scope,
        audit,
        PricingService(PricingRepository(db)),
        PaymentRepository(db),
    )


BookingServiceDep = Annotated[BookingService, Depends(get_booking_service)]


def get_payment_service(
    db: DbSession, scope: ScopeResolverDep, audit: AuditTrailDep
) -> PaymentService:
    """Assemble the payment service over a request-scoped session.

    It reuses the booking repository: a payment is reached through its booking, and that
    scoped lookup already exists rather than being duplicated here.
    """
    return PaymentService(db, PaymentRepository(db), BookingRepository(db), scope, audit)


def get_availability_service(db: DbSession, scope: ScopeResolverDep) -> AvailabilitySearchService:
    """Assemble the availability search over a request-scoped session.

    It reuses the room repository: availability is a question about rooms, and the scoped
    access to them already exists rather than being duplicated for search.
    """
    return AvailabilitySearchService(db, RoomRepository(db), scope)


AvailabilityServiceDep = Annotated[AvailabilitySearchService, Depends(get_availability_service)]


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]


def get_reconciliation_service(db: DbSession, scope: ScopeResolverDep) -> ReconciliationService:
    """Assemble the reconciliation service over a request-scoped session.

    It reuses both existing repositories rather than gaining its own: the booking side already
    knows how to reach a booking within a hotel, and the payment side already defines what
    counts as money. A third repository would be a third place for those to drift.
    """
    return ReconciliationService(db, BookingRepository(db), PaymentRepository(db), scope)


ReconciliationServiceDep = Annotated[ReconciliationService, Depends(get_reconciliation_service)]


def get_review_service(db: DbSession, scope: ScopeResolverDep) -> ReviewService:
    """Assemble the review service over a request-scoped session.

    It reuses the booking repository: a review is addressed by its booking, and that scoped
    lookup already exists rather than being duplicated here.
    """
    return ReviewService(db, ReviewRepository(db), BookingRepository(db), scope)


ReviewServiceDep = Annotated[ReviewService, Depends(get_review_service)]


def get_revenue_category_service(db: DbSession, audit: AuditTrailDep) -> RevenueCategoryService:
    """The revenue-stream vocabulary. Global -- no hotel scope resolver is needed or wanted."""
    return RevenueCategoryService(db, RevenueCategoryRepository(db), audit)


RevenueCategoryServiceDep = Annotated[RevenueCategoryService, Depends(get_revenue_category_service)]


def get_expense_category_service(db: DbSession, audit: AuditTrailDep) -> ExpenseCategoryService:
    """The cost vocabulary. Also global, and a separate code space from revenue categories."""
    return ExpenseCategoryService(db, ExpenseCategoryRepository(db), audit)


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


def get_audit_query_service(db: DbSession, scope: ScopeResolverDep) -> AuditQueryService:
    """Assemble the read-only audit history service (Stage 4.5.12).

    A DIFFERENT object from the ``AuditTrail`` the mutating services carry, over the same
    repository. The writer needs an actor and no hotel scope; the reader needs a hotel scope
    and no actor. One class doing both would be a class with a ``record`` method sitting on
    the object a read-only endpoint holds.

    It owns no unit of work -- reading history writes nothing -- so it is handed no session,
    exactly as the analytics service is not.
    """
    return AuditQueryService(AuditRepository(db), scope)


AuditServiceDep = Annotated[AuditQueryService, Depends(get_audit_query_service)]


def get_platform_audit_service(db: DbSession) -> PlatformAuditQueryService:
    """Assemble the read-only PLATFORM audit history service (Stage 4.5.13).

    It is handed the repository and nothing else -- no scope resolver, deliberately, so the
    object a platform-scoped route holds has no collaborator through which a hotel could be
    reached. Compare `get_audit_query_service` above, which needs one and gets one.

    No session either: reading history writes nothing, so there is no unit of work to own.

    **It does not depend on the platform policy.** Authorization is declared on the route by
    ``require_platform_admin``, exactly as it is for every catalogue write. Resolving the
    policy here as well would put the grant check in two places, and two places is how one of
    them ends up being the one nobody updates.
    """
    return PlatformAuditQueryService(AuditRepository(db))


PlatformAuditServiceDep = Annotated[PlatformAuditQueryService, Depends(get_platform_audit_service)]


def get_intelligence_service(db: DbSession, scope: ScopeResolverDep) -> IntelligenceService:
    """Assemble the read-only intelligence service.

    It is handed the ANALYTICS repository, not one of its own. Occupancy, room revenue and
    booking counts keep exactly one definition in this codebase; a forecasting layer with its
    own copy would drift from the dashboard within a release.
    """
    return IntelligenceService(AnalyticsRepository(db), scope)


IntelligenceServiceDep = Annotated[IntelligenceService, Depends(get_intelligence_service)]


def get_demand_prediction_service(
    db: DbSession, scope: ScopeResolverDep
) -> DemandPredictionService:
    """Assemble the read-only demand-model serving service (Stage 6.6).

    It is handed the SAME ``MlDemandRepository`` the Stage 6.1 dataset pipeline uses, not one of
    its own. The features a model is served are the features it was trained on, or they are not
    the same model -- and a serving-only copy of "realised occupied room nights" is exactly how
    the two stop agreeing without anything failing.

    No session is passed to the service itself, only to its repository: scoring a model writes
    nothing, so there is no unit of work to own. The same asymmetry as the analytics and
    intelligence services above.

    The artifact is NOT a dependency. It is loaded at most once per process, behind
    ``app.ml.artifact_store``, rather than per request -- a request-scoped dependency would
    unpickle 700 KB and rebuild the estimator on every call.

    Stage 6.8 hands it the session as well as the repositories. It now owns a unit of work: the
    prediction it returns and the row recording it commit together. That is why the session is
    passed to the SERVICE and not only to its repositories, exactly as it is for every other
    writing service -- the repositories query and flush, and the service decides what a complete
    operation is.
    """
    return DemandPredictionService(db, MlDemandRepository(db), MlPredictionRepository(db), scope)


DemandPredictionServiceDep = Annotated[
    DemandPredictionService, Depends(get_demand_prediction_service)
]


def get_demand_prediction_read_service(
    db: DbSession, scope: ScopeResolverDep
) -> DemandPredictionReadService:
    """Assemble the read-only stored-prediction service (Stage 6.11).

    No session reaches the service itself, only its repository: reading a hotel's own prediction
    history writes nothing, so there is no unit of work to own. The same asymmetry as the
    analytics, intelligence, accuracy and distribution services -- and the deliberate opposite of
    ``get_demand_prediction_service`` above, which hands the session to the SERVICE because
    serving a prediction and recording it are one operation.

    It is handed the same ``MlPredictionRepository`` the serving path writes through, not a
    read-only copy of its own. One module owns what a prediction row is.
    """
    return DemandPredictionReadService(MlPredictionRepository(db), scope)


DemandPredictionReadServiceDep = Annotated[
    DemandPredictionReadService, Depends(get_demand_prediction_read_service)
]


def get_forecast_performance_service(
    db: DbSession, scope: ScopeResolverDep
) -> ForecastPerformanceService:
    """Assemble the HTTP boundary over the two frozen measurement services (Stage 7.3).

    The two services beneath are constructed here exactly as Stages 6.9 and 6.10 construct them
    -- same repositories, same resolver, and the default protocol each declares. Neither is
    subclassed, wrapped or handed a modified protocol: a route measuring under different rules
    from a programmatic caller would make the protocol checksum in the response meaningless.

    No session reaches any of the three. Measuring predictions against what actually happened
    writes nothing, so there is no unit of work to own, and the repositories hold the session for
    their own SELECTs. The same asymmetry as the analytics, intelligence and stored-prediction
    services.

    Both services are handed the SAME ``MlPredictionRepository``, and the accuracy service the
    same ``MlDemandRepository`` the dataset pipeline and serving path use. One module owns what a
    prediction row is, and one owns what realised demand is.
    """
    predictions = MlPredictionRepository(db)
    return ForecastPerformanceService(
        DemandAccuracyService(predictions, MlDemandRepository(db), scope),
        DemandDistributionService(predictions, scope),
    )


ForecastPerformanceServiceDep = Annotated[
    ForecastPerformanceService, Depends(get_forecast_performance_service)
]


def get_auth_service(db: DbSession, settings: SettingsDep) -> AuthService:
    """Assemble the authentication service.

    It takes Settings because the signing secret lives there and must not be read from a
    module-level global -- the app factory can be constructed with different settings, and a
    cached global would ignore them.
    """
    return AuthService(db, UserRepository(db), settings, get_unbound_audit_trail(db))


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


def client_address(request: Request, settings: Settings) -> str:
    """The address a rate-limit bucket is keyed by.

    The decision itself lives in `app.core.client_address`, which is the single place that
    knows the trusted-proxy rule; this function only supplies the request's peer and headers.
    Parsing a forwarding header HERE, next to the limiter, is how the two copies drift apart
    and how one of them ends up trusting something it should not.

    The short version of the rule it applies: while `TRUSTED_PROXIES` is empty -- the
    default -- no forwarding header is read at all and the direct peer address is used, so a
    client cannot influence its own bucket. Once proxies are configured, a header is read only
    when the peer IS one of them, and the chain is walked from the end nearest to us.
    """
    client = request.client
    return resolve_client_ip(
        client.host if client else None,
        request.headers.getlist(FORWARDED_FOR_HEADER),
        trusted_networks(tuple(settings.trusted_proxies)),
    )


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
        verdict = limiter.check(f"{scope}:{client_address(request, settings)}", policy_of(settings))
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


def get_membership_service(
    db: DbSession, scope: ScopeResolverDep, audit: AuditTrailDep
) -> MembershipService:
    """Assemble the membership administration service over a request-scoped session.

    It reuses the SAME MembershipRepository the authorization policy reads through, so
    "who may act here?" and "who has access here?" can never drift into two answers, and
    the UserRepository authentication already owns -- identity is global, and a second way
    to look up an account would be a second place for the lookup to differ.
    """
    return MembershipService(db, MembershipRepository(db), UserRepository(db), scope, audit)


MembershipServiceDep = Annotated[MembershipService, Depends(get_membership_service)]


def get_room_type_amenity_service(db: DbSession, scope: ScopeResolverDep) -> RoomTypeAmenityService:
    """Assemble the assignment service: associations ARE hierarchy-scoped."""
    return RoomTypeAmenityService(db, RoomTypeAmenityRepository(db), AmenityRepository(db), scope)


RoomTypeAmenityServiceDep = Annotated[
    RoomTypeAmenityService, Depends(get_room_type_amenity_service)
]


def get_knowledge_service(
    db: DbSession, scope: ScopeResolverDep, audit: AuditTrailDep
) -> KnowledgeService:
    """Assemble the hotel knowledge service (Stage 7.9).

    It owns a unit of work -- uploads, new versions and withdrawals commit, each with its audit
    event in the same transaction -- so it is handed the session, like every writing service.
    Retrieval goes through the same repository, bounded by the hotel the resolver returns.
    """
    return KnowledgeService(db, KnowledgeRepository(db), scope, audit)


KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]


def get_insight_service(
    intelligence: IntelligenceServiceDep, scope: ScopeResolverDep
) -> InsightService:
    """The attention list (Stage 7.12), over the same intelligence service its routes use."""
    return InsightService(intelligence, scope)


InsightServiceDep = Annotated[InsightService, Depends(get_insight_service)]


# --- the copilot (Stage 7.7) -----------------------------------------------------------------
#
# The composition root is the one place above `app.llm` that may name the factory and the budget
# error: routers never import `app.llm`, and services receive a `ChatModel` without knowing which
# vendor stands behind it. An architecture test pins exactly which `app.llm` modules this file
# reaches.

#: The minimum role for the copilot: any member. A module-level instance rather than an inline
#: `require_role(...)` call, so the route and `copilot_budget` depend on the SAME callable -- which
#: FastAPI resolves once per request, and which makes "authorized before charged" a property of
#: the dependency graph rather than of the order a decorator lists its dependencies in.
require_copilot_member = require_role(HotelRole.VIEWER)

#: The one sentence both budget refusals share. It does not say which allowance was spent: the
#: caller learns to wait, and `Retry-After` says for how long.
COPILOT_BUDGET_MESSAGE = "The assistant's usage limit has been reached. Please try again later."


def copilot_budget(
    hotel_public_id: uuid.UUID,
    request: Request,
    settings: SettingsDep,
    current_user: CurrentUserDep,
    _member: Annotated[None, Depends(require_copilot_member)],
) -> None:
    """Charge the per-actor, then the per-hotel, copilot allowance. §4.5.

    **Authorization first, structurally.** `_member` is the route's own role dependency, so this
    function cannot run until the caller has been proved a member of this hotel. A non-member
    therefore gets the hotel's 404 and never touches the hotel's counter: an outsider cannot spend
    another property's allowance, and cannot learn from a 429 that the property exists.

    **Keys are public identifiers only** -- the caller's `public_id` and the path's hotel UUID --
    never an internal BIGINT. The existing `FixedWindowRateLimiter` is used exactly as it is: an
    opaque key and a policy in, a verdict out. Both policies share one window (settings), so
    neither can prune the other's live counters.

    **The actor is charged first, and the hotel only if the actor was allowed.** The limiter
    counts refused requests too; checking the hotel after an actor refusal would let one caller's
    retries drain the property's allowance for every other member.
    """
    _charge_copilot_budget(hotel_public_id, request, settings, current_user)


def _charge_copilot_budget(
    hotel_public_id: uuid.UUID, request: Request, settings: Settings, current_user: User
) -> None:
    """The one copilot allowance, charged. `copilot_budget` and `conversation_turn_budget` are
    the two routes into it; each decides only what must be proved before it runs."""
    limiter: FixedWindowRateLimiter = request.app.state.rate_limiter
    window = settings.copilot_rate_limit_window_seconds

    actor = limiter.check(
        f"copilot:actor:{current_user.public_id}",
        RateLimit(settings.copilot_actor_rate_limit, window),
    )
    if not actor.allowed:
        raise LlmBudgetExhaustedError(COPILOT_BUDGET_MESSAGE, retry_after=actor.retry_after)

    hotel = limiter.check(
        f"copilot:hotel:{hotel_public_id}",
        RateLimit(settings.copilot_hotel_rate_limit, window),
    )
    if not hotel.allowed:
        raise LlmBudgetExhaustedError(COPILOT_BUDGET_MESSAGE, retry_after=hotel.retry_after)


def get_chat_model(settings: SettingsDep) -> ChatModel:
    """The guarded `ChatModel` for this deployment. Disabled unless `llm_enabled` is set.

    Built per request; the circuit breaker behind it is process-wide per upstream (see
    `app.llm.factory.breaker_for`), so building the wrapper per request loses nothing.
    """
    return build_chat_model(settings)


ChatModelDep = Annotated[ChatModel, Depends(get_chat_model)]


@functools.cache
def default_tool_registry() -> ToolRegistry:
    """The Stage 7.6 registry, built once per process. It is immutable after construction."""
    return build_default_registry()


def get_tool_invocation_service(
    db: DbSession,
    scope: ScopeResolverDep,
    audit: AuditTrailDep,
    analytics: AnalyticsServiceDep,
    demand_prediction: DemandPredictionServiceDep,
    forecast_performance: ForecastPerformanceServiceDep,
    knowledge: KnowledgeServiceDep,
    insight: InsightServiceDep,
) -> ToolInvocationService:
    """The Stage 7.6 invocation boundary, over the five services the tools delegate to.

    Each delegated-to service is assembled by its own existing dependency, exactly as its route
    assembles it, so a tool and a route reach the same service built the same way.
    """
    services = ToolServices(
        analytics=analytics,
        demand_prediction=demand_prediction,
        forecast_performance=forecast_performance,
        knowledge=knowledge,
        insight=insight,
    )
    return ToolInvocationService(db, default_tool_registry(), scope, audit, services)


ToolInvocationServiceDep = Annotated[ToolInvocationService, Depends(get_tool_invocation_service)]


def get_llm_invocation_log(db: DbSession, current_user: CurrentUserDep) -> LlmInvocationLog:
    """The invocation recorder, already carrying the authenticated caller.

    Bound here for the same reason `get_audit_trail` binds the audit trail's actor: a service
    that records without naming an identity cannot name the wrong one.
    """
    return LlmInvocationLog(LlmInvocationRepository(db), current_user)


LlmInvocationLogDep = Annotated[LlmInvocationLog, Depends(get_llm_invocation_log)]


def get_copilot_service(
    db: DbSession,
    settings: SettingsDep,
    scope: ScopeResolverDep,
    model: ChatModelDep,
    invocations: ToolInvocationServiceDep,
    log: LlmInvocationLogDep,
) -> CopilotService:
    """Assemble the copilot over the request's session.

    The provider and model names are handed over as opaque strings for the accounting row --
    the service records them and never branches on them -- and the per-request budget comes from
    the same settings the boundary enforces.
    """
    return CopilotService(
        db,
        model,
        default_tool_registry(),
        invocations,
        log,
        scope,
        budget=Budget(
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
        ),
        provider_name=settings.llm_provider,
        model_name=settings.llm_model,
    )


CopilotServiceDep = Annotated[CopilotService, Depends(get_copilot_service)]


# --- copilot conversations (Stage 7.11) ---------------------------------------------------------


def get_copilot_conversation_service(
    db: DbSession,
    settings: SettingsDep,
    scope: ScopeResolverDep,
    copilot: CopilotServiceDep,
    current_user: CurrentUserDep,
) -> CopilotConversationService:
    """The conversation service, bound to the authenticated caller -- whose conversations are the
    only ones it can reach -- and to the configured retention period."""
    return CopilotConversationService(
        db,
        CopilotConversationRepository(db),
        scope,
        copilot,
        current_user,
        retention_days=settings.copilot_conversation_retention_days,
    )


CopilotConversationServiceDep = Annotated[
    CopilotConversationService, Depends(get_copilot_conversation_service)
]


def require_open_conversation(
    hotel_public_id: uuid.UUID,
    conversation_public_id: uuid.UUID,
    service: CopilotConversationServiceDep,
    _member: Annotated[None, Depends(require_copilot_member)],
) -> None:
    """The caller's own live conversation exists at this hotel and can take another turn.

    Membership first (the route's own role dependency), then this, then the budget: a
    non-member, someone else's conversation, an expired or unknown one and a full one are all
    refused before any allowance is spent.
    """
    service.require_open(hotel_public_id, conversation_public_id)


def conversation_turn_budget(
    hotel_public_id: uuid.UUID,
    request: Request,
    settings: SettingsDep,
    current_user: CurrentUserDep,
    _open: Annotated[None, Depends(require_open_conversation)],
) -> None:
    """The copilot budget for continuing a conversation: the same allowance `copilot_budget`
    charges, reached only after `require_open_conversation` -- ownership before cost, as a
    property of the dependency graph."""
    _charge_copilot_budget(hotel_public_id, request, settings, current_user)


__all__ = [
    "COPILOT_BUDGET_MESSAGE",
    "AmenityServiceDep",
    "AnalyticsServiceDep",
    "AuditServiceDep",
    "AuditTrailDep",
    "AuthServiceDep",
    "AvailabilityServiceDep",
    "BookingServiceDep",
    "ChatModelDep",
    "CopilotConversationServiceDep",
    "CopilotServiceDep",
    "CurrentUserDep",
    "DbSession",
    "DemandPredictionServiceDep",
    "ExpenseCategoryServiceDep",
    "ExpenseServiceDep",
    "GuestServiceDep",
    "HealthServiceDep",
    "HotelAccessPolicyDep",
    "HotelServiceDep",
    "InsightServiceDep",
    "IntelligenceServiceDep",
    "KnowledgeServiceDep",
    "LlmInvocationLogDep",
    "MembershipServiceDep",
    "PaymentServiceDep",
    "PlatformAccessPolicyDep",
    "PlatformAuditServiceDep",
    "ReconciliationServiceDep",
    "RevenueCategoryServiceDep",
    "RevenueServiceDep",
    "ReviewServiceDep",
    "RoomServiceDep",
    "RoomTypeAmenityServiceDep",
    "RoomTypeServiceDep",
    "ScopeResolverDep",
    "SettingsDep",
    "ToolInvocationServiceDep",
    "change_password_rate_limit",
    "client_address",
    "conversation_turn_budget",
    "copilot_budget",
    "default_tool_registry",
    "get_amenity_service",
    "get_analytics_service",
    "get_app_settings",
    "get_audit_query_service",
    "get_audit_trail",
    "get_auth_service",
    "get_availability_service",
    "get_booking_service",
    "get_chat_model",
    "get_copilot_conversation_service",
    "get_copilot_service",
    "get_current_user",
    "get_db",
    "get_demand_prediction_service",
    "get_expense_category_service",
    "get_expense_service",
    "get_guest_service",
    "get_health_service",
    "get_hotel_access_policy",
    "get_hotel_service",
    "get_insight_service",
    "get_intelligence_service",
    "get_knowledge_service",
    "get_llm_invocation_log",
    "get_membership_service",
    "get_payment_service",
    "get_platform_access_policy",
    "get_platform_audit_service",
    "get_reconciliation_service",
    "get_revenue_category_service",
    "get_revenue_service",
    "get_review_service",
    "get_room_service",
    "get_room_type_amenity_service",
    "get_room_type_service",
    "get_scope_resolver",
    "get_tool_invocation_service",
    "get_unbound_audit_trail",
    "login_rate_limit",
    "rate_limited",
    "require_copilot_member",
    "require_open_conversation",
    "require_platform_admin",
    "require_role",
]
