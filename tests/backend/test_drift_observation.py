"""Stage 6.10: the observation protocol, the summaries, the movement, and the event.

Everything here runs without a database. What needs one -- summaries over real stored rows, the
inherited selection rule as the database executes it, cross-tenant isolation, and the proof that
an observation writes nothing -- is in ``tests/integration/test_drift_observation_api.py``
against real PostgreSQL.

The quantile convention gets two kinds of test on purpose: hand-computed values from the
declared formula, and a cross-check against ``statistics.quantiles(method="inclusive")``. The
first says the rule is what the protocol claims; the second says the rule is the one everybody
else means by it.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import logging
import re
import statistics
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.main import create_app
from app.ml.accuracy_protocol import SEGMENT_BELOW_CALIBRATION, SEGMENT_WITHIN_CALIBRATION
from app.ml.drift import (
    ObservationError,
    ObservedPrediction,
    assert_one_per_group,
    by_segment,
    difference,
    partition,
    quantile,
    summarise,
    value_of,
)
from app.ml.drift_protocol import OBSERVED_FIELDS, PROTOCOL
from app.ml.serving import APPROVED_MODEL
from app.schemas.ml_drift import DistributionObservation
from app.services.ml_drift import DemandDistributionService

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPOSITORY_ROOT / "backend" / "app"

APPROVED_DIGEST = APPROVED_MODEL.canonical_model_digest
WINDOW = (dt.date(2026, 4, 1), dt.date(2026, 4, 30))
BASELINE = (dt.date(2026, 3, 1), dt.date(2026, 3, 31))

#: Every field name that would turn an observation into a verdict. None may appear anywhere in
#: the protocol or in the result tree.
VERDICT_WORDS = (
    "drift",
    "drifted",
    "drift_detected",
    "anomaly",
    "threshold",
    "alert",
    "passed",
    "failed",
    "severity",
    "status",
    "verdict",
)


def features(level: float, **overrides: float) -> dict[str, float]:
    """A feature vector whose three lag values are all *level*."""
    base = {
        "day_of_week": 1.0,
        "day_of_month": 2.0,
        "month": 4.0,
        "week_of_year": 14.0,
        "day_of_year": 92.0,
        "is_weekend": 0.0,
        "demand_lag_7": level,
        "demand_lag_14": level,
        "demand_lag_28": level,
    }
    return {**base, **overrides}


def prediction(
    day: int,
    predicted: float,
    *,
    level: float = 100.0,
    month: int = 4,
    model_version: str = "demand_baseline_v1",
    digest: str = APPROVED_DIGEST,
    feature_digest: str | None = None,
    **overrides: float,
) -> ObservedPrediction:
    target = dt.date(2026, month, day)
    return ObservedPrediction(
        target_date=target,
        model_version=model_version,
        canonical_model_digest=digest,
        feature_digest=feature_digest or f"d-{target.isoformat()}-{model_version}",
        feature_values=features(level, **overrides),
        predicted_room_nights=predicted,
    )


# --- capturing what the service logs ---------------------------------------------------------


class RecordingHandler(logging.Handler):
    """Collects records instead of formatting them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def events() -> Iterator[RecordingHandler]:
    """Everything ``app.services.ml_drift`` logs, captured at the source.

    Not caplog: it listens on root, and ``configure_logging`` replaces root's handlers the first
    time any test starts an application, so a caplog assertion here would pass alone and capture
    nothing in a full run.
    """
    handler = RecordingHandler()
    logger = logging.getLogger("app.services.ml_drift")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


#: Field names on a record that this stage did not put there.
FIELDS_NOT_ATTACHED_BY_THIS_STAGE = frozenset(
    logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None).__dict__
) | {"message", "asctime", "request_id"}


def attached(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in record.__dict__.items() if k not in FIELDS_NOT_ATTACHED_BY_THIS_STAGE}


def field(record: logging.LogRecord, name: str) -> Any:
    return record.__dict__[name]


# --- stub collaborators -----------------------------------------------------------------------


@dataclass
class FakeHotel:
    id: int
    public_id: uuid.UUID


class FakeScope:
    def __init__(self, hotel: FakeHotel) -> None:
        self._hotel = hotel
        self.calls = 0

    def require_hotel(self, hotel_public_id: uuid.UUID) -> FakeHotel:
        self.calls += 1
        return self._hotel


class FakeRow:
    """What ``scorable_predictions`` hands back, in the shape the service reads."""

    def __init__(self, row: ObservedPrediction) -> None:
        self.target_date = row.target_date
        self.forecast_horizon_days = 7
        self.model_version = row.model_version
        self.canonical_model_digest = row.canonical_model_digest
        self.feature_digest = row.feature_digest
        self.feature_values = row.feature_values
        self.predicted_room_nights = row.predicted_room_nights


class FakePredictions:
    def __init__(self, rows: list[ObservedPrediction]) -> None:
        self._rows = rows
        self.windows: list[tuple[int, dt.date, dt.date]] = []

    def scorable_predictions(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> list[FakeRow]:
        self.windows.append((hotel_id, date_from, date_to))
        return [FakeRow(row) for row in self._rows if date_from <= row.target_date <= date_to]


def build_service(
    rows: list[ObservedPrediction], *, hotel: FakeHotel | None = None
) -> tuple[DemandDistributionService, FakePredictions, FakeScope]:
    hotel = hotel or FakeHotel(id=4242, public_id=uuid.uuid4())
    predictions = FakePredictions(rows)
    scope = FakeScope(hotel)
    service = DemandDistributionService(
        predictions,  # type: ignore[arg-type]
        scope,  # type: ignore[arg-type]
    )
    return service, predictions, scope


def observe(
    service: DemandDistributionService,
    *,
    baseline: tuple[dt.date, dt.date] | None = None,
) -> DistributionObservation:
    return service.observe(
        uuid.uuid4(), window_from=WINDOW[0], window_to=WINDOW[1], baseline=baseline
    )


def field_summary(result: DistributionObservation, name: str, *, segment: str | None = None) -> Any:
    """One field's summary out of the first model version's chosen segment."""
    entry = result.observed.by_model_version[0]
    chosen = entry.within_calibration if segment is None else getattr(entry, segment)
    return next(summary for summary in chosen.fields if summary.field == name)


def walk_field_names(value: Any) -> set[str]:
    """Every dataclass field name in a result tree, however deeply nested."""
    found: set[str] = set()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for member in dataclasses.fields(value):
            found.add(member.name)
            found |= walk_field_names(getattr(value, member.name))
    elif isinstance(value, tuple | list):
        for item in value:
            found |= walk_field_names(item)
    return found


# ======================================================================================
# The protocol
# ======================================================================================


def test_the_protocol_carries_its_version_and_a_deterministic_checksum() -> None:
    """Criterion 1."""
    assert PROTOCOL.version == "distribution_v1"
    assert re.fullmatch(r"[0-9a-f]{64}", PROTOCOL.checksum)
    assert PROTOCOL.checksum == PROTOCOL.checksum


def test_changing_any_declared_value_changes_the_checksum() -> None:
    """Criterion 2. Otherwise the checksum would be decoration rather than evidence."""
    moved = [
        dataclasses.replace(PROTOCOL, version="distribution_v2"),
        dataclasses.replace(PROTOCOL, quantile_method="nearest rank"),
        dataclasses.replace(PROTOCOL, quantiles=(("p50", 0.5),)),
        dataclasses.replace(PROTOCOL, observed_fields=("predicted_room_nights",)),
        dataclasses.replace(PROTOCOL, small_hotel_max_lag_room_nights=41),
        dataclasses.replace(PROTOCOL, summary_statistics=("count",)),
    ]

    for protocol in moved:
        assert protocol.checksum != PROTOCOL.checksum, protocol.version
    assert len({protocol.checksum for protocol in moved}) == len(moved)


def test_the_protocol_has_no_threshold_verdict_or_alert_field() -> None:
    """Criterion 3. By field list, so the guarantee is structural rather than a promise."""
    names = {member.name for member in dataclasses.fields(PROTOCOL)}

    assert names == {
        "version",
        "observed_fields",
        "summary_statistics",
        "quantiles",
        "quantile_method",
        "empty_semantics",
        "single_observation_semantics",
        "comparison_semantics",
        "segmentation_semantics",
        "segment_below_calibration",
        "segment_within_calibration",
        "segment_features",
        "small_hotel_max_lag_room_nights",
        "model_version_semantics",
        "out_of_scope_semantics",
        "selection_semantics",
    }
    for forbidden in VERDICT_WORDS:
        assert not [name for name in names if forbidden in name], forbidden


def test_the_protocol_declares_every_semantic_the_stage_relies_on() -> None:
    """A convention applied but not declared is an undeclared convention."""
    for declared in (
        PROTOCOL.quantile_method,
        PROTOCOL.empty_semantics,
        PROTOCOL.single_observation_semantics,
        PROTOCOL.comparison_semantics,
        PROTOCOL.segmentation_semantics,
        PROTOCOL.model_version_semantics,
        PROTOCOL.out_of_scope_semantics,
        PROTOCOL.selection_semantics,
    ):
        assert declared and isinstance(declared, str)


def test_the_declared_quantiles_are_exactly_what_is_reported() -> None:
    """Criterion 4."""
    assert PROTOCOL.quantiles == (
        ("p05", 0.05),
        ("p25", 0.25),
        ("p50", 0.50),
        ("p75", 0.75),
        ("p95", 0.95),
    )
    assert PROTOCOL.quantile_labels == ("p05", "p25", "p50", "p75", "p95")

    summary = summarise([1.0, 2.0, 3.0, 4.0])
    assert [label for label, _ in summary["quantiles"]] == list(PROTOCOL.quantile_labels)


def test_the_observed_fields_are_the_nine_features_then_the_output() -> None:
    """Criterion 8 and 9, at the protocol level: the model's own columns, in its own order."""
    assert (*APPROVED_MODEL.feature_columns, "predicted_room_nights") == OBSERVED_FIELDS
    assert len(OBSERVED_FIELDS) == 10
    assert OBSERVED_FIELDS[6:9] == ("demand_lag_7", "demand_lag_14", "demand_lag_28")


def test_no_composite_drift_statistic_is_reachable() -> None:
    """PSI, KS and Jensen-Shannon are named nowhere in what this stage added."""
    for module in ("ml/drift.py", "ml/drift_protocol.py", "services/ml_drift.py"):
        tree = ast.parse((BACKEND / module).read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
        called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for forbidden in (
            "psi",
            "population_stability",
            "kolmogorov",
            "ks_",
            "jensen",
            "divergence",
        ):
            assert not [n for n in defined | called if forbidden in n.lower()], (
                f"{module}: {forbidden}"
            )


# ======================================================================================
# The quantile convention
# ======================================================================================


def test_the_quantile_follows_the_declared_formula() -> None:
    """Hand-computed from ``h = (n-1)p``, not by calling the function twice.

    xs = [1, 2, 3, 4]; n = 4. p25 -> h = 0.75 -> 1 + 0.75*(2-1) = 1.75.
    p50 -> h = 1.5 -> 2 + 0.5*(3-2) = 2.5. p75 -> h = 2.25 -> 3 + 0.25*(4-3) = 3.25.
    """
    xs = [1.0, 2.0, 3.0, 4.0]

    assert quantile(xs, 0.25) == pytest.approx(1.75)
    assert quantile(xs, 0.50) == pytest.approx(2.5)
    assert quantile(xs, 0.75) == pytest.approx(3.25)


@pytest.mark.parametrize(
    "series",
    [
        [1.0, 2.0, 3.0, 4.0],
        [1.0, 2.0, 3.0, 4.0, 5.0],
        [3.5, 1.25, 9.0, 2.0, 7.5, 4.25],
        [10.0, 10.0, 10.0],
        [0.0, 100.0],
    ],
)
def test_the_quantile_agrees_with_the_standard_library(series: list[float]) -> None:
    """Criterion 6 and 7: the declared method IS ``inclusive``, on even and odd lengths alike."""
    ordered = sorted(series)
    mine = [quantile(ordered, p) for p in (0.25, 0.5, 0.75)]
    theirs = statistics.quantiles(ordered, n=4, method="inclusive")

    assert mine == pytest.approx(theirs)


@pytest.mark.parametrize("series", [[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0], [5.0]])
def test_the_median_is_the_p50_quantile(series: list[float]) -> None:
    """One number, one road to it -- for odd and even lengths."""
    summary = summarise(series)
    quantiles = dict(summary["quantiles"])

    assert summary["median"] == pytest.approx(quantiles["p50"])
    assert summary["median"] == pytest.approx(statistics.median(series))


def test_a_quantile_of_an_empty_series_is_refused_rather_than_invented() -> None:
    with pytest.raises(ObservationError):
        quantile([], 0.5)


# ======================================================================================
# Summaries
# ======================================================================================


def test_the_summary_is_the_hand_computed_one() -> None:
    """Criterion 5. Series [2, 4, 4, 6]: min 2, max 6, mean 4, median 4."""
    summary = summarise([4.0, 2.0, 6.0, 4.0])

    assert summary["count"] == 4
    assert summary["minimum"] == pytest.approx(2.0)
    assert summary["maximum"] == pytest.approx(6.0)
    assert summary["mean"] == pytest.approx(4.0)
    assert summary["median"] == pytest.approx(4.0)


def test_an_empty_series_is_none_everywhere_and_never_zero() -> None:
    """Criterion 10. Zero movement and nothing observed are different claims."""
    summary = summarise([])

    assert summary["count"] == 0
    for statistic in ("minimum", "maximum", "mean", "median"):
        assert summary[statistic] is None, statistic
    assert all(value is None for _, value in summary["quantiles"])


def test_a_single_observation_summarises_without_error() -> None:
    """Criterion 11."""
    summary = summarise([7.5])

    assert summary["count"] == 1
    for statistic in ("minimum", "maximum", "mean", "median"):
        assert summary[statistic] == pytest.approx(7.5), statistic
    assert all(value == pytest.approx(7.5) for _, value in summary["quantiles"])


def test_the_mean_does_not_depend_on_the_order_the_rows_arrived_in() -> None:
    """``fsum``, not ``sum``: the reason the arithmetic is reproducible."""
    values = [0.1, 0.2, 0.3, 1e16, -1e16]

    assert summarise(values)["mean"] == pytest.approx(summarise(list(reversed(values)))["mean"])


def test_every_observed_field_is_summarised(events: RecordingHandler) -> None:
    """Criteria 8 and 9 through the service: nine features and the output, in column order."""
    service, _, _ = build_service([prediction(5, 120.0), prediction(6, 130.0)])

    result = observe(service)
    summaries = result.observed.by_model_version[0].within_calibration.fields

    assert tuple(summary.field for summary in summaries) == OBSERVED_FIELDS
    assert field_summary(result, "predicted_room_nights").mean == pytest.approx(125.0)
    assert field_summary(result, "demand_lag_7").mean == pytest.approx(100.0)


def test_the_stored_values_are_summarised_rather_than_recomputed() -> None:
    """A stored feature that disagrees with the calendar is still what the model was given."""
    odd = prediction(5, 120.0, month=4, day_of_week=6.0)
    service, _, _ = build_service([odd])

    result = observe(service)

    assert field_summary(result, "day_of_week").mean == pytest.approx(6.0)


def test_a_field_absent_from_a_stored_row_contributes_nothing_rather_than_zero() -> None:
    partial = ObservedPrediction(
        target_date=dt.date(2026, 4, 5),
        model_version="demand_baseline_v1",
        canonical_model_digest=APPROVED_DIGEST,
        feature_digest="partial",
        feature_values={"demand_lag_7": 100.0, "demand_lag_14": 100.0, "demand_lag_28": 100.0},
        predicted_room_nights=120.0,
    )

    assert value_of(partial, "day_of_week") is None
    assert value_of(partial, "demand_lag_7") == pytest.approx(100.0)
    assert value_of(partial, "predicted_room_nights") == pytest.approx(120.0)


# ======================================================================================
# Comparison
# ======================================================================================


def test_two_windows_produce_summaries_and_differences() -> None:
    """Criterion 12."""
    service, _, _ = build_service(
        [
            prediction(5, 100.0, month=4),
            prediction(5, 80.0, month=3),
        ]
    )

    result = observe(service, baseline=BASELINE)

    assert result.comparison is not None
    assert result.comparison.baseline.window_from == BASELINE[0]
    moved = result.comparison.by_model_version[0].within_calibration
    predicted = next(f for f in moved.fields if f.field == "predicted_room_nights")
    assert predicted.mean == pytest.approx(20.0)
    assert predicted.count == 0


def test_identical_windows_produce_zero_differences() -> None:
    """Criterion 13, for every statistic that was measured on both sides.

    Both segments are populated deliberately. An empty-versus-empty segment is a different
    case with a different right answer, and the test below it states that one.
    """
    rows = [
        prediction(5, 100.0, month=4, level=200.0),
        prediction(7, 140.0, month=4, level=200.0),
        prediction(9, 165.0, month=4, level=10.0),
    ]
    service, _, _ = build_service(rows)

    result = service.observe(
        uuid.uuid4(), window_from=WINDOW[0], window_to=WINDOW[1], baseline=WINDOW
    )

    assert result.comparison is not None
    entry = result.comparison.by_model_version[0]
    for segment in (entry.below_calibration, entry.within_calibration):
        assert segment.observations == 0
        for moved in segment.fields:
            assert moved.count == 0
            for statistic in (moved.minimum, moved.maximum, moved.mean, moved.median):
                assert statistic == pytest.approx(0.0), (segment.segment, moved.field)
            for label, value in moved.quantiles:
                assert value == pytest.approx(0.0), (segment.segment, moved.field, label)


def test_a_segment_empty_in_both_windows_reports_none_rather_than_a_fabricated_zero() -> None:
    """The other half of criterion 13, and the rule that stops it becoming a lie.

    Nothing was measured on either side, so nothing moved by zero -- it did not move at all,
    which is not the same claim. The COUNT difference is still 0, because zero rows minus zero
    rows is a real answer; the statistics are None, because there were none to subtract.
    """
    service, _, _ = build_service([prediction(5, 100.0, month=4, level=200.0)])

    result = service.observe(
        uuid.uuid4(), window_from=WINDOW[0], window_to=WINDOW[1], baseline=WINDOW
    )

    assert result.comparison is not None
    empty = result.comparison.by_model_version[0].below_calibration
    assert empty.observations == 0
    for moved in empty.fields:
        assert moved.count == 0
        assert moved.minimum is None and moved.maximum is None
        assert moved.mean is None and moved.median is None
        assert all(value is None for _, value in moved.quantiles)


def test_a_missing_baseline_leaves_the_comparison_absent() -> None:
    """Criterion 14. Absent, not a fabricated zero."""
    service, _, _ = build_service([prediction(5, 100.0)])

    result = observe(service)

    assert result.comparison is None


def test_a_missing_baseline_reads_only_one_window() -> None:
    service, predictions, _ = build_service([prediction(5, 100.0)])

    observe(service)

    assert len(predictions.windows) == 1


def test_a_difference_against_an_unmeasured_side_is_none_rather_than_zero() -> None:
    assert difference(5.0, None) is None
    assert difference(None, 5.0) is None
    assert difference(None, None) is None
    assert difference(5.0, 2.0) == pytest.approx(3.0)


def test_a_model_version_present_in_only_one_window_still_appears() -> None:
    """Movement includes a version starting or stopping, with its missing side unmeasured."""
    service, _, _ = build_service(
        [
            prediction(5, 100.0, month=4, model_version="demand_baseline_v1"),
            prediction(5, 90.0, month=3, model_version="demand_baseline_v2"),
        ]
    )

    result = observe(service, baseline=BASELINE)

    assert result.comparison is not None
    versions = [entry.model_version for entry in result.comparison.by_model_version]
    assert versions == ["demand_baseline_v1", "demand_baseline_v2"]
    absent = result.comparison.by_model_version[1].within_calibration
    assert all(moved.mean is None for moved in absent.fields)


def test_no_field_in_the_result_reaches_a_verdict() -> None:
    """Criterion 15. Walks the whole tree, not just the top level."""
    service, _, _ = build_service([prediction(5, 100.0), prediction(5, 90.0, month=3)])

    result = observe(service, baseline=BASELINE)
    names = walk_field_names(result)

    assert names, "the walk found nothing -- the assertion below would be vacuous"
    for forbidden in VERDICT_WORDS:
        assert not [name for name in names if forbidden in name], f"{forbidden} in {names}"


# ======================================================================================
# Segmentation
# ======================================================================================


@pytest.mark.parametrize("level", [0.0, 1.0, 39.0, 40.0])
def test_at_or_below_forty_is_below_calibration(level: float) -> None:
    """Criterion 16, reusing Stage 6.9's boundary rather than a second copy of it."""
    grouped = by_segment([prediction(5, 165.0, level=level)])

    assert len(grouped[SEGMENT_BELOW_CALIBRATION]) == 1
    assert grouped[SEGMENT_WITHIN_CALIBRATION] == ()


@pytest.mark.parametrize("level", [41.0, 60.0, 200.0])
def test_above_forty_is_within_calibration(level: float) -> None:
    """Criterion 17."""
    grouped = by_segment([prediction(5, 165.0, level=level)])

    assert grouped[SEGMENT_BELOW_CALIBRATION] == ()
    assert len(grouped[SEGMENT_WITHIN_CALIBRATION]) == 1


def test_both_segments_are_always_present_even_when_empty() -> None:
    """An absent denominator and a zero one must not look alike."""
    grouped = by_segment([])

    assert set(grouped) == {SEGMENT_BELOW_CALIBRATION, SEGMENT_WITHIN_CALIBRATION}


def test_the_two_segments_are_summarised_separately_with_their_own_counts() -> None:
    service, _, _ = build_service(
        [
            prediction(5, 165.0, level=10.0, feature_digest="small"),
            prediction(6, 150.0, level=200.0, feature_digest="large"),
        ]
    )

    entry = observe(service).observed.by_model_version[0]

    assert entry.below_calibration.observations == 1
    assert entry.within_calibration.observations == 1
    assert entry.below_calibration.feature_digests == ("small",)
    assert entry.within_calibration.feature_digests == ("large",)
    assert field_summary(observe(service), "predicted_room_nights", segment="below_calibration")


def test_no_combined_segment_metric_exists() -> None:
    """Criterion 18. There is no shape in which the two populations could be pooled."""
    service, _, _ = build_service([prediction(5, 100.0)])
    entry = observe(service).observed.by_model_version[0]

    names = {member.name for member in dataclasses.fields(entry)}
    assert names == {
        "model_version",
        "canonical_model_digest",
        "below_calibration",
        "within_calibration",
    }
    for forbidden in ("overall", "combined", "total", "pooled", "headline", "all_segments"):
        assert not [name for name in names if forbidden in name], forbidden


def test_segment_membership_reads_stored_inputs_only() -> None:
    """Criterion 19. The prediction VALUE is large; the stored lags are small."""
    service, _, _ = build_service([prediction(5, 9999.0, level=10.0)])

    entry = observe(service).observed.by_model_version[0]

    assert entry.below_calibration.observations == 1
    assert entry.within_calibration.observations == 0


def test_the_highest_lag_decides_the_segment() -> None:
    grouped = by_segment([prediction(5, 100.0, level=10.0, demand_lag_28=200.0)])

    assert len(grouped[SEGMENT_WITHIN_CALIBRATION]) == 1


# ======================================================================================
# Model version and digest
# ======================================================================================


def test_two_model_versions_never_share_a_summary() -> None:
    """Criterion 20."""
    service, _, _ = build_service(
        [
            prediction(5, 100.0, model_version="demand_baseline_v1"),
            prediction(5, 300.0, model_version="demand_baseline_v2"),
        ]
    )

    result = observe(service)

    assert [entry.model_version for entry in result.observed.by_model_version] == [
        "demand_baseline_v1",
        "demand_baseline_v2",
    ]
    for entry, expected in zip(result.observed.by_model_version, (100.0, 300.0), strict=True):
        summary = next(
            f for f in entry.within_calibration.fields if f.field == "predicted_room_nights"
        )
        assert summary.mean == pytest.approx(expected)


def test_a_foreign_digest_is_reported_and_summarised_into_nothing() -> None:
    """Criterion 21."""
    service, _, _ = build_service([prediction(5, 100.0), prediction(6, 100.0, digest="0" * 64)])

    result = observe(service)

    assert result.observed.candidates == 2
    assert result.observed.out_of_scope_model_digest == 1
    assert len(result.observed.by_model_version) == 1
    assert result.observed.by_model_version[0].within_calibration.observations == 1


def test_the_model_digest_is_preserved_on_the_group() -> None:
    service, _, _ = build_service([prediction(5, 100.0)])

    assert observe(service).observed.by_model_version[0].canonical_model_digest == APPROVED_DIGEST


def test_two_predictions_for_one_group_are_refused_rather_than_summarised() -> None:
    """The inherited selection rule is checked, not assumed."""
    with pytest.raises(ObservationError):
        assert_one_per_group([prediction(5, 100.0), prediction(5, 200.0)])


def test_the_partition_counts_what_it_sets_aside() -> None:
    split = partition(
        [prediction(5, 100.0), prediction(6, 100.0, digest="0" * 64)],
        approved_digest=APPROVED_DIGEST,
    )

    assert len(split.in_scope) == 1
    assert split.out_of_scope_model_digest == 1


# ======================================================================================
# Determinism, isolation and no writes
# ======================================================================================


def test_two_identical_observations_are_bit_identical() -> None:
    """Criterion 22."""
    rows = [prediction(5, 100.0), prediction(7, 160.0), prediction(9, 80.0, month=3)]
    hotel = FakeHotel(id=4242, public_id=uuid.uuid4())
    first, _, _ = build_service(rows, hotel=hotel)
    second, _, _ = build_service(rows, hotel=hotel)

    assert (
        observe(first, baseline=BASELINE).as_dict() == observe(second, baseline=BASELINE).as_dict()
    )


@pytest.mark.parametrize("module", ["ml/drift.py", "ml/drift_protocol.py", "services/ml_drift.py"])
def test_no_ambient_clock_on_the_observation_path(module: str) -> None:
    """Criterion 23, from the source. The behavioural half is below."""
    tree = ast.parse((BACKEND / module).read_text(encoding="utf-8"))
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    for forbidden in (
        "dt.date.today",
        "dt.datetime.now",
        "dt.datetime.utcnow",
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "func.now",
    ):
        assert forbidden not in calls, f"{module}: {forbidden}"


def test_the_windows_decide_what_is_read_rather_than_the_wall_clock() -> None:
    """Criterion 23, behavioural: the same rows, two windows, two different answers."""
    rows = [prediction(5, 100.0, month=4), prediction(5, 50.0, month=3)]
    april, _, _ = build_service(rows)
    march, _, _ = build_service(rows)

    april_mean = field_summary(observe(april), "predicted_room_nights").mean
    march_result = march.observe(
        uuid.uuid4(), window_from=BASELINE[0], window_to=BASELINE[1], baseline=None
    )
    march_mean = next(
        f
        for f in march_result.observed.by_model_version[0].within_calibration.fields
        if f.field == "predicted_room_nights"
    ).mean

    assert april_mean == pytest.approx(100.0)
    assert march_mean == pytest.approx(50.0)


def test_the_service_holds_no_session_and_cannot_write() -> None:
    """Criterion 24. Structural: there is nothing to write to."""
    tree = ast.parse((BACKEND / "services" / "ml_drift.py").read_text(encoding="utf-8"))
    init = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    parameters = {argument.arg for argument in [*init.args.args, *init.args.kwonlyargs]}

    assert "session" not in parameters
    assert "db" not in parameters

    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden in ("commit", "rollback", "flush", "add", "insert", "delete", "update"):
        assert not [call for call in calls if call.endswith(f".{forbidden}")], forbidden


def test_the_service_builds_no_sqlalchemy_query() -> None:
    tree = ast.parse((BACKEND / "services" / "ml_drift.py").read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not [module for module in imported if module.startswith("sqlalchemy")]
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden in ("select", "and_", "or_", "func.count"):
        assert forbidden not in calls, forbidden


@pytest.mark.parametrize("forbidden", ["ml.", "fastapi", "starlette", "sklearn"])
def test_the_service_imports_neither_the_offline_package_nor_a_framework(forbidden: str) -> None:
    source = (BACKEND / "services" / "ml_drift.py").read_text(encoding="utf-8")
    pattern = re.compile(rf"^\s*(?:import|from)\s+{re.escape(forbidden)}", re.MULTILINE)

    assert not pattern.search(source)


def test_the_observation_path_never_loads_the_artifact() -> None:
    """It reads numbers the model already produced; it does not run one."""
    for module in ("ml/drift.py", "ml/drift_protocol.py", "services/ml_drift.py"):
        tree = ast.parse((BACKEND / module).read_text(encoding="utf-8"))
        calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for forbidden in ("approved_model", "predict_room_nights", "load_artifact"):
            assert forbidden not in calls, f"{module}: {forbidden}"


def test_the_hotel_is_resolved_before_anything_is_read() -> None:
    """A non-member must not learn whether this hotel has predictions."""
    source = (BACKEND / "services" / "ml_drift.py").read_text(encoding="utf-8")
    body = source[source.index("def observe(") :]

    assert body.index("require_hotel") < body.index("scorable_predictions")

    service, predictions, scope = build_service([prediction(5, 100.0)])
    observe(service)
    assert scope.calls == 1
    assert [window[0] for window in predictions.windows] == [4242]


def test_a_refused_hotel_reads_nothing() -> None:
    class Refusing:
        def require_hotel(self, hotel_public_id: uuid.UUID) -> object:
            raise LookupError("Hotel not found.")

    predictions = FakePredictions([])
    service = DemandDistributionService(
        predictions,  # type: ignore[arg-type]
        Refusing(),  # type: ignore[arg-type]
    )

    with pytest.raises(LookupError):
        observe(service)

    assert predictions.windows == []


def test_no_method_accepts_more_than_one_hotel() -> None:
    tree = ast.parse((BACKEND / "services" / "ml_drift.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "observe"
    )
    arguments = [
        argument.arg
        for argument in [*function.args.args, *function.args.kwonlyargs]
        if argument.arg != "self"
    ]

    assert not [name for name in arguments if name.endswith("s") and "hotel" in name]


def test_the_result_carries_no_internal_identifier() -> None:
    service, _, _ = build_service([prediction(5, 100.0)])

    result = observe(service)

    assert "hotel_id" not in walk_field_names(result)
    assert "4242" not in str(result.as_dict())


# ======================================================================================
# The event
# ======================================================================================


def test_one_observation_emits_exactly_one_event(events: RecordingHandler) -> None:
    """Criterion 26."""
    service, _, _ = build_service([prediction(5, 100.0)])

    observe(service)

    assert len([record for record in events.records if hasattr(record, "outcome")]) == 1


def test_the_event_carries_exactly_the_five_approved_fields(events: RecordingHandler) -> None:
    """Criterion 25. As a set, so a sixth field cannot arrive unnoticed."""
    service, _, _ = build_service([prediction(5, 100.0), prediction(5, 90.0, month=3)])

    observe(service, baseline=BASELINE)

    event = next(record for record in events.records if hasattr(record, "outcome"))
    assert set(attached(event)) == {
        "outcome",
        "model_version",
        "predictions_summarised",
        "windows_compared",
        "duration_ms",
    }
    assert field(event, "outcome") == "observed"
    assert field(event, "model_version") == "demand_baseline_v1"
    assert field(event, "predictions_summarised") == 2
    assert field(event, "windows_compared") == 2
    assert isinstance(field(event, "duration_ms"), float)


def test_a_single_window_reports_one_window_compared(events: RecordingHandler) -> None:
    service, _, _ = build_service([prediction(5, 100.0)])

    observe(service)

    event = next(record for record in events.records if hasattr(record, "outcome"))
    assert field(event, "windows_compared") == 1
    assert field(event, "predictions_summarised") == 1


def test_an_empty_observation_says_it_summarised_nothing(events: RecordingHandler) -> None:
    service, _, _ = build_service([])

    observe(service)

    event = next(record for record in events.records if hasattr(record, "outcome"))
    assert field(event, "outcome") == "nothing_to_observe"
    assert field(event, "predictions_summarised") == 0


def test_the_event_names_nothing_a_hotel_owns(events: RecordingHandler) -> None:
    """Criterion 27. Captured records, not a source scan.

    ``duration_ms`` is excluded and the forbidden values are deliberately distinctive: it is a
    machine timing whose digits are arbitrary, so a short value collides with it sooner or later
    and fails for a reason that is not a leak. It is pinned by name in the five-field test above.
    """
    service, _, scope = build_service(
        [prediction(5, 8317.25, level=6143.0, feature_digest="e" * 64)]
    )

    observe(service)

    assert events.records, "nothing was captured -- the assertion below would be vacuous"
    rendered = "\n".join(
        f"{record.getMessage()} "
        f"{ {k: v for k, v in attached(record).items() if k != 'duration_ms'}!r}"
        for record in events.records
    )
    for forbidden in (
        "8317.25",
        "6143",
        "e" * 64,
        APPROVED_DIGEST,
        str(scope._hotel.public_id),
        "demand_lag_7",
        "median",
        "quantile",
        "SELECT",
        "password",
        "secret",
        "/app/",
    ):
        assert forbidden not in rendered, forbidden


# ======================================================================================
# Claims and contract
# ======================================================================================


def test_the_result_carries_the_protocol_that_produced_it() -> None:
    """Criterion 28."""
    service, _, _ = build_service([prediction(5, 100.0)])

    result = observe(service)

    assert result.protocol_version == PROTOCOL.version
    assert result.protocol_checksum == PROTOCOL.checksum
    assert result.establishes_production_accuracy is False
    assert result.observed.window_from == WINDOW[0]
    assert result.observed.window_to == WINDOW[1]


def test_the_api_surface_is_the_one_stage_73_published() -> None:
    """Stage 6.10 added no endpoint; Stage 7.3 published this service over HTTP deliberately.

    The surface moved 52 / 84 -> 54 / 86 with that stage. Asserting the old figures here would
    assert that a later stage did not happen; what this test was protecting is kept below.
    """
    schema = create_app(Settings(environment="test", debug=True)).openapi()
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    operations = sum(1 for path in schema["paths"].values() for verb in path if verb in methods)

    # Stage 7.7 added POST .../copilot/ask: 54 / 86 -> 55 / 87.
    # Stage 7.9 added the knowledge documents and their search: 55 / 87 -> 60 / 93.
    assert len(schema["paths"]) == 60
    assert operations == 93


def test_the_frozen_result_type_is_still_not_an_http_contract() -> None:
    """Stage 7.3 publishes a separate projection, so a field added here is not published."""
    schema = create_app(Settings(environment="test", debug=True)).openapi()
    published = set(schema["components"]["schemas"])

    assert "DistributionObservation" not in published
    assert "DistributionComparison" not in published
    assert "WindowSummary" not in published
    assert "SegmentSummary" not in published
    assert "FieldSummary" not in published


def test_no_published_schema_carries_the_per_prediction_digests() -> None:
    """``feature_digests`` attributes a summary to its rows internally. It does not leave."""
    schema = create_app(Settings(environment="test", debug=True)).openapi()

    carrying = [
        name
        for name, definition in schema["components"]["schemas"].items()
        if "feature_digests" in definition.get("properties", {})
    ]

    assert carrying == []


def test_nothing_published_from_this_observation_reaches_a_verdict() -> None:
    """The observation decides nothing, and neither may anything published from it.

    A field named for drift, a threshold or a verdict would be this stage's non-goal arriving
    through the API instead of through the protocol, which is the same non-goal.

    Scoped to the models Stage 7.3 publishes rather than to the whole document, deliberately:
    V1's ``AnomalyPoint`` carries a ``threshold`` and is correct to -- statistical anomaly
    detection over the analytics series is a different feature that genuinely does decide
    something, and it decides it about a series rather than about the model.
    """
    import app.schemas.ml_performance as published_models

    schema = create_app(Settings(environment="test", debug=True)).openapi()
    ours = set(published_models.__all__) & set(schema["components"]["schemas"])
    forbidden = {"drift", "drifted", "drift_detected", "threshold", "verdict", "alert", "anomaly"}

    # Guards the loop: an empty intersection would pass this test without checking anything.
    assert len(ours) >= 10

    for name in ours:
        offending = forbidden & set(schema["components"]["schemas"][name].get("properties", {}))
        assert not offending, f"{name} publishes {sorted(offending)}"


def test_the_migration_chain_did_not_move() -> None:
    """Stage 6.10 adds no migration."""
    versions = REPOSITORY_ROOT / "database" / "migrations" / "versions"
    revisions = sorted(path.name for path in versions.glob("*.py"))

    # Stage 7.9 added 0014 (`hotel_documents`, `hotel_document_chunks`).
    assert len(revisions) == 14
    assert revisions[-1].endswith("0014_hotel_documents.py")


def test_the_model_identity_is_untouched() -> None:
    assert APPROVED_MODEL.model_version == "demand_baseline_v1"
    assert (
        APPROVED_MODEL.canonical_model_digest
        == "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"
    )


def test_no_repository_read_was_added_for_this_stage() -> None:
    """Decision 3: the Stage 6.9 read provides every locked field, so Stage 6.10 added nothing.

    The set grew by one in Stage 6.11, which added ``stored_predictions_page`` for the read API.
    That is a different stage's method and does not weaken this claim: what this test says is
    that *distribution observation* introduced no read of its own, and it still did not.
    """
    tree = ast.parse((BACKEND / "repositories" / "ml_prediction.py").read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }

    assert methods == {
        "record",
        "scorable_predictions",
        "unsettled_allocation_count",
        "stored_predictions_page",
    }


def test_the_backend_requirements_did_not_move() -> None:
    declared = [
        line.strip().lower()
        for line in (REPOSITORY_ROOT / "backend" / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert [line for line in declared if line.startswith("scikit-learn")] == ["scikit-learn==1.9.1"]
    for library in ("pandas", "torch", "tensorflow", "xgboost", "lightgbm", "numpy", "scipy"):
        assert not [line for line in declared if line.startswith(library)], library


def test_production_accuracy_remains_unestablished_and_nothing_is_detected() -> None:
    """The sentence this stage exists not to change, plus the one it adds."""
    docs = REPOSITORY_ROOT / "docs"
    card = (docs / "ml-model-card.md").read_text(encoding="utf-8")
    serving = (docs / "ml-serving.md").read_text(encoding="utf-8")
    runtime = (docs / "ml-production-runtime.md").read_text(encoding="utf-8")
    observation = (docs / "ml-drift-observation.md").read_text(encoding="utf-8")

    assert "| Production accuracy established | **No** |" in card
    assert "No production accuracy is established" in serving
    assert "Production accuracy is not established" in runtime
    assert "This stage detects nothing and establishes no production accuracy." in observation


def test_the_stale_persistence_claim_was_corrected() -> None:
    """Decision 5: the runtime document said predictions are not persisted. They are."""
    runtime = (REPOSITORY_ROOT / "docs" / "ml-production-runtime.md").read_text(encoding="utf-8")

    assert "**Predictions are not persisted**" not in runtime
    assert "0010_demand_predictions" in runtime or "Stage 6.8" in runtime


def test_no_test_in_this_repository_asserts_a_drift_threshold() -> None:
    """Nothing here is a threshold, a verdict or a detection claim."""
    pattern = re.compile(r"assert\s+[^\n]*\bdrift(ed|_detected)?\b[^\n]*[<>]=?\s*[0-9]", re.I)
    for source in (REPOSITORY_ROOT / "tests").rglob("*.py"):
        assert not pattern.search(source.read_text(encoding="utf-8")), source.name
