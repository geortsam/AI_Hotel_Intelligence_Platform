"""Intelligence layering, model arithmetic and the boundaries this stage must not cross.

Two halves. The first exercises ``app.ml`` directly with hand-built series, where every
expected number is computed by hand in the test -- if the arithmetic here is wrong, no
database can tell us. The second pins the layering: no SQL in the models, no second
definition of any analytics metric, no writes, and none of the agentic or generative
machinery this stage explicitly excludes.
"""

from __future__ import annotations

import ast
import datetime as dt
import decimal
import inspect
import uuid
from collections.abc import Sequence
from typing import Any, get_args

import pytest

from app.api import deps
from app.api.v1.endpoints import intelligence as intelligence_router
from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationError
from app.main import create_app
from app.ml import timeseries
from app.ml.timeseries import (
    ANOMALY_THRESHOLD,
    MIN_BUCKET_OBSERVATIONS,
    MIN_TRAINING_OBSERVATIONS,
    MODEL_NAME,
    MODEL_VERSION,
    TREND_THRESHOLD,
    ForecastMethod,
    Observation,
    detect_anomalies,
    forecast_series,
    measure_trend,
)
from app.repositories.analytics import AnalyticsRepository
from app.schemas.intelligence import (
    MAX_HORIZON_DAYS,
    MAX_TRAINING_DAYS,
    MIN_TRAINING_DAYS,
    AnomalyResponse,
    DemandTrendResponse,
    ForecastMethodLiteral,
    Insight,
    InsightsResponse,
    OccupancyForecastPoint,
    OccupancyForecastResponse,
    RevenueForecastPoint,
    RevenueForecastResponse,
)
from app.services.intelligence import IntelligenceService
from app.services.scope import HotelScopeResolver
from tests.backend.authorization_stubs import AllowAllPolicy

RESPONSE_SCHEMAS = [
    OccupancyForecastResponse,
    RevenueForecastResponse,
    DemandTrendResponse,
    AnomalyResponse,
    InsightsResponse,
]

MONDAY = dt.date(2026, 1, 5)  # a Monday, so weekday arithmetic in the tests is legible


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


def identifiers_in(module: object) -> set[str]:
    """Every name and attribute a module actually references.

    Deliberately not a text scan: the router's OpenAPI descriptions explain in prose that
    payments are NOT used as a revenue proxy, and a text scan would read that sentence as
    the very thing it promises does not happen.
    """
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[-1])
    return found


def series(values: Sequence[int | str], start: dt.date = MONDAY) -> list[Observation]:
    """A dense daily series starting on *start*."""
    return [
        Observation(date=start + dt.timedelta(days=index), value=decimal.Decimal(str(value)))
        for index, value in enumerate(values)
    ]


# --- the models: forecasting -------------------------------------------------------------------


def test_a_constant_series_forecasts_that_constant() -> None:
    """The simplest sanity check the arithmetic must pass."""
    points = forecast_series(series([10] * 28), [MONDAY + dt.timedelta(days=28)])

    assert points[0].value == 10
    assert points[0].lower == 10  # a constant series has zero spread
    assert points[0].upper == 10


def test_the_day_of_week_median_is_used_when_the_bucket_is_deep_enough() -> None:
    """Four weeks where every Monday is 20 and every other day is 2. The Monday forecast
    must be 20, not the overall median of 2."""
    values = []
    for _ in range(4):
        values += [20, 2, 2, 2, 2, 2, 2]  # Monday first
    next_monday = MONDAY + dt.timedelta(days=28)

    point = forecast_series(series(values), [next_monday])[0]

    assert point.date.weekday() == 0
    assert point.value == 20
    assert point.method is ForecastMethod.SEASONAL_DOW_MEDIAN
    assert point.observations == 4


def test_a_shallow_bucket_falls_back_to_the_window_median_and_says_so() -> None:
    """Eight days gives each weekday one observation at most, so no bucket reaches the
    minimum. The fallback must be reported rather than dressed up as seasonal."""
    point = forecast_series(series([5, 5, 5, 5, 5, 5, 5, 9]), [MONDAY + dt.timedelta(days=20)])[0]

    assert point.method is ForecastMethod.OVERALL_MEDIAN
    assert point.value == 5
    assert point.observations == 8


def test_too_little_history_produces_no_number_at_all() -> None:
    """No fabricated value, no fabricated interval."""
    points = forecast_series(series([1, 2, 3]), [MONDAY + dt.timedelta(days=10)])

    assert points[0].method is ForecastMethod.INSUFFICIENT_DATA
    assert points[0].value is None
    assert points[0].lower is None
    assert points[0].upper is None
    assert points[0].observations == 3


def test_the_minimum_training_threshold_is_exact() -> None:
    horizon = [MONDAY + dt.timedelta(days=30)]

    below = forecast_series(series([4] * (MIN_TRAINING_OBSERVATIONS - 1)), horizon)
    at = forecast_series(series([4] * MIN_TRAINING_OBSERVATIONS), horizon)

    assert below[0].method is ForecastMethod.INSUFFICIENT_DATA
    assert at[0].value == 4


def test_the_median_resists_a_single_outlier() -> None:
    """One conference night must not move the baseline. A mean would give 26."""
    point = forecast_series(
        series([10, 10, 10, 10, 10, 10, 150]), [MONDAY + dt.timedelta(days=30)]
    )[0]

    assert point.value == 10


def test_the_interval_widens_with_spread() -> None:
    tight = forecast_series(series([10, 10, 10, 10, 10, 10, 11]), [MONDAY + dt.timedelta(days=30)])[
        0
    ]
    loose = forecast_series(series([2, 18, 3, 17, 4, 16, 5]), [MONDAY + dt.timedelta(days=30)])[0]

    assert (tight.upper or 0) - (tight.lower or 0) < (loose.upper or 0) - (loose.lower or 0)


def test_the_lower_bound_never_goes_negative() -> None:
    """A negative number of room nights is not a thing the interval may suggest."""
    point = forecast_series(series([0, 5, 0, 6, 0, 7, 1]), [MONDAY + dt.timedelta(days=30)])[0]

    assert point.lower is not None
    assert point.lower >= 0


def test_the_interval_is_computed_from_the_documented_constants() -> None:
    """half_width = MAD x 1.4826 x 1.96, recomputed here by hand."""
    values = [10, 10, 10, 10, 10, 20, 20]
    point = forecast_series(series(values), [MONDAY + dt.timedelta(days=30)])[0]

    centre = decimal.Decimal(10)
    deviations = sorted(abs(decimal.Decimal(v) - centre) for v in values)
    mad = deviations[len(deviations) // 2]
    expected = mad * timeseries.MAD_TO_SIGMA * timeseries.CONFIDENCE_Z

    assert point.upper == centre + expected


def test_one_point_is_returned_per_horizon_date_in_order() -> None:
    horizon = [MONDAY + dt.timedelta(days=n) for n in (40, 41, 42)]

    points = forecast_series(series([3] * 21), horizon)

    assert [point.date for point in points] == horizon


def test_forecasting_is_deterministic() -> None:
    observations = series([4, 9, 2, 7, 5, 5, 8, 1, 6, 3, 4, 9, 2, 7])
    horizon = [MONDAY + dt.timedelta(days=n) for n in (30, 31)]

    assert forecast_series(observations, horizon) == forecast_series(observations, horizon)


def test_the_model_never_reads_a_horizon_value() -> None:
    """The function is given dates, not values, for the horizon -- leakage is impossible by
    signature, not by discipline."""
    parameters = inspect.signature(forecast_series).parameters

    assert parameters["horizon"].annotation == "list[dt.date]"


# --- the models: anomalies ---------------------------------------------------------------------


def test_a_clear_spike_is_flagged_with_its_statistical_basis() -> None:
    # Ordinary variation around 10, then one enormous day. A series of pure constants would
    # have MAD = 0 and correctly yield no anomaly at all -- see the constant-series test.
    observations = series([10, 11, 9, 12, 8, 10, 11, 9, 10, 12, 9, 11, 10, 400])

    found = detect_anomalies(observations)

    assert len(found) == 1
    assert found[0].value == 400
    assert found[0].median == 10
    assert found[0].direction == "above"
    assert found[0].score > ANOMALY_THRESHOLD


def test_a_clear_collapse_is_flagged_below() -> None:
    observations = series([100, 102, 98, 101, 99, 100, 103, 97, 100, 101, 99, 102, 100, 0])

    found = detect_anomalies(observations)

    assert len(found) == 1
    assert found[0].direction == "below"
    assert found[0].score < -ANOMALY_THRESHOLD


def test_ordinary_variation_is_not_flagged() -> None:
    """Arbitrary values must not become anomalies just because they differ."""
    assert detect_anomalies(series([10, 11, 9, 12, 8, 10, 11, 9, 10, 12])) == []


def test_a_constant_series_yields_no_anomalies() -> None:
    """MAD is zero: the series has no notion of usual spread, so nothing can be unusual.
    Dividing by it would be the wrong kind of confident."""
    assert detect_anomalies(series([7] * 20)) == []


def test_too_little_history_yields_no_anomalies() -> None:
    assert detect_anomalies(series([1, 500, 1])) == []


def test_the_score_matches_the_documented_formula() -> None:
    """score = 0.6745 x (value - median) / MAD, recomputed by hand."""
    values = [10, 11, 9, 12, 8, 10, 11, 9, 10, 12, 9, 11, 10, 400]
    found = detect_anomalies(series(values))[0]

    centre = decimal.Decimal(10)
    deviations = sorted(abs(decimal.Decimal(v) - centre) for v in values)
    mad = deviations[len(deviations) // 2]
    expected = timeseries.MODIFIED_Z_CONSTANT * (decimal.Decimal(400) - centre) / mad

    assert found.score == expected
    assert found.deviation == mad


def test_anomalies_come_back_in_date_order() -> None:
    observations = series([10, 11, 9, 12, 8, 10, 500, 11, 9, 10, 12, 9, 11, 600])

    found = detect_anomalies(observations)

    assert [item.date for item in found] == sorted(item.date for item in found)


def test_anomaly_detection_is_deterministic() -> None:
    observations = series([10, 11, 9, 12, 8, 10, 11, 9, 10, 12, 9, 11, 10, 400])

    assert detect_anomalies(observations) == detect_anomalies(observations)


# --- the models: trend -------------------------------------------------------------------------


def test_a_rising_series_is_increasing() -> None:
    result = measure_trend(series([1, 1, 1, 1, 1, 1, 10, 10, 10, 10, 10, 10]))

    assert result.direction == "increasing"
    assert result.earlier_median == 1
    assert result.recent_median == 10
    assert result.relative_change == 9


def test_a_falling_series_is_decreasing() -> None:
    result = measure_trend(series([10, 10, 10, 10, 10, 10, 1, 1, 1, 1, 1, 1]))

    assert result.direction == "decreasing"
    assert result.relative_change == decimal.Decimal("-0.9")


def test_a_flat_series_is_stable() -> None:
    result = measure_trend(series([10, 12, 10, 8, 10, 10, 12, 10, 8, 10]))

    assert result.earlier_median == result.recent_median == 10
    assert result.direction == "stable"


def test_a_change_just_under_the_threshold_is_stable() -> None:
    """10 -> 10.9 is +9%, below the 10% threshold. The boundary is asserted, not assumed."""
    result = measure_trend(series(["10"] * 6 + ["10.9"] * 6))

    assert result.relative_change is not None
    assert result.relative_change == decimal.Decimal("0.09")
    assert result.relative_change < TREND_THRESHOLD
    assert result.direction == "stable"


def test_a_change_just_over_the_threshold_moves() -> None:
    result = measure_trend(series(["10"] * 6 + ["11.1"] * 6))

    assert result.relative_change is not None
    assert result.relative_change > TREND_THRESHOLD
    assert result.direction == "increasing"


def test_growth_from_nothing_has_no_relative_change() -> None:
    """Dividing by a zero baseline is undefined; the direction still follows the move."""
    result = measure_trend(series([0, 0, 0, 0, 0, 0, 4, 4, 4, 4, 4, 4]))

    assert result.direction == "increasing"
    assert result.relative_change is None


def test_a_series_of_zeros_is_stable_not_increasing() -> None:
    result = measure_trend(series([0] * 12))

    assert result.direction == "stable"


def test_too_little_history_reports_insufficient_data() -> None:
    result = measure_trend(series([1, 2, 3]))

    assert result.direction == "insufficient_data"
    assert result.earlier_median is None
    assert result.relative_change is None


def test_trend_is_deterministic() -> None:
    observations = series([1, 4, 2, 8, 3, 9, 4, 12])

    assert measure_trend(observations) == measure_trend(observations)


# --- the model layer is pure -------------------------------------------------------------------


def test_the_ml_package_contains_no_database_or_http_concepts() -> None:
    source = code_of(timeseries)

    for forbidden in [
        "sqlalchemy",
        "select(",
        "Session",
        "session",
        "fastapi",
        "HTTPException",
        "commit",
    ]:
        assert forbidden not in source, f"ml layer references {forbidden!r}"


def test_the_ml_package_imports_nothing_from_the_application() -> None:
    """It must stay reusable by anything that can produce a dated series."""
    tree = ast.parse(inspect.getsource(timeseries))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith("app."), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app."), alias.name


def test_no_heavy_ml_dependency_was_added() -> None:
    """A learner fitted to a few dozen points would be less accurate than this baseline and
    far harder to explain. The stdlib is enough."""
    source = code_of(timeseries)

    for heavy in ["numpy", "pandas", "sklearn", "scipy", "torch", "tensorflow", "prophet"]:
        assert heavy not in source, heavy


# --- layer separation ----------------------------------------------------------------------------


def test_router_contains_no_sql_or_statistics() -> None:
    source = code_of(intelligence_router)

    for forbidden in ["select(", "func.", "session.", "Session", "sqlalchemy"]:
        assert forbidden not in source, f"router references {forbidden!r}"
    # It may DESCRIBE the methodology in its OpenAPI text; it may not compute it.
    assert "median" not in identifiers_in(intelligence_router)
    assert "statistics" not in identifiers_in(intelligence_router)


def test_service_contains_no_sqlalchemy_query_construction() -> None:
    source = code_of(inspect.getmodule(IntelligenceService))

    for forbidden in ["select(", "func.", ".where(", "group_by", "order_by", "join("]:
        assert forbidden not in source, f"service references {forbidden!r}"


def test_service_never_touches_a_session() -> None:
    source = code_of(inspect.getmodule(IntelligenceService))

    assert "Session" not in source
    assert "_session" not in source
    assert "session" not in inspect.signature(IntelligenceService).parameters


def test_no_intelligence_layer_can_write() -> None:
    for module in (timeseries, inspect.getmodule(IntelligenceService), intelligence_router):
        source = code_of(module)
        for forbidden in ["commit", "rollback", "insert(", "update(", "delete(", "session.add"]:
            assert forbidden not in source, f"{module} contains {forbidden!r}"


def test_every_intelligence_route_is_a_get() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    for path, operations in paths.items():
        if "/intelligence/" in path:
            assert set(operations) == {"get"}, path


# --- no second definition of any analytics metric -------------------------------------------------


def test_there_is_no_intelligence_repository() -> None:
    """Occupancy, room revenue and booking counts keep exactly one definition. A forecasting
    layer with its own copy would drift from the dashboard within a release."""
    with pytest.raises(ModuleNotFoundError):
        __import__("app.repositories.intelligence")


def test_the_service_holds_the_analytics_repository() -> None:
    class _Session:
        pass

    stub: Any = _Session()
    scope = deps.get_scope_resolver(stub, AllowAllPolicy())
    service = deps.get_intelligence_service(stub, scope)

    assert isinstance(service, IntelligenceService)
    assert isinstance(service._repository, AnalyticsRepository)
    assert isinstance(service._scope, HotelScopeResolver)
    assert not hasattr(service, "_session")


def test_the_service_reads_series_only_through_analytics_methods() -> None:
    """Every call into the data layer is one of Stage 3B.10's own methods."""
    source = code_of(inspect.getmodule(IntelligenceService))
    used = {
        "occupied_nights_by_day",
        "bookings_created_by_day",
        "room_revenue_by_day",
        "active_room_count",
    }

    for method in used:
        assert f"self._repository.{method}(" in source
    # No ORM model is referenced for querying: the repository owns that entirely. Matched by
    # exact identifier, since RoomRevenueRow and RevenueForecastPoint both contain "Revenue".
    names = identifiers_in(inspect.getmodule(IntelligenceService))
    assert (
        names & {"BookingRoomNight", "BookingRoom", "Booking", "Revenue", "Expense", "Review"}
        == set()
    )


def test_occupancy_uses_the_analytics_status_definition() -> None:
    """Not re-stated here: the filter lives in the shared repository."""
    service_source = code_of(inspect.getmodule(IntelligenceService))

    assert "OCCUPANCY_STATUSES" not in service_source
    assert "confirmed" not in service_source
    assert "OCCUPANCY_STATUSES" in code_of(inspect.getmodule(AnalyticsRepository))


def test_room_revenue_never_comes_from_payments_or_a_list_price() -> None:
    for module in (timeseries, inspect.getmodule(IntelligenceService), intelligence_router):
        names = identifiers_in(module)
        for forbidden in ["Payment", "PaymentRepository", "base_price", "total_amount"]:
            assert forbidden not in names, f"{module} references {forbidden!r}"


# --- temporal leakage -----------------------------------------------------------------------------


def test_the_training_window_ends_before_the_horizon_begins() -> None:
    horizon, window = IntelligenceService._windows(
        dt.date(2026, 6, 1), dt.date(2026, 6, 7), training_days=30
    )

    assert window.date_to == dt.date(2026, 5, 31)
    assert window.date_to < horizon.date_from
    assert window.date_from == dt.date(2026, 5, 2)
    assert window.days == 30


@pytest.mark.parametrize("training_days", [14, 30, 90, 365])
def test_the_boundary_holds_for_every_training_length(training_days: int) -> None:
    horizon, window = IntelligenceService._windows(
        dt.date(2026, 6, 1), dt.date(2026, 6, 2), training_days=training_days
    )

    assert window.date_to == horizon.date_from - dt.timedelta(days=1)
    assert window.days == training_days


def test_only_the_on_the_books_read_looks_forward() -> None:
    """Two forward reads exist and both are labelled actuals, never training input."""
    source = inspect.getsource(IntelligenceService.occupancy_forecast)

    assert "horizon.date_from, horizon.date_to" in source
    # The series handed to the model is built from the training window only.
    assert "self._occupancy_observations(hotel.id, window)" in source
    assert "forecast_series(observations, horizon_days)" in source


def test_the_forecast_response_separates_facts_from_predictions() -> None:
    fields = set(OccupancyForecastPoint.model_fields)

    assert "on_the_books_room_nights" in fields  # actual
    assert "predicted_room_nights" in fields  # prediction
    assert "on_the_books_room_revenue" in set(RevenueForecastPoint.model_fields)
    assert "predicted_room_revenue" in set(RevenueForecastPoint.model_fields)


# --- date handling --------------------------------------------------------------------------------


def test_a_reversed_horizon_is_rejected() -> None:
    with pytest.raises(ValidationError, match="date_to must not be earlier"):
        IntelligenceService._windows(dt.date(2026, 6, 7), dt.date(2026, 6, 1), training_days=30)


def test_an_over_long_horizon_is_rejected() -> None:
    start = dt.date(2026, 6, 1)
    with pytest.raises(ValidationError, match="maximum"):
        IntelligenceService._windows(
            start, start + dt.timedelta(days=MAX_HORIZON_DAYS), training_days=30
        )


def test_the_maximum_horizon_itself_is_accepted() -> None:
    start = dt.date(2026, 6, 1)
    horizon, _ = IntelligenceService._windows(
        start, start + dt.timedelta(days=MAX_HORIZON_DAYS - 1), training_days=30
    )

    assert horizon.days == MAX_HORIZON_DAYS


def test_a_reversed_observation_window_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IntelligenceService._observation_window(dt.date(2026, 6, 7), dt.date(2026, 6, 1))


def test_a_single_day_window_is_one_day() -> None:
    window = IntelligenceService._observation_window(dt.date(2026, 6, 1), dt.date(2026, 6, 1))

    assert window.days == 1


def test_no_route_carries_a_today_relative_default() -> None:
    """A default window would make two identical requests mean different things on different
    days, which is the opposite of reproducible."""
    paths = create_app(Settings(environment="test")).openapi()["paths"]

    for path, operations in paths.items():
        if "/intelligence/" not in path:
            continue
        required = {p["name"] for p in operations["get"].get("parameters", []) if p.get("required")}
        assert {"date_from", "date_to"} <= required, path


def test_training_days_is_bounded() -> None:
    paths = create_app(Settings(environment="test")).openapi()["paths"]
    forecast = paths["/api/v1/hotels/{hotel_public_id}/intelligence/forecast/occupancy"]["get"]
    parameter = next(p for p in forecast["parameters"] if p["name"] == "training_days")

    assert parameter["schema"]["minimum"] == MIN_TRAINING_DAYS
    assert parameter["schema"]["maximum"] == MAX_TRAINING_DAYS


# --- scoping --------------------------------------------------------------------------------------


def _service() -> IntelligenceService:
    class _NoHotels:
        def get_by_public_id(self, _: uuid.UUID) -> None:
            return None

    class _NoRoomTypes:
        def get_by_hotel_and_code(self, _hotel_id: int, _code: str) -> None:
            return None

    scope = HotelScopeResolver(_NoHotels(), _NoRoomTypes(), AllowAllPolicy())  # type: ignore[arg-type]
    return IntelligenceService(object(), scope)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.occupancy_forecast(uuid.uuid4(), dt.date(2026, 6, 1), dt.date(2026, 6, 2), 30),
        lambda s: s.revenue_forecast(uuid.uuid4(), dt.date(2026, 6, 1), dt.date(2026, 6, 2), 30),
        lambda s: s.demand_trend(uuid.uuid4(), dt.date(2026, 6, 1), dt.date(2026, 6, 2)),
        lambda s: s.anomalies(uuid.uuid4(), dt.date(2026, 6, 1), dt.date(2026, 6, 2)),
        lambda s: s.insights(uuid.uuid4(), dt.date(2026, 6, 1), dt.date(2026, 6, 2), 7),
    ],
)
def test_every_operation_resolves_the_hotel_first(call: Any) -> None:
    with pytest.raises(NotFoundError, match=r"Hotel not found\."):
        call(_service())


def test_no_repository_call_takes_a_public_id() -> None:
    """The tenant is established before any query is issued."""
    source = code_of(inspect.getmodule(IntelligenceService))

    assert "self._repository.occupied_nights_by_day(hotel.id" in source
    assert "self._repository.active_room_count(hotel.id)" in source


# --- response contracts ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema", RESPONSE_SCHEMAS)
def test_no_response_exposes_an_internal_key(schema: type) -> None:
    fields = set(schema.model_fields)  # type: ignore[attr-defined]

    for forbidden in ["id", "hotel_id", "booking_id", "room_id", "guest_id", "category_id"]:
        assert forbidden not in fields
    assert "hotel_public_id" in fields


@pytest.mark.parametrize("schema", RESPONSE_SCHEMAS)
def test_every_response_carries_model_provenance(schema: type) -> None:
    assert "model" in schema.model_fields  # type: ignore[attr-defined]


def test_the_method_literal_matches_the_model_enum() -> None:
    assert set(get_args(ForecastMethodLiteral)) == {method.value for method in ForecastMethod}


def test_the_model_is_named_and_versioned() -> None:
    assert MODEL_NAME
    assert MODEL_VERSION
    metadata = IntelligenceService._metadata()
    assert metadata.model_name == MODEL_NAME
    assert metadata.model_version == MODEL_VERSION
    assert metadata.methodology


def test_predictions_and_intervals_are_all_nullable() -> None:
    """Insufficient data must be representable without inventing a number."""
    for field in ("predicted_room_nights", "interval_lower", "interval_upper", "confidence_level"):
        assert type(None) in get_args(OccupancyForecastPoint.model_fields[field].annotation)


def test_an_insight_carries_every_required_part() -> None:
    fields = set(Insight.model_fields)

    assert {
        "type",
        "severity",
        "title",
        "explanation",
        "supporting_metrics",
        "date_from",
        "date_to",
        "confidence",
    } == fields


def test_revenue_forecasts_are_per_currency_never_a_total() -> None:
    fields = set(RevenueForecastResponse.model_fields)

    assert "currencies" in fields
    for banned in ["total", "total_revenue", "amount", "combined"]:
        assert banned not in fields
    # The currency lives on the per-currency wrapper, not on each point.
    assert "currency" not in RevenueForecastPoint.model_fields
    assert "currency" in set(
        get_args(RevenueForecastResponse.model_fields["currencies"].annotation)[0].model_fields
    )


def test_no_fx_or_conversion_exists_anywhere_in_the_domain() -> None:
    for module in (timeseries, inspect.getmodule(IntelligenceService), intelligence_router):
        source = code_of(module).lower()
        for forbidden in ["fx", "exchange_rate", "convert_currency", "base_currency"]:
            assert forbidden not in source, forbidden


# --- the boundary this stage must not cross -------------------------------------------------------


def test_nothing_agentic_or_generative_was_introduced() -> None:
    for module in (timeseries, inspect.getmodule(IntelligenceService), intelligence_router):
        source = code_of(module).lower()
        for forbidden in [
            "openai",
            "anthropic",
            "llm",
            "embedding",
            "vector",
            "chatbot",
            "agent",
            "prompt",
            "httpx",
            "requests.",
        ]:
            assert forbidden not in source, forbidden


def test_no_background_job_or_queue_was_introduced() -> None:
    for module in (timeseries, inspect.getmodule(IntelligenceService), intelligence_router):
        source = code_of(module).lower()
        for forbidden in ["celery", "redis", "kafka", "backgroundtask", "threading", "cron"]:
            assert forbidden not in source, forbidden


def test_nothing_changes_hotel_operations() -> None:
    """No pricing, no reallocation, no automatic posting."""
    for module in (timeseries, inspect.getmodule(IntelligenceService), intelligence_router):
        source = code_of(module).lower()
        for forbidden in ["set_rate", "apply_price", "reallocate", "auto_post", "recommend"]:
            assert forbidden not in source, forbidden


def test_no_flat_or_global_intelligence_route_exists() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert "/api/v1/intelligence" not in paths
    for banned in ("/api/v1/forecast", "/api/v1/ml", "/api/v1/predictions"):
        assert not any(p.startswith(banned) for p in paths), banned


def test_the_intelligence_surface_is_exactly_five_reports() -> None:
    paths = set(create_app(Settings(environment="test")).openapi()["paths"])

    assert {p for p in paths if "intelligence" in p} == {
        "/api/v1/hotels/{hotel_public_id}/intelligence/forecast/occupancy",
        "/api/v1/hotels/{hotel_public_id}/intelligence/forecast/revenue",
        "/api/v1/hotels/{hotel_public_id}/intelligence/demand-trend",
        "/api/v1/hotels/{hotel_public_id}/intelligence/anomalies",
        "/api/v1/hotels/{hotel_public_id}/intelligence/insights",
    }


def test_daily_hotel_metrics_is_still_untouched() -> None:
    """Stage 3B.10 found it empty with no population job. This stage did not quietly become
    that job."""
    for module in (timeseries, inspect.getmodule(IntelligenceService), intelligence_router):
        source = code_of(module)
        assert "DailyHotelMetric" not in source
        assert "daily_hotel_metrics" not in source


def test_the_bucket_minimum_is_at_least_two() -> None:
    """With one observation a 'seasonal' median is just that value wearing a hat."""
    assert MIN_BUCKET_OBSERVATIONS >= 2
