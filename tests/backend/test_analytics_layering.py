"""Analytics layering, read-only guarantees and currency discipline.

Three things this file exists to pin down: that analytics can never write, that no monetary
figure can ever be a bare scalar, and that ``daily_hotel_metrics`` is neither read nor
written while it remains an unpopulated snapshot table.
"""

from __future__ import annotations

import ast
import datetime as dt
import decimal
import inspect
import itertools
import textwrap
import uuid
from typing import Any, get_args, get_origin

import pytest

from app.api import deps
from app.api.v1.endpoints import analytics as analytics_router
from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationError
from app.main import create_app
from app.models.enums import INVENTORY_HOLDING_STATUSES, OCCUPANCY_STATUSES, BookingStatus
from app.repositories.analytics import AnalyticsRepository
from app.schemas.analytics import (
    MAX_RANGE_DAYS,
    BookingStatusCounts,
    DailyMetricsRow,
    DailySeriesResponse,
    ExpenseBreakdownResponse,
    MoneyByCurrency,
    OccupancyMetrics,
    OverviewResponse,
    RevenueBreakdownResponse,
    ReviewAnalyticsResponse,
    RoomRevenueByCurrency,
)
from app.services.analytics import RATING_BUCKETS, AnalyticsService
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy

MONEY_LIST_FIELDS = [
    "other_revenue",
    "ledger_room_revenue",
    "total_expenses",
    "net_operating_result",
]
RESPONSE_SCHEMAS = [
    OverviewResponse,
    DailySeriesResponse,
    RevenueBreakdownResponse,
    ExpenseBreakdownResponse,
    ReviewAnalyticsResponse,
]


#: The four grains that fan out from a booking. Aggregating any two in one statement
#: multiplies both.
FAN_OUT_MODELS = {"BookingRoomNight", "Revenue", "Expense", "Review"}


def names_in(function: object) -> set[str]:
    """Every identifier a function references, matched EXACTLY.

    Substring matching would be wrong here: ``RoomRevenueRow`` contains ``Revenue`` without
    touching the revenue ledger at all.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))  # type: ignore[arg-type]
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def code_of(module: object) -> str:
    """Module source with docstrings removed, so prose explaining an absent construct cannot
    trip a scan for that construct."""
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# --- layer separation ------------------------------------------------------------------------


def test_router_contains_no_sql() -> None:
    source = code_of(analytics_router)

    for forbidden in ["select(", "func.", "session.", "Session", "sqlalchemy", "join("]:
        assert forbidden not in source, f"router references {forbidden!r}"


def test_service_contains_no_sqlalchemy_query_construction() -> None:
    source = code_of(inspect.getmodule(AnalyticsService))

    for forbidden in ["select(", "func.", ".where(", "group_by", "order_by", "join("]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_service_never_touches_a_session() -> None:
    """It is not even given one: with no writes there is no unit of work to own."""
    source = code_of(inspect.getmodule(AnalyticsService))

    assert "Session" not in source
    assert "_session" not in source
    assert "session" not in inspect.signature(AnalyticsService).parameters


def test_repository_holds_no_domain_or_http_concerns() -> None:
    source = code_of(inspect.getmodule(AnalyticsRepository))

    for forbidden in ["raise ", "HTTPException", "status_code", "NotFoundError", "ValidationError"]:
        assert forbidden not in source, f"repository contains {forbidden!r}"


# --- read-only, at every layer -----------------------------------------------------------------


def test_no_analytics_layer_commits_or_rolls_back() -> None:
    for module in (inspect.getmodule(AnalyticsRepository), inspect.getmodule(AnalyticsService)):
        source = code_of(module)
        assert "commit" not in source
        assert "rollback" not in source


def test_no_analytics_layer_can_write() -> None:
    """No INSERT, UPDATE or DELETE construct exists anywhere in the domain."""
    for module in (inspect.getmodule(AnalyticsRepository), inspect.getmodule(AnalyticsService)):
        source = code_of(module)
        for forbidden in ["insert(", "update(", "delete(", "session.add", "flush", "merge("]:
            assert forbidden not in source, f"{module} contains {forbidden!r}"


def test_the_repository_exposes_only_read_methods() -> None:
    methods = {name for name in dir(AnalyticsRepository) if not name.startswith("_")}

    for forbidden in ["add", "create", "update", "delete", "save", "upsert", "populate"]:
        assert forbidden not in methods, f"repository exposes {forbidden!r}"


def test_the_service_exposes_only_the_five_reports() -> None:
    methods = {name for name in dir(AnalyticsService) if not name.startswith("_")}

    assert methods == {"overview", "daily", "revenue_breakdown", "expense_breakdown", "reviews"}


def test_every_analytics_route_is_a_get() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    for path, operations in paths.items():
        if "/analytics/" in path:
            assert set(operations) == {"get"}, path


# --- daily_hotel_metrics is not touched ---------------------------------------------------------


def test_no_analytics_layer_reads_or_writes_daily_hotel_metrics() -> None:
    """It is an empty snapshot table awaiting a population job that does not exist. Reading
    it would report zeros for every hotel; writing it from a GET would make a read endpoint
    mutate state."""
    for module in (
        inspect.getmodule(AnalyticsRepository),
        inspect.getmodule(AnalyticsService),
        analytics_router,
    ):
        source = code_of(module)
        assert "DailyHotelMetric" not in source
        assert "daily_hotel_metrics" not in source


def test_no_background_job_or_scheduler_was_introduced() -> None:
    for module in (inspect.getmodule(AnalyticsRepository), inspect.getmodule(AnalyticsService)):
        source = code_of(module)
        for forbidden in ["celery", "schedule", "cron", "BackgroundTask", "threading"]:
            assert forbidden.lower() not in source.lower(), forbidden


# --- currency discipline ------------------------------------------------------------------------


@pytest.mark.parametrize("field", MONEY_LIST_FIELDS)
def test_every_overview_money_field_is_a_list_of_buckets(field: str) -> None:
    """A scalar total would be a number that is not money as soon as two currencies appear."""
    annotation = OverviewResponse.model_fields[field].annotation

    assert get_origin(annotation) is list
    assert get_args(annotation)[0] is MoneyByCurrency


def test_room_revenue_is_bucketed_and_carries_its_own_rates() -> None:
    annotation = OverviewResponse.model_fields["room_revenue"].annotation

    assert get_origin(annotation) is list
    assert get_args(annotation)[0] is RoomRevenueByCurrency
    assert {"currency", "room_revenue", "adr", "revpar"} == set(RoomRevenueByCurrency.model_fields)


@pytest.mark.parametrize("schema", RESPONSE_SCHEMAS)
def test_no_response_schema_has_a_bare_decimal_money_field(schema: type) -> None:
    """Every Decimal that leaves must either be a rate or sit inside a currency-bearing
    model. A bare `total_revenue: Decimal` is exactly the bug this domain must not have."""
    banned = {"total_revenue", "total_amount", "revenue", "expenses", "net_result", "amount"}

    assert not (banned & set(schema.model_fields))  # type: ignore[attr-defined]


def test_no_fx_conversion_exists_anywhere_in_the_domain() -> None:
    for module in (inspect.getmodule(AnalyticsRepository), inspect.getmodule(AnalyticsService)):
        source = code_of(module).lower()
        for forbidden in ["fx", "exchange_rate", "convert_currency", "base_currency"]:
            assert forbidden not in source, forbidden


def test_the_hotels_own_currency_is_never_assumed() -> None:
    """Reading hotel.currency would be the first step towards presenting one headline
    number and silently mislabelling the rest."""
    source = code_of(inspect.getmodule(AnalyticsService))

    assert "hotel.currency" not in source
    assert "Hotel.currency" not in code_of(inspect.getmodule(AnalyticsRepository))


def test_combining_money_keeps_currencies_apart() -> None:
    combined = AnalyticsService._combine(
        [
            MoneyByCurrency(currency="EUR", amount=decimal.Decimal("100")),
            MoneyByCurrency(currency="USD", amount=decimal.Decimal("50")),
        ],
        [MoneyByCurrency(currency="EUR", amount=decimal.Decimal("25"))],
    )

    assert combined == [
        MoneyByCurrency(currency="EUR", amount=decimal.Decimal("125")),
        MoneyByCurrency(currency="USD", amount=decimal.Decimal("50")),
    ]


def test_subtracting_money_keeps_currencies_apart() -> None:
    """A currency present on only one side still appears, with the other side counted as
    zero for that currency alone -- a fact about entry counts, not an FX assumption."""
    net = AnalyticsService._subtract(
        [MoneyByCurrency(currency="EUR", amount=decimal.Decimal("100"))],
        [
            MoneyByCurrency(currency="EUR", amount=decimal.Decimal("30")),
            MoneyByCurrency(currency="JPY", amount=decimal.Decimal("5000")),
        ],
    )

    assert net == [
        MoneyByCurrency(currency="EUR", amount=decimal.Decimal("70")),
        MoneyByCurrency(currency="JPY", amount=decimal.Decimal("-5000")),
    ]


def test_money_buckets_are_ordered_for_determinism() -> None:
    combined = AnalyticsService._combine(
        [
            MoneyByCurrency(currency="USD", amount=decimal.Decimal("1")),
            MoneyByCurrency(currency="EUR", amount=decimal.Decimal("1")),
            MoneyByCurrency(currency="JPY", amount=decimal.Decimal("1")),
        ]
    )

    assert [bucket.currency for bucket in combined] == ["EUR", "JPY", "USD"]


# --- the formulas come from the generated columns -------------------------------------------------


def test_an_undefined_rate_is_null_not_zero() -> None:
    """Mirrors the NULLIF guards on daily_hotel_metrics. Reporting zero would drag every
    downstream average down."""
    rows = AnalyticsService._room_revenue_rows([], available_room_nights=0)
    assert rows == []

    from app.repositories.analytics import RoomRevenueRow

    computed = AnalyticsService._room_revenue_rows(
        [RoomRevenueRow(currency="EUR", room_revenue=decimal.Decimal("0"), room_nights_sold=0)],
        available_room_nights=0,
    )
    assert computed[0].adr is None
    assert computed[0].revpar is None


def test_adr_divides_by_nights_sold_and_revpar_by_nights_available() -> None:
    from app.repositories.analytics import RoomRevenueRow

    row = AnalyticsService._room_revenue_rows(
        [RoomRevenueRow(currency="EUR", room_revenue=decimal.Decimal("400"), room_nights_sold=4)],
        available_room_nights=10,
    )[0]

    assert row.adr == decimal.Decimal("100.00")  # 400 / 4 sold
    assert row.revpar == decimal.Decimal("40.00")  # 400 / 10 available


def test_occupancy_rate_is_nullable_in_the_schema() -> None:
    annotation = OccupancyMetrics.model_fields["occupancy_rate"].annotation

    assert type(None) in get_args(annotation)


def test_the_occupancy_limitation_is_stated_in_the_payload() -> None:
    """rooms.status and rooms.is_active have no history, so a past range's capacity is
    current inventory. Said in the response, not only in the docs."""
    assert "available_room_nights_basis" in OccupancyMetrics.model_fields
    assert (
        OccupancyMetrics.model_fields["available_room_nights_basis"].default
        == "current_active_rooms"
    )


# --- occupancy is counted from nights, not booking headers ----------------------------------------


def test_occupancy_uses_the_night_table_not_the_booking_header() -> None:
    """A booking is not occupancy. A night row is."""
    source = code_of(AnalyticsRepository)

    assert "BookingRoomNight.stay_date" in source
    assert "occupancy_counts" in source


def test_occupancy_uses_the_wider_occupancy_status_set() -> None:
    """A completed stay no longer blocks a room but certainly occupied it, so
    OCCUPANCY_STATUSES is deliberately wider than INVENTORY_HOLDING_STATUSES."""
    assert set(INVENTORY_HOLDING_STATUSES) < set(OCCUPANCY_STATUSES)
    assert BookingStatus.CHECKED_OUT.value in OCCUPANCY_STATUSES
    assert BookingStatus.PENDING.value not in OCCUPANCY_STATUSES
    assert "OCCUPANCY_STATUSES" in code_of(inspect.getmodule(AnalyticsRepository))


def test_pending_bookings_are_excluded_from_occupancy() -> None:
    """An abandoned checkout must never appear as an occupied room."""
    assert BookingStatus.PENDING.value not in OCCUPANCY_STATUSES
    assert BookingStatus.CANCELLED.value not in OCCUPANCY_STATUSES
    assert BookingStatus.NO_SHOW.value not in OCCUPANCY_STATUSES


def test_the_night_filter_is_defined_once() -> None:
    """Every night-grained query shares one definition, so they cannot disagree."""
    source = code_of(AnalyticsRepository)

    # One definition plus four call sites: the two occupancy queries and the two room-revenue
    # queries all share it, so they cannot drift apart.
    assert source.count("_night_filters(") == 5


# --- no join multiplication -----------------------------------------------------------------------


def test_no_repository_method_joins_two_fan_out_branches() -> None:
    """Bookings fan out to nights AND independently to revenue and reviews. Joining any two
    in one statement multiplies both. Each method aggregates exactly one grain."""
    checked = 0
    for method_name in dir(AnalyticsRepository):
        if method_name.startswith("_"):
            continue
        touched = names_in(getattr(AnalyticsRepository, method_name)) & FAN_OUT_MODELS
        assert len(touched) <= 1, f"{method_name} spans {sorted(touched)}"
        checked += 1

    assert checked >= 10  # the scan is not vacuous


def test_the_ledger_joins_only_to_its_own_category_lookup() -> None:
    """revenue -> revenue_categories is many-to-one, so it cannot multiply."""
    source = inspect.getsource(AnalyticsRepository.revenue_by_category)
    names = names_in(AnalyticsRepository.revenue_by_category)

    assert "RevenueCategory.id == Revenue.category_id" in source
    assert names & FAN_OUT_MODELS == {"Revenue"}
    assert "RevenueCategory" in names  # the many-to-one lookup, which cannot multiply


def test_room_revenue_joins_only_the_many_to_one_chain() -> None:
    """night -> allocation -> booking. Each hop is many-to-one, so nothing fans out."""
    source = inspect.getsource(AnalyticsRepository.room_revenue_by_currency)
    names = names_in(AnalyticsRepository.room_revenue_by_currency)

    assert "BookingRoom.id == BookingRoomNight.booking_room_id" in source
    assert "Booking.id == BookingRoom.booking_id" in source
    # The ledger and the review table are not reachable from this query at all.
    assert names & FAN_OUT_MODELS == {"BookingRoomNight"}


# --- scoping --------------------------------------------------------------------------------------


def test_every_repository_method_takes_a_hotel_id() -> None:
    for name in dir(AnalyticsRepository):
        if name.startswith("_"):
            continue
        parameters = inspect.signature(getattr(AnalyticsRepository, name)).parameters
        assert "hotel_id" in parameters, name


def test_no_repository_method_accepts_a_public_id() -> None:
    """A query cannot be issued before the service has established the tenant."""
    for name in dir(AnalyticsRepository):
        if name.startswith("_"):
            continue
        parameters = set(inspect.signature(getattr(AnalyticsRepository, name)).parameters)
        assert not any("public" in p for p in parameters), name


def test_no_unscoped_or_portfolio_wide_method_exists() -> None:
    methods = {name for name in dir(AnalyticsRepository) if not name.startswith("_")}

    for forbidden in [
        "get_all_metrics",
        "list_all_revenue",
        "get_global_analytics",
        "all_hotels",
        "portfolio",
    ]:
        assert forbidden not in methods, f"repository exposes {forbidden!r}"


def test_the_service_resolves_the_hotel_before_anything_else() -> None:
    for name in ("overview", "daily", "revenue_breakdown", "expense_breakdown", "reviews"):
        source = inspect.getsource(getattr(AnalyticsService, name))
        assert "self._resolve(hotel_public_id)" in source, name


# --- date range validation ------------------------------------------------------------------------


def _service() -> AnalyticsService:
    class _NoHotels:
        def get_by_public_id(self, _: uuid.UUID) -> None:
            return None

    class _NoRoomTypes:
        def get_by_hotel_and_code(self, _hotel_id: int, _code: str) -> None:
            return None

    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    return AnalyticsService(object(), scope)  # type: ignore[arg-type]


def test_a_valid_range_is_echoed_with_an_inclusive_day_count() -> None:
    span = AnalyticsService._require_range(dt.date(2026, 9, 1), dt.date(2026, 9, 3))

    assert span.days == 3  # inclusive at both ends


def test_a_same_day_range_is_one_day() -> None:
    span = AnalyticsService._require_range(dt.date(2026, 9, 1), dt.date(2026, 9, 1))

    assert span.days == 1


def test_a_reversed_range_is_rejected_not_swapped() -> None:
    """Swapping would answer a question the caller did not ask."""
    with pytest.raises(ValidationError, match="date_to must not be earlier"):
        AnalyticsService._require_range(dt.date(2026, 9, 5), dt.date(2026, 9, 1))


def test_an_over_long_range_is_rejected() -> None:
    start = dt.date(2026, 1, 1)
    with pytest.raises(ValidationError, match="maximum"):
        AnalyticsService._require_range(start, start + dt.timedelta(days=MAX_RANGE_DAYS))


def test_the_maximum_range_itself_is_accepted() -> None:
    start = dt.date(2026, 1, 1)
    span = AnalyticsService._require_range(start, start + dt.timedelta(days=MAX_RANGE_DAYS - 1))

    assert span.days == MAX_RANGE_DAYS


def test_the_hotel_is_resolved_before_the_range_is_validated() -> None:
    """An unknown hotel is a 404 whatever the range looks like."""
    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        _service().overview(uuid.uuid4(), dt.date(2026, 9, 5), dt.date(2026, 9, 1))


# --- response shape -------------------------------------------------------------------------------


@pytest.mark.parametrize("schema", RESPONSE_SCHEMAS)
def test_no_response_exposes_an_internal_key(schema: type) -> None:
    fields = set(schema.model_fields)  # type: ignore[attr-defined]

    for forbidden in ["id", "hotel_id", "category_id", "booking_id", "room_id", "guest_id"]:
        assert forbidden not in fields
    assert "hotel_public_id" in fields


def test_the_booking_status_counts_cover_the_whole_vocabulary() -> None:
    fields = set(BookingStatusCounts.model_fields) - {"total"}

    assert fields == set(BookingStatus.values())


def test_the_daily_row_omits_booking_status_counts() -> None:
    """Status is current, not historical, so attributing today's status to the day a booking
    was created would misreport every past day."""
    fields = set(DailyMetricsRow.model_fields)

    for absent in ["confirmed", "cancelled", "pending", "checked_out", "bookings_by_stay"]:
        assert absent not in fields
    assert "bookings_created" in fields  # the one count whose date column is unambiguous


def test_the_rating_buckets_tile_the_normalized_range() -> None:
    assert len(RATING_BUCKETS) == 5
    assert RATING_BUCKETS[0][1] == "0.0000"
    assert RATING_BUCKETS[-1][2] == "1.0000"
    for (_, _, upper), (_, lower, _) in itertools.pairwise(RATING_BUCKETS):
        assert upper == lower


# --- OpenAPI surface ------------------------------------------------------------------------------


def test_analytics_is_always_nested_under_a_hotel() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])
    analytics_paths = {p for p in paths if "analytics" in p}

    assert analytics_paths == {
        "/api/v1/hotels/{hotel_public_id}/analytics/overview",
        "/api/v1/hotels/{hotel_public_id}/analytics/daily",
        "/api/v1/hotels/{hotel_public_id}/analytics/revenue-by-category",
        "/api/v1/hotels/{hotel_public_id}/analytics/expenses-by-category",
        "/api/v1/hotels/{hotel_public_id}/analytics/reviews",
    }


def test_no_flat_or_portfolio_wide_analytics_route_exists() -> None:
    """The hotel segment is where tenant isolation is established."""
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert "/api/v1/analytics" not in paths
    for banned in ("/api/v1/reports", "/api/v1/dashboard", "/api/v1/metrics"):
        assert not any(p.startswith(banned) for p in paths), banned


def test_the_date_range_is_required_on_every_analytics_route() -> None:
    """No implicit window: a default would make two identical requests mean different things
    on different days."""
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    for path, operations in paths.items():
        if "/analytics/" not in path:
            continue
        required = {p["name"] for p in operations["get"].get("parameters", []) if p.get("required")}
        assert {"date_from", "date_to"} <= required, path


def test_the_analytics_domain_itself_grew_no_predictive_route() -> None:
    """Stage 3B.10 is the deterministic foundation. Forecasting arrived in 3B.11 as a
    SEPARATE domain under its own path segment, so the analytics paths themselves must
    still contain nothing predictive -- and nothing agentic or generative exists anywhere."""
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])
    analytics_paths = {p for p in paths if "/analytics/" in p}

    for banned in ("forecast", "predict", "recommend", "insight", "anomaly", "embedding"):
        assert not any(banned in path for path in analytics_paths), banned
    for never in ("chat", "agent", "embedding", "pricing", "recommend"):
        assert not any(never in path for path in paths), never


def test_the_dependency_assembles_the_service_without_a_session_of_its_own() -> None:
    class _Session:
        pass

    stub: Any = _Session()
    scope = deps.get_scope_resolver(stub, AllowAllPolicy())
    service = deps.get_analytics_service(stub, scope)

    assert isinstance(service, AnalyticsService)
    assert isinstance(service._repository, AnalyticsRepository)
    assert isinstance(service._scope, HotelScopeResolver)
    assert not hasattr(service, "_session")
