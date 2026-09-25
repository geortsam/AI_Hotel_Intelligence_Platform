"""Stage 6.9: the protocol, the pairing, the segments, and the evaluation event.

Everything here runs without a database. What needs one -- the selection rule executed by
``DISTINCT ON``, the settlement boundary over real rows, cross-tenant isolation, and the proof
that an evaluation writes nothing -- is in
``tests/integration/test_forecast_accuracy_api.py`` against real PostgreSQL.

The service is exercised with stub collaborators, which is how the *rules* get tested cheaply
and exhaustively: one case per skip reason, one per segment boundary, with the log records
captured rather than the source read.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.main import create_app
from app.ml.accuracy import (
    AccuracyError,
    ScoredCandidate,
    assert_one_per_group,
    metrics_for,
    partition,
    score_segments,
    scored_window,
)
from app.ml.accuracy_protocol import (
    PROTOCOL,
    SEGMENT_BELOW_CALIBRATION,
    SEGMENT_WITHIN_CALIBRATION,
    SETTLEMENT_LAG_DAYS,
    VOLATILE_OCCUPANCY_STATUSES,
    is_eligible,
    last_settled_target_date,
    segment_of,
)
from app.ml.serving import APPROVED_MODEL
from app.models.enums import BookingStatus
from app.schemas.ml_accuracy import AccuracyEvaluation
from app.services.ml_accuracy import DemandAccuracyService

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPOSITORY_ROOT / "backend" / "app"

APPROVED_DIGEST = APPROVED_MODEL.canonical_model_digest
AS_OF = dt.date(2026, 6, 1)
#: The newest target date that has cleared the lag at :data:`AS_OF`.
SETTLED = AS_OF - dt.timedelta(days=SETTLEMENT_LAG_DAYS)


def features(level: float) -> dict[str, float]:
    """A feature vector whose three lag values are all *level*."""
    return {
        "day_of_week": 1.0,
        "day_of_month": 2.0,
        "month": 6.0,
        "week_of_year": 23.0,
        "day_of_year": 124.0,
        "is_weekend": 0.0,
        "demand_lag_7": level,
        "demand_lag_14": level,
        "demand_lag_28": level,
    }


def candidate(
    target_date: dt.date,
    predicted: float,
    *,
    level: float = 100.0,
    model_version: str = "demand_baseline_v1",
    digest: str = APPROVED_DIGEST,
    feature_digest: str | None = None,
) -> ScoredCandidate:
    return ScoredCandidate(
        target_date=target_date,
        forecast_horizon_days=7,
        model_version=model_version,
        canonical_model_digest=digest,
        feature_digest=feature_digest or f"digest-{target_date.isoformat()}-{model_version}",
        feature_values=features(level),
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
    """Everything ``app.services.ml_accuracy`` logs, captured at the source.

    Not caplog: it listens on root, and ``configure_logging`` replaces root's handlers the
    first time any test starts an application, so a caplog assertion here would pass alone and
    silently capture nothing in a full run.
    """
    handler = RecordingHandler()
    logger = logging.getLogger("app.services.ml_accuracy")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


#: Field names on a record that this stage did not put there: the standard library's own, read
#: from a real record, plus the three the shared console handler adds once logging is configured.
FIELDS_NOT_ATTACHED_BY_THIS_STAGE = frozenset(
    logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None).__dict__
) | {"message", "asctime", "request_id"}


def attached(record: logging.LogRecord) -> dict[str, object]:
    """Only what this stage put on the record through ``extra``."""
    return {k: v for k, v in record.__dict__.items() if k not in FIELDS_NOT_ATTACHED_BY_THIS_STAGE}


def field(record: logging.LogRecord, name: str) -> Any:
    """One structured field, read from ``__dict__`` because ``extra`` adds it at emit time."""
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


class FakePredictions:
    def __init__(self, rows: list[ScoredCandidate], unsettled: int = 0) -> None:
        self._rows = rows
        self._unsettled = unsettled
        self.windows: list[tuple[int, dt.date, dt.date]] = []
        self.unsettled_windows: list[tuple[int, dt.date, dt.date]] = []

    def scorable_predictions(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> list[ScoredCandidate]:
        self.windows.append((hotel_id, date_from, date_to))
        return [row for row in self._rows if date_from <= row.target_date <= date_to]

    def unsettled_allocation_count(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> int:
        self.unsettled_windows.append((hotel_id, date_from, date_to))
        return self._unsettled


class FakeDemand:
    def __init__(self, realised: dict[dt.date, int]) -> None:
        self._realised = realised
        self.calls = 0

    def demand_by_date(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> dict[dt.date, int]:
        self.calls += 1
        return {day: value for day, value in self._realised.items() if date_from <= day <= date_to}


def build_service(
    rows: list[ScoredCandidate],
    realised: dict[dt.date, int],
    *,
    unsettled: int = 0,
    hotel: FakeHotel | None = None,
) -> tuple[DemandAccuracyService, FakePredictions, FakeDemand, FakeScope]:
    hotel = hotel or FakeHotel(id=4242, public_id=uuid.uuid4())
    predictions = FakePredictions(rows, unsettled)
    demand = FakeDemand(realised)
    scope = FakeScope(hotel)
    service = DemandAccuracyService(
        predictions,  # type: ignore[arg-type]
        demand,  # type: ignore[arg-type]
        scope,  # type: ignore[arg-type]
    )
    return service, predictions, demand, scope


def evaluate(
    service: DemandAccuracyService,
    *,
    window_from: dt.date,
    window_to: dt.date,
    as_of_date: dt.date = AS_OF,
) -> AccuracyEvaluation:
    return service.evaluate(
        uuid.uuid4(), as_of_date=as_of_date, window_from=window_from, window_to=window_to
    )


# ======================================================================================
# The protocol
# ======================================================================================


def test_the_protocol_carries_a_version_and_a_deterministic_checksum() -> None:
    """Criterion 1. The checksum is over the protocol's own values, so it cannot drift."""
    assert PROTOCOL.version == "accuracy_v1"
    assert re.fullmatch(r"[0-9a-f]{64}", PROTOCOL.checksum)
    assert PROTOCOL.checksum == PROTOCOL.checksum
    assert dataclasses.replace(PROTOCOL, version="other").checksum != PROTOCOL.checksum


def test_changing_any_protocol_value_changes_the_checksum() -> None:
    """Otherwise the checksum would be decoration rather than evidence."""
    moved = [
        dataclasses.replace(PROTOCOL, settlement_lag_days=29),
        dataclasses.replace(PROTOCOL, small_hotel_max_lag_room_nights=41),
        dataclasses.replace(PROTOCOL, selection_rule="latest generated_at"),
        dataclasses.replace(PROTOCOL, segment_features=("demand_lag_7",)),
    ]

    for protocol in moved:
        assert protocol.checksum != PROTOCOL.checksum, protocol.as_dict()
    assert len({protocol.checksum for protocol in moved}) == len(moved)


def test_the_protocol_holds_the_locked_values() -> None:
    assert PROTOCOL.settlement_lag_days == 28
    assert PROTOCOL.small_hotel_max_lag_room_nights == 40
    assert PROTOCOL.selection_rule == "earliest generated_at, tie-break lowest id"
    assert PROTOCOL.segment_below_calibration == SEGMENT_BELOW_CALIBRATION
    assert PROTOCOL.segment_within_calibration == SEGMENT_WITHIN_CALIBRATION
    assert PROTOCOL.skip_semantics, "the skip semantics are declared, not implied"


def test_the_protocol_has_no_threshold_baseline_or_ranking_field() -> None:
    """Criterion 2. By field list, so the guarantee is structural rather than a promise.

    The evaluation cannot reach a verdict because there is nothing in its rules to reach one
    against -- the same structural argument Stage 6.4's acceptance policy makes about metrics.
    """
    names = {field.name for field in dataclasses.fields(PROTOCOL)}

    assert names == {
        "version",
        "settlement_lag_days",
        "selection_rule",
        "small_hotel_max_lag_room_nights",
        "segment_below_calibration",
        "segment_within_calibration",
        "segment_features",
        "volatile_occupancy_statuses",
        "skip_semantics",
    }
    for forbidden in ("threshold", "pass", "minimum", "maximum", "baseline", "rank", "target"):
        assert not [name for name in names if forbidden in name], forbidden


def test_the_comparing_metric_helpers_are_never_reached(tmp_path: Path) -> None:
    """Criterion 3. ``difference`` is a baseline comparison; ``pooled`` combines metric sets.

    Neither is something this stage performs, and the accuracy module imports exactly one name
    from ``ml.metrics``. Checked on the import itself rather than by scanning for a word that
    appears legitimately in prose.
    """
    tree = ast.parse((BACKEND / "ml" / "accuracy.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "ml.metrics"
        for alias in node.names
    }

    assert imported == {"metric_set"}

    # Attribute ACCESSES, not the text. The accuracy module's docstring names both helpers in
    # order to say it does not use them, and a substring search would read that explanation as
    # the thing it rules out.
    for source in BACKEND.rglob("*.py"):
        reached = {
            ast.unparse(node)
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
            if isinstance(node, ast.Attribute)
        }
        for forbidden in ("ml.metrics.difference", "ml.metrics.pooled"):
            assert forbidden not in reached, f"{source.name}: {forbidden}"


def test_the_volatile_statuses_are_derived_rather_than_restated() -> None:
    """Criterion 15's other half: no second definition of occupancy semantics.

    The set is computed from the transition graph, so a migration that changed the graph would
    change this with it. Its current value is asserted here as a fact about that graph, not as
    a definition.
    """
    assert set(VOLATILE_OCCUPANCY_STATUSES) == {
        BookingStatus.CONFIRMED.value,
        BookingStatus.PENDING.value,
    }
    assert tuple(sorted(VOLATILE_OCCUPANCY_STATUSES)) == VOLATILE_OCCUPANCY_STATUSES
    source = (BACKEND / "ml" / "accuracy_protocol.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "checked_in" not in literals
    assert "no_show" not in literals


# ======================================================================================
# Settlement
# ======================================================================================


def test_the_settlement_boundary_is_inclusive() -> None:
    """Criterion 4. ``target_date + 28 == as_of_date`` is eligible."""
    assert last_settled_target_date(AS_OF) == AS_OF - dt.timedelta(days=28)
    assert is_eligible(SETTLED, AS_OF)


def test_one_day_short_of_the_boundary_is_ineligible() -> None:
    """Criterion 5."""
    assert not is_eligible(SETTLED + dt.timedelta(days=1), AS_OF)


def test_a_prediction_one_day_short_is_counted_not_scored() -> None:
    rows = [candidate(SETTLED, 100.0), candidate(SETTLED + dt.timedelta(days=1), 100.0)]
    split = partition(rows, as_of_date=AS_OF, approved_digest=APPROVED_DIGEST)

    assert len(split.eligible) == 1
    assert split.eligible[0].target_date == SETTLED
    assert split.ineligible_by_settlement == 1


def test_as_of_date_is_required() -> None:
    """Criterion 6. Keyword-only and with no default, so it cannot be forgotten."""
    service, _, _, _ = build_service([], {})

    with pytest.raises(TypeError):
        service.evaluate(uuid.uuid4())  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "module", ["ml/accuracy.py", "ml/accuracy_protocol.py", "services/ml_accuracy.py"]
)
def test_no_ambient_clock_on_the_computation_path(module: str) -> None:
    """Criterion 7, read from the source. The behavioural half is the test below."""
    tree = ast.parse((BACKEND / module).read_text(encoding="utf-8"))
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    for forbidden in (
        "dt.date.today",
        "dt.datetime.now",
        "dt.datetime.utcnow",
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "time.time",
        "func.now",
    ):
        assert forbidden not in calls, f"{module}: {forbidden}"


def test_the_as_of_date_decides_the_eligible_set_rather_than_the_wall_clock() -> None:
    """Criterion 7, behavioural: the same data, two as-of dates, two different answers."""
    target = dt.date(2026, 4, 1)
    rows = [candidate(target, 100.0)]
    realised = {target: 100}

    early, _, _, _ = build_service(rows, realised)
    late, _, _, _ = build_service(rows, realised)

    before = evaluate(
        early,
        window_from=target,
        window_to=target,
        as_of_date=target + dt.timedelta(days=27),
    )
    after = evaluate(
        late, window_from=target, window_to=target, as_of_date=target + dt.timedelta(days=28)
    )

    assert before.ineligible_by_settlement == 1
    assert before.by_model_version == ()
    assert after.ineligible_by_settlement == 0
    assert after.by_model_version[0].within_calibration.observations == 1


def test_unsettled_allocations_make_the_result_provisional() -> None:
    """Criterion 8. The lag is an assumption; this is the mechanism that reports its failure."""
    rows = [candidate(SETTLED, 100.0)]
    service, _, _, _ = build_service(rows, {SETTLED: 100}, unsettled=3)

    result = evaluate(service, window_from=SETTLED, window_to=SETTLED)

    assert result.unsettled_allocations == 3
    assert result.settled is False


def test_a_fully_settled_window_says_so() -> None:
    service, _, _, _ = build_service([candidate(SETTLED, 100.0)], {SETTLED: 100}, unsettled=0)

    result = evaluate(service, window_from=SETTLED, window_to=SETTLED)

    assert result.unsettled_allocations == 0
    assert result.settled is True


def test_the_unsettled_count_is_asked_for_the_scored_window_only() -> None:
    """Not the requested window: a date nobody scored cannot make a measurement provisional."""
    service, predictions, _, _ = build_service([], {})

    evaluate(service, window_from=SETTLED, window_to=SETTLED + dt.timedelta(days=10))

    assert predictions.unsettled_windows == [(4242, SETTLED, SETTLED)]


def test_a_window_with_nothing_settled_yet_scores_nothing_and_asks_nothing() -> None:
    """A young deployment is a normal case, not an error -- and it costs no query."""
    service, predictions, demand, _ = build_service([], {})
    future = AS_OF - dt.timedelta(days=1)

    result = evaluate(service, window_from=future, window_to=future)

    assert scored_window(future, future, AS_OF) is None
    assert result.scored_from is None and result.scored_to is None
    assert predictions.unsettled_windows == []
    assert demand.calls == 0


# ======================================================================================
# Selection
# ======================================================================================


def test_two_predictions_for_one_group_are_refused_rather_than_pooled() -> None:
    """Criterion 9's unit half. The database applies the rule; this refuses to score a set
    where it evidently was not applied, because pooling two into one denominator would be
    invisible in the result."""
    duplicated = [candidate(SETTLED, 100.0), candidate(SETTLED, 120.0)]

    with pytest.raises(AccuracyError):
        assert_one_per_group(duplicated)


def test_two_predictions_for_different_model_versions_are_not_a_duplicate() -> None:
    rows = [
        candidate(SETTLED, 100.0, model_version="demand_baseline_v1"),
        candidate(SETTLED, 110.0, model_version="demand_baseline_v2"),
    ]

    assert_one_per_group(rows)


def test_the_selected_digests_are_carried_into_the_result() -> None:
    """Criteria 12 and 13. Every scored number traces back to one row."""
    rows = [candidate(SETTLED, 90.0, feature_digest="the-selected-digest")]
    service, _, _, _ = build_service(rows, {SETTLED: 100})

    result = evaluate(service, window_from=SETTLED, window_to=SETTLED)
    entry = result.by_model_version[0]

    assert entry.within_calibration.scored_feature_digests == ("the-selected-digest",)
    assert entry.canonical_model_digest == APPROVED_DIGEST


# ======================================================================================
# Segmentation
# ======================================================================================


@pytest.mark.parametrize("level", [0.0, 1.0, 10.0, 39.0, 40.0])
def test_at_or_below_forty_is_below_calibration(level: float) -> None:
    """Criterion 14. Forty is the largest level Stage 6.6 measured to be indistinguishable."""
    assert segment_of(features(level)) == SEGMENT_BELOW_CALIBRATION


@pytest.mark.parametrize("level", [41.0, 60.0, 165.0])
def test_above_forty_is_within_calibration(level: float) -> None:
    """Criterion 15."""
    assert segment_of(features(level)) == SEGMENT_WITHIN_CALIBRATION


def test_the_highest_lag_decides_the_segment() -> None:
    """A hotel that was small last month and is not now is not a small hotel."""
    mixed = features(10.0)
    mixed["demand_lag_28"] = 200.0

    assert segment_of(mixed) == SEGMENT_WITHIN_CALIBRATION


def test_both_segments_are_always_reported_separately() -> None:
    """Criteria 16 and 17. Two fields, never a mapping and never a sum."""
    rows = [
        candidate(SETTLED, 165.0, level=10.0, feature_digest="small"),
        candidate(SETTLED - dt.timedelta(days=1), 150.0, level=200.0, feature_digest="large"),
    ]
    service, _, _, _ = build_service(rows, {SETTLED: 10, SETTLED - dt.timedelta(days=1): 160})

    entry = evaluate(
        service, window_from=SETTLED - dt.timedelta(days=1), window_to=SETTLED
    ).by_model_version[0]

    assert entry.below_calibration.observations == 1
    assert entry.within_calibration.observations == 1
    assert entry.below_calibration.scored_feature_digests == ("small",)
    assert entry.within_calibration.scored_feature_digests == ("large",)


def test_no_field_anywhere_combines_the_two_segments() -> None:
    """Criterion 17. There is no shape in which a combined headline could be read."""
    rows = [candidate(SETTLED, 100.0)]
    service, _, _, _ = build_service(rows, {SETTLED: 100})
    result = evaluate(service, window_from=SETTLED, window_to=SETTLED)

    entry_fields = {field.name for field in dataclasses.fields(result.by_model_version[0])}
    assert entry_fields == {
        "model_version",
        "canonical_model_digest",
        "below_calibration",
        "within_calibration",
    }
    for forbidden in ("overall", "combined", "total", "pooled", "headline", "all_segments"):
        assert not [name for name in entry_fields if forbidden in name], forbidden


def test_the_below_calibration_segment_is_never_dropped() -> None:
    """Excluding it would hide the model's worst documented failure."""
    rows = [candidate(SETTLED, 165.0, level=5.0)]
    service, _, _, _ = build_service(rows, {SETTLED: 5})

    entry = evaluate(service, window_from=SETTLED, window_to=SETTLED).by_model_version[0]

    assert entry.below_calibration.observations == 1
    assert entry.below_calibration.metrics.mae == pytest.approx(160.0)


def test_segment_membership_ignores_the_realised_demand() -> None:
    """Criterion 18. The segment is fixed by the inputs at the moment the prediction was made."""
    row = candidate(SETTLED, 165.0, level=10.0)

    for realised in ({SETTLED: 5}, {SETTLED: 500}, {SETTLED: 165}):
        service, _, _, _ = build_service([row], realised)
        entry = evaluate(service, window_from=SETTLED, window_to=SETTLED).by_model_version[0]
        assert entry.below_calibration.observations == 1
        assert entry.within_calibration.observations == 0


# ======================================================================================
# Model attribution
# ======================================================================================


def test_two_model_versions_never_share_a_denominator() -> None:
    """Criterion 19."""
    rows = [
        candidate(SETTLED, 100.0, model_version="demand_baseline_v1"),
        candidate(SETTLED, 140.0, model_version="demand_baseline_v2"),
    ]
    service, _, _, _ = build_service(rows, {SETTLED: 120})

    result = evaluate(service, window_from=SETTLED, window_to=SETTLED)

    assert [entry.model_version for entry in result.by_model_version] == [
        "demand_baseline_v1",
        "demand_baseline_v2",
    ]
    for entry in result.by_model_version:
        assert entry.within_calibration.observations == 1


def test_a_foreign_model_digest_is_out_of_scope_and_pooled_into_nothing() -> None:
    """Criterion 20. A number attributed to the approved identity must come from it."""
    rows = [
        candidate(SETTLED, 100.0),
        candidate(SETTLED - dt.timedelta(days=1), 100.0, digest="0" * 64),
    ]
    service, _, _, _ = build_service(rows, {SETTLED: 100, SETTLED - dt.timedelta(days=1): 100})

    result = evaluate(service, window_from=SETTLED - dt.timedelta(days=1), window_to=SETTLED)

    assert result.out_of_scope_model_digest == 1
    assert len(result.by_model_version) == 1
    assert result.by_model_version[0].within_calibration.observations == 1


def test_the_three_counts_partition_the_candidates_exactly() -> None:
    """A reader can add them up, which is what makes the denominators auditable."""
    rows = [
        candidate(SETTLED, 100.0),
        candidate(SETTLED + dt.timedelta(days=1), 100.0),
        candidate(SETTLED - dt.timedelta(days=1), 100.0, digest="0" * 64),
    ]
    split = partition(rows, as_of_date=AS_OF, approved_digest=APPROVED_DIGEST)

    assert len(split.eligible) + split.ineligible_by_settlement + split.out_of_scope_model_digest
    assert len(split.eligible) == 1
    assert split.ineligible_by_settlement == 1
    assert split.out_of_scope_model_digest == 1


# ======================================================================================
# Security
# ======================================================================================


def test_the_hotel_is_resolved_before_anything_is_read() -> None:
    """Criterion 23. A non-member must not learn whether this hotel has predictions."""
    service, _, _, scope = build_service([], {})

    source = (BACKEND / "services" / "ml_accuracy.py").read_text(encoding="utf-8")
    body = source[source.index("def evaluate(") :]
    assert body.index("require_hotel") < body.index("scorable_predictions")

    evaluate(service, window_from=SETTLED, window_to=SETTLED)
    assert scope.calls == 1


def test_a_refused_hotel_reads_nothing() -> None:
    class Refusing:
        def require_hotel(self, hotel_public_id: uuid.UUID) -> object:
            raise LookupError("Hotel not found.")

    predictions = FakePredictions([])
    demand = FakeDemand({})
    service = DemandAccuracyService(
        predictions,  # type: ignore[arg-type]
        demand,  # type: ignore[arg-type]
        Refusing(),  # type: ignore[arg-type]
    )

    with pytest.raises(LookupError):
        evaluate(service, window_from=SETTLED, window_to=SETTLED)

    assert predictions.windows == []
    assert demand.calls == 0


def test_every_read_is_bounded_by_the_resolved_hotel() -> None:
    """Criteria 21 and 22's unit half: the id comes from the resolver, never from the caller."""
    service, predictions, _, _ = build_service([candidate(SETTLED, 100.0)], {SETTLED: 100})

    evaluate(service, window_from=SETTLED, window_to=SETTLED)

    assert [window[0] for window in predictions.windows] == [4242]
    assert [window[0] for window in predictions.unsettled_windows] == [4242]


@pytest.mark.parametrize("name", ["evaluate", "_by_model_version"])
def test_no_method_accepts_more_than_one_hotel(name: str) -> None:
    """Criterion 21. There is no parameter through which a second hotel could be named."""
    tree = ast.parse((BACKEND / "services" / "ml_accuracy.py").read_text(encoding="utf-8"))
    function = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name
    )
    arguments = [
        argument.arg
        for argument in [*function.args.args, *function.args.kwonlyargs]
        if argument.arg != "self"
    ]

    assert not [
        argument for argument in arguments if argument.endswith("s") and "hotel" in argument
    ]


def test_the_result_carries_no_internal_identifier() -> None:
    """Criterion 24. The hotel is named by the public UUID the caller already supplied."""
    service, _, _, _ = build_service([candidate(SETTLED, 100.0)], {SETTLED: 100})
    result = evaluate(service, window_from=SETTLED, window_to=SETTLED)

    names = {field.name for field in dataclasses.fields(result)}
    assert "hotel_id" not in names
    assert "id" not in names
    assert "4242" not in str(result.as_dict())


# ======================================================================================
# Metrics
# ======================================================================================


def test_the_metrics_are_the_hand_computed_ones() -> None:
    """Criterion 25. Computed here from the definitions, not by calling the function twice.

    Three observations: predicted 100/110/90 against realised 100/100/100.
    Errors 0, 10, 10 -> MAE 20/3; RMSE sqrt(200/3); sMAPE the three symmetric terms.
    """
    days = [SETTLED - dt.timedelta(days=offset) for offset in (2, 1, 0)]
    rows = [candidate(days[0], 100.0), candidate(days[1], 110.0), candidate(days[2], 90.0)]
    service, _, _, _ = build_service(rows, dict.fromkeys(days, 100))

    metrics = (
        evaluate(service, window_from=days[0], window_to=days[2])
        .by_model_version[0]
        .within_calibration.metrics
    )

    assert metrics.observations == 3
    assert metrics.mae == pytest.approx(20.0 / 3.0)
    assert metrics.rmse == pytest.approx((200.0 / 3.0) ** 0.5)
    expected_smape = 100.0 * (0.0 + 2 * 10 / (100 + 110) + 2 * 10 / (100 + 90)) / 3.0
    assert metrics.smape == pytest.approx(expected_smape)


def test_an_empty_evaluation_reports_none_and_never_zero() -> None:
    """Criterion 26. Zero error and nothing measured are different claims."""
    service, _, _, _ = build_service([], {})

    result = evaluate(service, window_from=SETTLED, window_to=SETTLED)

    assert result.by_model_version == ()

    empty = metrics_for(score_segments([], {})[SEGMENT_WITHIN_CALIBRATION])
    assert empty.observations == 0
    assert empty.mae is None and empty.rmse is None and empty.smape is None


def test_a_target_date_with_no_recorded_occupancy_is_skipped_not_scored_as_zero() -> None:
    """Criterion 27. The denominator does not move; the skip count does."""
    days = [SETTLED - dt.timedelta(days=1), SETTLED]
    rows = [candidate(days[0], 100.0), candidate(days[1], 100.0)]
    service, _, _, _ = build_service(rows, {days[0]: 100})

    segment = (
        evaluate(service, window_from=days[0], window_to=days[1])
        .by_model_version[0]
        .within_calibration
    )

    assert segment.observations == 1
    assert segment.skipped == 1
    assert segment.metrics.mae == pytest.approx(0.0)


def test_the_metric_implementation_is_the_offline_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 28. Proves there is no second implementation hiding in the backend."""
    import ml.metrics

    sentinel = object()
    monkeypatch.setattr(ml.metrics, "metric_set", lambda *a, **k: sentinel)

    assert metrics_for(score_segments([], {})[SEGMENT_WITHIN_CALIBRATION]) is sentinel


def test_no_metric_formula_is_restated_in_the_backend() -> None:
    """The formulas have one definition, in ml/metrics.py, and this keeps it that way."""
    for source in BACKEND.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for forbidden in (
            "mean_absolute_error",
            "root_mean_squared_error",
            "symmetric_mean_absolute_percentage_error",
            "absolute_errors",
        ):
            assert forbidden not in names, f"{source.name} redefines {forbidden}"


# ======================================================================================
# Determinism
# ======================================================================================


def test_two_identical_evaluations_are_bit_identical() -> None:
    """Criterion 29."""
    days = [SETTLED - dt.timedelta(days=offset) for offset in (3, 2, 1, 0)]
    rows = [candidate(day, 100.0 + index) for index, day in enumerate(days)]
    realised = {day: 100 + index * 2 for index, day in enumerate(days)}

    # The same hotel for both: a fresh public id would differ for a reason that has nothing
    # to do with whether the EVALUATION is deterministic.
    hotel = FakeHotel(id=4242, public_id=uuid.uuid4())
    first, _, _, _ = build_service(rows, realised, hotel=hotel)
    second, _, _, _ = build_service(rows, realised, hotel=hotel)

    left = evaluate(first, window_from=days[0], window_to=days[-1])
    right = evaluate(second, window_from=days[0], window_to=days[-1])

    assert left.as_dict() == right.as_dict()


def test_the_service_holds_no_session_and_cannot_write() -> None:
    """Criterion 30. Structural: there is nothing to commit to."""
    tree = ast.parse((BACKEND / "services" / "ml_accuracy.py").read_text(encoding="utf-8"))
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
    """Criterion 41. Query construction belongs to the repository, without exception."""
    source = (BACKEND / "services" / "ml_accuracy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not [module for module in imported if module.startswith("sqlalchemy")]
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden in ("select", "and_", "or_", "func.count"):
        assert forbidden not in calls, forbidden


@pytest.mark.parametrize("forbidden", ["ml.", "fastapi", "starlette", "sklearn"])
def test_the_service_imports_neither_the_offline_package_nor_a_framework(forbidden: str) -> None:
    """Criteria 39 and 40."""
    source = (BACKEND / "services" / "ml_accuracy.py").read_text(encoding="utf-8")
    pattern = re.compile(rf"^\s*(?:import|from)\s+{re.escape(forbidden)}", re.MULTILINE)

    assert not pattern.search(source)


# ======================================================================================
# Contract preservation
# ======================================================================================


def test_the_api_surface_is_the_one_stage_73_published() -> None:
    """Criterion 32, as Stage 7.3 left it.

    Stage 6.9 added no endpoint and the surface stayed at 52 / 84. Stage 7.3 published this
    service over HTTP deliberately, taking it to 54 / 86; asserting the old numbers here would
    now be asserting that a later stage did not happen.

    What the criterion was actually protecting is below and is unchanged: this module's own
    result type is internal, and no published schema carries a field it withholds.
    """
    schema = create_app(Settings(environment="test", debug=True)).openapi()
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    operations = sum(1 for path in schema["paths"].values() for verb in path if verb in methods)

    # Stage 7.7 added POST .../copilot/ask: 54 / 86 -> 55 / 87.
    assert len(schema["paths"]) == 55
    assert operations == 87


def test_the_frozen_result_type_is_still_not_an_http_contract() -> None:
    """The point of Criterion 32, kept.

    ``AccuracyEvaluation`` and its members are dataclasses this stage owns. Stage 7.3 publishes
    a SEPARATE set of Pydantic models and projects onto them, precisely so that adding a field
    here does not publish it. If one of these names ever appears in the document, the projection
    has been replaced by the dataclass itself and the withholding below is no longer enforced.
    """
    schema = create_app(Settings(environment="test", debug=True)).openapi()
    published = set(schema["components"]["schemas"])

    assert "AccuracyEvaluation" not in published
    assert "ModelVersionAccuracy" not in published
    assert "SegmentAccuracy" not in published


@pytest.mark.parametrize("withheld", ["canonical_model_digest", "scored_feature_digests"])
def test_no_published_schema_carries_a_field_this_stage_withholds(withheld: str) -> None:
    """Criterion 32's real content: the digests are computed, and they do not leave.

    Checked against every schema in the document rather than the two this stage's endpoint
    returns, so a later stage cannot publish the same field from somewhere else.
    """
    schema = create_app(Settings(environment="test", debug=True)).openapi()

    carrying = [
        name
        for name, definition in schema["components"]["schemas"].items()
        if withheld in definition.get("properties", {})
    ]

    assert carrying == []


def test_the_migration_chain_did_not_move() -> None:
    """Criteria 33 and 34. Stage 6.9 adds no migration."""
    versions = REPOSITORY_ROOT / "database" / "migrations" / "versions"
    revisions = sorted(path.name for path in versions.glob("*.py"))

    assert len(revisions) == 13
    assert revisions[-1].endswith("0013_llm_invocations.py")


def test_the_model_identity_is_untouched() -> None:
    """Criteria 35 and 36."""
    assert APPROVED_MODEL.model_version == "demand_baseline_v1"
    assert (
        APPROVED_MODEL.canonical_model_digest
        == "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"
    )


def test_the_accuracy_path_never_loads_the_artifact() -> None:
    """Criterion 37. It reads numbers the artifact already produced; it does not run one."""
    for module in ("ml/accuracy.py", "ml/accuracy_protocol.py", "services/ml_accuracy.py"):
        source = (BACKEND / module).read_text(encoding="utf-8")
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }
        for forbidden in ("approved_model", "predict_room_nights", "artifact_store.configure"):
            assert forbidden not in calls, f"{module}: {forbidden}"


def test_the_backend_requirements_did_not_move() -> None:
    """Criterion 43. ml/metrics.py is pure standard library, so the bridge costs nothing."""
    declared = [
        line.strip().lower()
        for line in (REPOSITORY_ROOT / "backend" / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert [line for line in declared if line.startswith("scikit-learn")] == ["scikit-learn==1.9.1"]
    for library in ("pandas", "torch", "tensorflow", "xgboost", "lightgbm", "numpy"):
        assert not [line for line in declared if line.startswith(library)], library


def test_the_shipped_ml_allowlist_did_not_move() -> None:
    """Criterion 44. ``metrics`` was already one of the thirteen."""
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "-ne 13 ]" in workflow
    assert "metrics \\" in workflow or " metrics " in workflow


# ======================================================================================
# Logging
# ======================================================================================


def test_one_evaluation_emits_exactly_one_event(events: RecordingHandler) -> None:
    """Criterion 45."""
    service, _, _, _ = build_service([candidate(SETTLED, 100.0)], {SETTLED: 100})

    evaluate(service, window_from=SETTLED, window_to=SETTLED)

    emitted = [record for record in events.records if hasattr(record, "outcome")]
    assert len(emitted) == 1


def test_the_event_carries_exactly_the_five_approved_fields(events: RecordingHandler) -> None:
    """Criterion 46. As a set, so a sixth field cannot arrive unnoticed.

    This is also what keeps the leakage test below honest: it scans exactly these fields, so
    if the set ever emptied it would pass by having nothing left to look at.
    """
    days = [SETTLED - dt.timedelta(days=1), SETTLED]
    rows = [candidate(days[0], 100.0), candidate(days[1], 100.0)]
    service, _, _, _ = build_service(rows, {days[0]: 100})

    evaluate(service, window_from=days[0], window_to=days[1])

    event = next(record for record in events.records if hasattr(record, "outcome"))
    assert set(attached(event)) == {
        "outcome",
        "model_version",
        "predictions_scored",
        "predictions_skipped",
        "duration_ms",
    }
    assert field(event, "outcome") == "evaluated"
    assert field(event, "model_version") == "demand_baseline_v1"
    assert field(event, "predictions_scored") == 1
    assert field(event, "predictions_skipped") == 1
    assert isinstance(field(event, "duration_ms"), float)


def test_an_evaluation_with_nothing_to_score_says_so(events: RecordingHandler) -> None:
    service, _, _, _ = build_service([], {})

    evaluate(service, window_from=SETTLED, window_to=SETTLED)

    event = next(record for record in events.records if hasattr(record, "outcome"))
    assert field(event, "outcome") == "nothing_to_score"
    assert field(event, "predictions_scored") == 0


def test_the_event_names_nothing_a_hotel_owns(events: RecordingHandler) -> None:
    """Criterion 47. Captured records, not a source scan."""
    rows = [candidate(SETTLED, 8317.25, level=6143.0, feature_digest="a" * 64)]
    service, _, _, scope = build_service(rows, {SETTLED: 5209})

    result = evaluate(service, window_from=SETTLED, window_to=SETTLED)

    assert events.records, "nothing was captured -- the assertion below would be vacuous"
    # `duration_ms` is excluded, and the forbidden values are deliberately distinctive. It is a
    # machine timing whose digits are arbitrary, so a short forbidden value collides with it
    # sooner or later and fails for a reason that is not a leak -- "99" against a duration of
    # 0.099 did exactly that. The field is not left unchecked: it is one of the five pinned by
    # name in `test_the_event_carries_exactly_the_five_approved_fields`.
    rendered = "\n".join(
        f"{record.getMessage()} "
        f"{ {k: v for k, v in attached(record).items() if k != 'duration_ms'}!r}"
        for record in events.records
    )
    for forbidden in (
        "8317.25",
        "5209",
        "6143",
        "a" * 64,
        APPROVED_DIGEST,
        str(scope._hotel.public_id),
        "4242",
        "demand_lag_7",
        "mae",
        "rmse",
        "smape",
        "SELECT",
        "password",
        "secret",
        "/app/",
    ):
        assert forbidden not in rendered, forbidden
    assert result.by_model_version[0].within_calibration.metrics.mae is not None


# ======================================================================================
# Claims
# ======================================================================================


def test_the_result_carries_the_protocol_that_produced_it(events: RecordingHandler) -> None:
    """Criterion 48. A metric without its rules is a number looking for a caption."""
    service, _, _, _ = build_service([candidate(SETTLED, 100.0)], {SETTLED: 100})

    result = evaluate(service, window_from=SETTLED, window_to=SETTLED)

    assert result.protocol_version == PROTOCOL.version
    assert result.protocol_checksum == PROTOCOL.checksum
    assert result.as_of_date == AS_OF
    assert result.settlement_lag_days == 28
    assert result.establishes_production_accuracy is False


def test_production_accuracy_remains_unestablished() -> None:
    """Criterion 49. Stage 6.9 measures under a protocol; it certifies nothing."""
    docs = REPOSITORY_ROOT / "docs"
    card = (docs / "ml-model-card.md").read_text(encoding="utf-8")
    serving = (docs / "ml-serving.md").read_text(encoding="utf-8")
    runtime = (docs / "ml-production-runtime.md").read_text(encoding="utf-8")
    measurement = (docs / "ml-accuracy-measurement.md").read_text(encoding="utf-8")

    assert "| Production accuracy established | **No** |" in card
    assert "No production accuracy is established" in serving
    assert "Production accuracy is not established" in runtime
    assert "does not establish production accuracy" in measurement


def test_no_test_in_this_repository_asserts_the_model_is_good() -> None:
    """Criterion 50. Nothing here is a threshold, a pass mark or a baseline comparison."""
    pattern = re.compile(r"assert\s+[^\n]*\b(mae|rmse|smape)\b[^\n]*[<>]=?\s*[0-9]", re.IGNORECASE)
    for source in (REPOSITORY_ROOT / "tests").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert not pattern.search(text), f"{source.name} asserts a model-quality threshold"
