"""Stage 7.12 -- the attention list, its protocol and its offline evaluation, without a database.

    A. what the service composes, and the order it produces
    B. every figure traces to its source; every sentence is a template filled from figures
    C. closed templates, and no imperative or operational vocabulary
    D. the protocol and the method block
    E. the route and the seventh tool
    F. the insight_ranking_v1 evaluation: scorers, methods, the pinned report
    G. structure

`tests/integration/test_priorities_api.py` proves tenancy and authorization over PostgreSQL.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import math
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from app.api.v1.endpoints import priorities as priorities_endpoint
from app.copilot.contracts import HOTEL_IDENTIFIER_FIELD, ToolContext, ToolServices
from app.copilot.registry import build_default_registry, property_names
from app.copilot.tools import priorities as priorities_tool
from app.core.config import Settings
from app.main import create_app
from app.ml import ranking_protocol
from app.ml.ranking_protocol import HORIZON_DAYS, PROTOCOL, TRAINING_DAYS, K
from app.ml.timeseries import MODEL_NAME, forecast_series
from app.models.enums import HotelRole
from app.schemas.insight import PRIORITY_KINDS, PrioritiesResponse
from app.schemas.intelligence import (
    AnomalyPoint,
    AnomalyResponse,
    DemandTrendResponse,
    ForecastHorizon,
    ModelMetadata,
    ObservationWindow,
    OccupancyForecastPoint,
    OccupancyForecastResponse,
    TrainingWindow,
)
from app.services import insight as insight_module
from app.services.insight import MAX_ANOMALY_ITEMS, TEMPLATES, VIEW_PATHS, InsightService
from tests.evaluation import insight_ranking
from tests.evaluation.insight_ranking import (
    REPORT,
    baseline_scores,
    ndcg_at_k,
    platform_scores,
    precision_at_k,
    ranked,
)

APP = Path(__file__).resolve().parents[2] / "backend" / "app"
HOTEL = uuid.UUID("11111111-1111-4111-8111-111111111111")
WINDOW = (dt.date(2026, 5, 1), dt.date(2026, 5, 31))
MODEL = ModelMetadata(
    model_name=MODEL_NAME,
    model_version="1.0.0",
    methodology="m",
    generated_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
)


def point(day: int, predicted: str | None, booked: int = 5) -> OccupancyForecastPoint:
    return OccupancyForecastPoint(
        date=dt.date(2026, 6, day),
        on_the_books_room_nights=booked,
        available_room_nights=20,
        predicted_room_nights=None if predicted is None else Decimal(predicted),
        predicted_occupancy_rate=None if predicted is None else Decimal(predicted) / 20,
        interval_lower=None if predicted is None else Decimal(predicted) - 2,
        interval_upper=None if predicted is None else Decimal(predicted) + 2,
        confidence_level=None if predicted is None else Decimal("0.95"),
        method="insufficient_data" if predicted is None else "seasonal_dow_median",
        observations=90,
        capacity_clamped=False,
    )


def anomaly(day: int, z: str, metric: str = "occupied_room_nights") -> AnomalyPoint:
    return AnomalyPoint(
        metric=metric,
        date=dt.date(2026, 5, day),
        value=Decimal("19"),
        median=Decimal("12"),
        median_absolute_deviation=Decimal("1.5"),
        modified_z_score=Decimal(z),
        threshold=Decimal("3.5"),
        direction="above" if Decimal(z) > 0 else "below",
    )


class FakeIntelligence:
    """`IntelligenceService`'s three methods, returning fixed responses and recording calls."""

    def __init__(
        self,
        points: list[OccupancyForecastPoint],
        anomalies: list[AnomalyPoint],
        direction: str = "increasing",
    ) -> None:
        self.points = points
        self.anomaly_points = anomalies
        self.direction = direction
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def window(self, date_from: dt.date, date_to: dt.date) -> ObservationWindow:
        days = (date_to - date_from).days + 1
        return ObservationWindow(date_from=date_from, date_to=date_to, days=days, observations=days)

    def anomalies(self, hotel: uuid.UUID, date_from: dt.date, date_to: dt.date) -> AnomalyResponse:
        self.calls.append(("anomalies", (hotel, date_from, date_to)))
        return AnomalyResponse(
            hotel_public_id=hotel,
            model=MODEL,
            window=self.window(date_from, date_to),
            metrics_scanned=["occupied_room_nights"],
            anomalies=self.anomaly_points,
        )

    def demand_trend(
        self, hotel: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> DemandTrendResponse:
        self.calls.append(("demand_trend", (hotel, date_from, date_to)))
        return DemandTrendResponse(
            hotel_public_id=hotel,
            model=MODEL,
            window=self.window(date_from, date_to),
            metric="bookings_created",
            direction=cast(Any, self.direction),
            earlier_median=Decimal("4"),
            recent_median=Decimal("6"),
            relative_change=Decimal("0.5000"),
            threshold=Decimal("0.10"),
        )

    def occupancy_forecast(
        self, hotel: uuid.UUID, date_from: dt.date, date_to: dt.date, training_days: int
    ) -> OccupancyForecastResponse:
        self.calls.append(("occupancy_forecast", (hotel, date_from, date_to, training_days)))
        return OccupancyForecastResponse(
            hotel_public_id=hotel,
            model=MODEL,
            training_window=TrainingWindow(
                date_from=date_from - dt.timedelta(days=training_days),
                date_to=date_from - dt.timedelta(days=1),
                days=training_days,
                observations=training_days,
            ),
            horizon=ForecastHorizon(
                date_from=date_from, date_to=date_to, days=(date_to - date_from).days + 1
            ),
            points=self.points,
        )


class FakeScope:
    def __init__(self) -> None:
        self.resolved: list[uuid.UUID] = []

    def require_hotel(self, hotel: uuid.UUID) -> Any:
        self.resolved.append(hotel)

        class Hotel:
            public_id = hotel

        return Hotel()


def fourteen(values: list[str | None]) -> list[OccupancyForecastPoint]:
    return [point(day, value) for day, value in enumerate(values, 1)]


DEFAULT_POINTS = fourteen(
    ["10", "18", "12", "18", "9", "15", "16", "11", "10", "19", "8", "7", "14", "12"]
)
DEFAULT_ANOMALIES = [anomaly(3, "4.1"), anomaly(9, "-5.2"), anomaly(20, "3.9"), anomaly(25, "4.8")]


def build(
    points: list[OccupancyForecastPoint] | None = None,
    anomalies: list[AnomalyPoint] | None = None,
    direction: str = "increasing",
) -> tuple[InsightService, FakeIntelligence, FakeScope]:
    intelligence = FakeIntelligence(
        DEFAULT_POINTS if points is None else points,
        DEFAULT_ANOMALIES if anomalies is None else anomalies,
        direction,
    )
    scope = FakeScope()
    return InsightService(cast(Any, intelligence), cast(Any, scope)), intelligence, scope


def priorities(**kwargs: Any) -> PrioritiesResponse:
    service, _, _ = build(**kwargs)
    return service.priorities(HOTEL, *WINDOW)


# ======================================================================================
# A. Composition and order
# ======================================================================================


def test_the_list_is_peaks_then_anomalies_then_the_trend_ranked_contiguously() -> None:
    response = priorities()
    kinds = [item.kind for item in response.items]
    assert kinds == ["upcoming_peak_day"] * K + ["observed_anomaly"] * MAX_ANOMALY_ITEMS + [
        "demand_trend"
    ]
    assert [item.rank for item in response.items] == list(range(1, len(kinds) + 1))


def test_peak_days_are_the_k_busiest_by_forecast_with_ties_broken_by_date() -> None:
    peaks = [i for i in priorities().items if i.kind == "upcoming_peak_day"]
    # 19 on the 10th; 18 on the 2nd and the 4th (tie: earlier first).
    assert [item.date_from for item in peaks] == [
        dt.date(2026, 6, 10),
        dt.date(2026, 6, 2),
        dt.date(2026, 6, 4),
    ]


def test_anomalies_are_the_strongest_by_absolute_z_score() -> None:
    found = [i for i in priorities().items if i.kind == "observed_anomaly"]
    assert [item.date_from.day for item in found] == [9, 25, 3]  # |-5.2|, 4.8, 4.1


def test_the_order_is_deterministic() -> None:
    first, second = priorities(), priorities()
    assert first.model_dump() == second.model_dump()
    shuffled = priorities(points=list(reversed(DEFAULT_POINTS)), anomalies=DEFAULT_ANOMALIES[::-1])
    assert [i.model_dump() for i in shuffled.items] == [i.model_dump() for i in first.items]


def test_a_day_the_forecast_cannot_score_is_never_listed() -> None:
    response = priorities(points=fourteen([None] * 14))
    assert [i.kind for i in response.items if i.kind == "upcoming_peak_day"] == []


@pytest.mark.parametrize("direction", ["stable", "insufficient_data"])
def test_a_trend_that_did_not_move_is_not_listed(direction: str) -> None:
    assert "demand_trend" not in [i.kind for i in priorities(direction=direction).items]


def test_no_anomalies_means_no_anomaly_items() -> None:
    assert "observed_anomaly" not in [i.kind for i in priorities(anomalies=[]).items]


def test_it_reads_exactly_the_window_and_the_protocols_horizon() -> None:
    service, intelligence, scope = build()
    response = service.priorities(HOTEL, *WINDOW)
    assert intelligence.calls == [
        ("anomalies", (HOTEL, *WINDOW)),
        ("demand_trend", (HOTEL, *WINDOW)),
        (
            "occupancy_forecast",
            (HOTEL, dt.date(2026, 6, 1), dt.date(2026, 6, 14), TRAINING_DAYS),
        ),
    ]
    assert scope.resolved == [HOTEL]
    assert (response.horizon.date_from, response.horizon.days) == (dt.date(2026, 6, 1), 14)


# ======================================================================================
# B. Traceability
# ======================================================================================


def test_every_peak_figure_is_the_forecast_points_own_value() -> None:
    by_date = {p.date: p for p in DEFAULT_POINTS}
    for item in [i for i in priorities().items if i.kind == "upcoming_peak_day"]:
        source = by_date[item.date_from]
        figures = {f.name: f for f in item.figures}
        assert {f.source for f in item.figures} == {"IntelligenceService.occupancy_forecast"}
        assert figures["predicted_room_nights"].value == str(source.predicted_room_nights)
        assert figures["on_the_books_room_nights"].value == str(source.on_the_books_room_nights)
        assert figures["available_room_nights"].value == str(source.available_room_nights)
        assert figures["interval_lower"].value == str(source.interval_lower)
        assert figures["interval_upper"].value == str(source.interval_upper)


def test_every_anomaly_figure_is_the_anomaly_points_own_value() -> None:
    by_day = {a.date: a for a in DEFAULT_ANOMALIES}
    for item in [i for i in priorities().items if i.kind == "observed_anomaly"]:
        source = by_day[item.date_from]
        values = {f.name: f.value for f in item.figures}
        assert {f.source for f in item.figures} == {"IntelligenceService.anomalies"}
        assert values == {
            "value": str(source.value),
            "median": str(source.median),
            "median_absolute_deviation": str(source.median_absolute_deviation),
            "modified_z_score": str(source.modified_z_score),
            "threshold": str(source.threshold),
        }
        assert item.measure == source.metric


def test_every_trend_figure_is_the_trend_responses_own_value() -> None:
    [item] = [i for i in priorities().items if i.kind == "demand_trend"]
    assert {f.name: f.value for f in item.figures} == {
        "earlier_median": "4",
        "recent_median": "6",
        "relative_change": "0.5000",
        "threshold": "0.10",
    }
    assert {f.source for f in item.figures} == {"IntelligenceService.demand_trend"}


NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def test_every_number_in_a_sentence_is_a_figure_a_date_or_a_declared_constant() -> None:
    allowed_constants = {str(TRAINING_DAYS), str(HORIZON_DAYS), "1"}  # "insight_ranking_v1"
    for item in priorities().items:
        figures = {f.value for f in item.figures}
        dates = {item.date_from.isoformat(), item.date_to.isoformat()}
        rank_like = {str(n) for n in range(1, K + 1)}
        for sentence in (item.observation, item.comparison or "", item.limitation):
            stripped = sentence
            for date in dates:
                stripped = stripped.replace(date, " ")
            for number in NUMBER.findall(stripped):
                assert number in figures | allowed_constants | rank_like, (
                    item.kind,
                    number,
                    sentence,
                )


def test_look_at_names_an_existing_get_route() -> None:
    paths = create_app(Settings(environment="test", debug=True)).openapi()["paths"]
    for view, path in VIEW_PATHS.items():
        assert path in paths, view
        assert set(paths[path]) == {"get"}, view
    for item in priorities().items:
        assert item.look_at.path == VIEW_PATHS[item.look_at.view]


# ======================================================================================
# C. Closed templates, and no advice
# ======================================================================================

#: Vocabulary no template may contain: instructions, and the operational acts the platform does
#: not take. Whole words, case-insensitive.
BANNED = (
    "should",
    "must",
    "recommend",
    "recommended",
    "recommendation",
    "consider",
    "suggest",
    "advise",
    "raise",
    "lower",
    "reduce",
    "increase",
    "decrease",
    "cut",
    "discount",
    "price",
    "prices",
    "pricing",
    "rate",
    "rates",
    "staff",
    "staffing",
    "hire",
    "schedule",
    "allocate",
    "reallocate",
    "assign",
    "adjust",
    "apply",
    "execute",
    "automate",
    "automatic",
    "optimise",
    "optimize",
    "action",
    "you",
)


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def test_the_templates_are_a_closed_set_of_nine() -> None:
    assert len(TEMPLATES) == 9
    template_names = {
        name
        for name, value in vars(insight_module).items()
        if isinstance(value, str) and name.endswith(("_OBSERVATION", "_COMPARISON", "_LIMITATION"))
    }
    assert len(template_names) == 9
    assert {getattr(insight_module, name) for name in template_names} == set(TEMPLATES)


@pytest.mark.parametrize("template", TEMPLATES)
def test_no_template_carries_an_instruction_or_an_operational_act(template: str) -> None:
    assert not (words(template) & set(BANNED)), template


def test_no_rendered_sentence_carries_one_either() -> None:
    for item in priorities().items:
        for sentence in (item.observation, item.comparison or "", item.limitation):
            assert not (words(sentence) & set(BANNED)), sentence


def test_every_sentence_is_one_of_the_templates_filled() -> None:
    patterns = [
        re.compile("^" + re.escape(t).replace(r"\{", "{").replace(r"\}", "}") + "$")
        for t in TEMPLATES
    ]
    shapes = [re.sub(r"\{[a-z_]+\}", "(.+)", p.pattern) for p in patterns]
    for item in priorities().items:
        for sentence in (item.observation, item.comparison or "", item.limitation):
            if sentence:
                assert any(re.match(shape, sentence) for shape in shapes), sentence


def test_kinds_and_views_are_closed() -> None:
    assert PRIORITY_KINDS == ("upcoming_peak_day", "observed_anomaly", "demand_trend")
    assert set(VIEW_PATHS) == {"occupancy_forecast", "anomalies", "demand_trend"}


# ======================================================================================
# D. The protocol and the method block
# ======================================================================================


def test_the_protocol_is_frozen_and_declares_k_and_the_comparison_rule() -> None:
    assert PROTOCOL.identity == "insight_ranking_v1"
    assert PROTOCOL.checksum == "37245decde495fbdf94f8652d8027c589b69d45052119d3ef3a7d493c19f7efa"
    assert (PROTOCOL.k, PROTOCOL.horizon_days, PROTOCOL.step_days, PROTOCOL.training_days) == (
        3,
        14,
        14,
        90,
    )
    assert PROTOCOL.metrics == ("precision_at_k", "ndcg_at_k")
    assert PROTOCOL.platform_method == MODEL_NAME == "seasonal-naive-dow-median"
    assert PROTOCOL.baseline_method == "last-week-same-weekday"
    assert "no winner" in PROTOCOL.comparison_rule
    assert "no business-value claim" in PROTOCOL.comparison_rule
    assert PROTOCOL.dataset_sha256 == (
        "904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d"
    )


def test_the_method_block_names_what_ranked_the_days_and_how_it_is_measured() -> None:
    method = priorities().method
    assert method.ranking == MODEL_NAME
    assert (method.training_days, method.horizon_days, method.k) == (TRAINING_DAYS, HORIZON_DAYS, K)
    assert method.evaluation_protocol == "insight_ranking_v1"
    assert method.evaluation_protocol_checksum == PROTOCOL.checksum
    assert method.baseline == "last-week-same-weekday"


def test_the_learned_demand_model_is_not_the_ranking_method() -> None:
    imported = imports_of(Path(inspect.getfile(insight_module)))
    assert not any("ml_serving" in i or "ml.serving" in i or "prediction" in i for i in imported)
    assert "app.ml.timeseries" in imported


# ======================================================================================
# E. The route and the seventh tool
# ======================================================================================


def test_the_route_is_one_read_only_get_with_a_window_and_nothing_else() -> None:
    [route] = [r for r in priorities_endpoint.router.routes if hasattr(r, "methods")]
    assert cast(Any, route).path == "/hotels/{hotel_public_id}/intelligence/priorities"
    assert cast(Any, route).methods == {"GET"}
    spec = create_app(Settings(environment="test", debug=True)).openapi()
    operation = spec["paths"]["/api/v1/hotels/{hotel_public_id}/intelligence/priorities"]["get"]
    assert sorted(p["name"] for p in operation["parameters"]) == [
        "date_from",
        "date_to",
        "hotel_public_id",
    ]


def test_the_seventh_tool_is_a_viewer_read_of_the_same_list() -> None:
    registry = build_default_registry()
    assert len(registry.names()) == 7
    contract = priorities_tool.CONTRACT
    assert contract.name == "get_hotel_priorities" and contract.name in registry
    assert contract.min_role == HotelRole.VIEWER
    assert contract.side_effect == "none"
    assert contract.delegates_to == "InsightService.priorities"
    schema = contract.input_schema()
    assert sorted(schema["properties"]) == ["date_from", "date_to"]
    assert schema["additionalProperties"] is False
    assert set(contract.output_model.model_fields) == (
        set(PrioritiesResponse.model_fields) - {HOTEL_IDENTIFIER_FIELD}
    )
    for model_schema in (schema, contract.output_model.model_json_schema()):
        for name in property_names(model_schema):
            assert "hotel" not in name and not name.endswith("_id"), name


def test_the_tool_uses_the_context_hotel_and_returns_the_list_without_it() -> None:
    service, intelligence, _ = build()
    context = ToolContext(
        hotel_public_id=HOTEL,
        services=ToolServices(
            analytics=cast(Any, None),
            demand_prediction=cast(Any, None),
            forecast_performance=cast(Any, None),
            knowledge=cast(Any, None),
            insight=service,
        ),
    )
    output = priorities_tool.run(
        context, priorities_tool.HotelPrioritiesArguments(date_from=WINDOW[0], date_to=WINDOW[1])
    )
    assert {call[1][0] for call in intelligence.calls} == {HOTEL}
    assert HOTEL_IDENTIFIER_FIELD not in output.model_dump()
    assert len(output.items) == K + MAX_ANOMALY_ITEMS + 1


def test_the_tool_refuses_a_model_supplied_hotel() -> None:
    with pytest.raises(ValueError):
        priorities_tool.HotelPrioritiesArguments.model_validate(
            {"date_from": "2026-05-01", "date_to": "2026-05-31", "hotel_public_id": str(HOTEL)}
        )


# ======================================================================================
# F. insight_ranking_v1: the scorers, the methods, the pinned report
# ======================================================================================

D = [dt.date(2026, 1, n) for n in range(1, 6)]


def test_precision_at_k_counts_shared_top_k_days() -> None:
    assert precision_at_k([D[0], D[1], D[2]], [D[0], D[2], D[3]], 3) == pytest.approx(2 / 3)
    assert precision_at_k([D[4], D[3]], [D[0], D[1]], 2) == 0.0


def test_ndcg_at_k_matches_a_hand_computation() -> None:
    gains = {D[0]: 10.0, D[1]: 6.0, D[2]: 3.0, D[3]: 0.0}
    ideal = ranked(gains)
    predicted = [D[1], D[0], D[3], D[2]]
    dcg = 6 / math.log2(2) + 10 / math.log2(3) + 0 / math.log2(4)
    idcg = 10 / math.log2(2) + 6 / math.log2(3) + 3 / math.log2(4)
    assert ndcg_at_k(predicted, ideal, gains, 3) == pytest.approx(dcg / idcg)
    assert ndcg_at_k(ideal, ideal, gains, 3) == pytest.approx(1.0)
    assert ndcg_at_k(predicted, ideal, dict.fromkeys(D, 0.0), 3) is None


def test_ties_rank_by_date_ascending() -> None:
    assert ranked({D[2]: 5.0, D[0]: 5.0, D[1]: 7.0}) == [D[1], D[0], D[2]]


def test_the_baseline_never_reads_a_day_after_the_origin() -> None:
    origin = dt.date(2026, 3, 11)  # a Wednesday
    history = {origin - dt.timedelta(days=n): n for n in range(0, 7)}
    horizon = [origin + dt.timedelta(days=n) for n in range(1, 15)]
    scores = baseline_scores(history, origin, horizon)
    assert scores is not None
    for day in horizon:
        known = origin - dt.timedelta(days=(origin.weekday() - day.weekday()) % 7)
        assert known <= origin and known.weekday() == day.weekday()
        assert scores[day] == history[known]


def test_the_platform_method_is_the_applications_own_forecast() -> None:
    assert insight_ranking.forecast_series is forecast_series
    history = {dt.date(2026, 1, 1) + dt.timedelta(days=n): n % 7 for n in range(120)}
    origin = dt.date(2026, 4, 30)
    horizon = [origin + dt.timedelta(days=n) for n in range(1, 15)]
    scores = platform_scores(history, origin, horizon, TRAINING_DAYS)
    assert scores is not None and set(scores) == set(horizon)
    too_short = platform_scores({origin: 1}, origin, horizon, TRAINING_DAYS)
    assert too_short is None


def test_the_pinned_report_is_what_the_protocol_computes_today() -> None:
    assert insight_ranking.canonical(insight_ranking.run()) == REPORT.read_text(encoding="utf-8")


def test_the_report_shows_both_methods_side_by_side_and_claims_nothing() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["protocol"] == {"identity": PROTOCOL.identity, "checksum": PROTOCOL.checksum}
    for summary in (report["pooled"], *report["per_hotel"].values()):
        for metric in ("precision_at_k", "ndcg_at_k"):
            assert set(summary[metric]) == {
                "platform_mean",
                "baseline_mean",
                "platform_higher",
                "platform_lower",
                "equal",
            }
            counts = summary[metric]
            assert (
                counts["platform_higher"] + counts["platform_lower"] + counts["equal"]
                == (summary["scored"])
            )
    flat = json.dumps(report).lower()
    for claim in ("better", "outperform", "improve", "winner", "significant", "revenue gain"):
        assert claim not in flat.replace("no winner", ""), claim
    assert "nothing is claimed about hotel operations" in report["statement"]


def test_the_evaluation_refuses_any_other_dataset(tmp_path: Path) -> None:
    other = tmp_path / "demand_daily_v1.csv"
    source = Path(insight_ranking.DEFAULT_DATASET).read_bytes()
    # Half the frozen file: well-formed, but not the bytes the protocol names.
    other.write_bytes(source[: len(source) // 2].rsplit(b"\n", 1)[0] + b"\n")
    with pytest.raises(ValueError, match="not the dataset"):
        insight_ranking.run(path=other)


# ======================================================================================
# G. Structure
# ======================================================================================


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_service_writes_nothing_and_calls_no_model() -> None:
    imported = imports_of(Path(inspect.getfile(insight_module)))
    assert not any(
        i.startswith(("sqlalchemy", "app.llm", "app.copilot", "app.repositories")) for i in imported
    )
    code = ast.unparse(ast.parse(Path(inspect.getfile(insight_module)).read_text(encoding="utf-8")))
    assert "commit" not in code and "session" not in code.lower()


def code_without_docstrings(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree).lower()


@pytest.mark.parametrize(
    "path",
    [
        APP / "services" / "insight.py",
        APP / "schemas" / "insight.py",
        APP / "api" / "v1" / "endpoints" / "priorities.py",
        APP / "copilot" / "tools" / "priorities.py",
        APP / "ml" / "ranking_protocol.py",
    ],
    ids=lambda p: p.name,
)
def test_the_word_recommend_appears_nowhere_in_the_new_code(path: Path) -> None:
    assert "recommend" not in code_without_docstrings(path), path.name


def test_the_protocol_module_is_pure() -> None:
    imported = imports_of(Path(inspect.getfile(ranking_protocol)))
    assert imported <= {
        "__future__",
        "hashlib",
        "json",
        "dataclasses",
        "typing",
        "app.ml.timeseries",
    }
