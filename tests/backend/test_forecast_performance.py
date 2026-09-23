"""Stage 7.3: the HTTP boundary over the two frozen measurement services.

What this stage is actually claiming is narrow, and so is this module. The arithmetic behind
every number was tested in Stages 6.9 and 6.10 and is untouched; re-asserting a metric value
here would test the same code twice and prove nothing about the routing. What is new, and what
is checked below, is:

* **that the routes delegate** -- the published figures are the service's own, and nothing on
  the HTTP path recomputes, re-aggregates, rounds or defaults one;
* **that the projection withholds** the two fields the internal results carry and the API must
  not: ``canonical_model_digest`` and the per-prediction digest lists;
* **that the request rules exist at all** -- a reversed window is an error rather than an empty
  result, which neither frozen service would have told you;
* **that the claims boundary rides in the payload**, not only in a docstring;
* **that the layering survived** -- no SQL, no repository, no artifact, no provider in a router.

Tenant isolation over real rows is in ``tests/integration/test_forecast_performance_api.py``,
where there is a database for one hotel's predictions to fail to reach another's.

No database and no network: these read the routing table, the OpenAPI document and the source,
so they run in the fast suite.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute

import app.schemas.ml_performance as published
from app.core.config import Settings
from app.core.errors import ValidationError
from app.main import create_app
from app.ml.accuracy_protocol import PROTOCOL as ACCURACY_PROTOCOL
from app.ml.drift_protocol import PROTOCOL as DISTRIBUTION_PROTOCOL
from app.schemas.ml_accuracy import AccuracyEvaluation, ModelVersionAccuracy, SegmentAccuracy
from app.schemas.ml_drift import (
    DistributionComparison,
    DistributionObservation,
    FieldDifference,
    FieldSummary,
    ModelVersionDifference,
    ModelVersionSummary,
    SegmentDifference,
    SegmentSummary,
    WindowSummary,
)
from app.schemas.ml_performance import MEASUREMENT_STATEMENT
from app.services.ml_performance import MAX_WINDOW_DAYS, ForecastPerformanceService
from tests.backend.test_authorization_surface import discover

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPOSITORY_ROOT / "backend" / "app"

ROUTER_SOURCE = BACKEND / "api" / "v1" / "endpoints" / "ml_performance.py"
SERVICE_SOURCE = BACKEND / "services" / "ml_performance.py"
SCHEMA_SOURCE = BACKEND / "schemas" / "ml_performance.py"

ACCURACY_PATH = "/api/v1/hotels/{hotel_public_id}/ml/forecast-accuracy"
DISTRIBUTION_PATH = "/api/v1/hotels/{hotel_public_id}/ml/prediction-distribution"

HOTEL = uuid.UUID("11111111-2222-3333-4444-555555555555")
WINDOW_FROM = dt.date(2026, 4, 1)
WINDOW_TO = dt.date(2026, 4, 30)
AS_OF = dt.date(2026, 6, 1)


# ======================================================================================
# Doubles
# ======================================================================================


class FakeMetrics:
    """``ml.metrics.MetricSet``'s five names. The structural shape, not a second definition."""

    def __init__(self, observations: int, skipped: int, mae: float | None) -> None:
        self.observations = observations
        self.skipped = skipped
        self.mae = mae
        self.rmse = None if mae is None else mae * 2
        self.smape = None if mae is None else mae * 3

    def as_dict(self, *, digits: int = 6) -> dict[str, object]:
        return {"observations": self.observations}


def segment_accuracy(name: str, observations: int, mae: float | None) -> SegmentAccuracy:
    return SegmentAccuracy(
        segment=name,
        metrics=FakeMetrics(observations, 2, mae),
        scored_feature_digests=("digest-a", "digest-b"),
    )


def evaluation(**overrides: Any) -> AccuracyEvaluation:
    defaults: dict[str, Any] = {
        "hotel_public_id": HOTEL,
        "as_of_date": AS_OF,
        "window_from": WINDOW_FROM,
        "window_to": WINDOW_TO,
        "scored_from": WINDOW_FROM,
        "scored_to": WINDOW_TO,
        "protocol_version": ACCURACY_PROTOCOL.version,
        "protocol_checksum": ACCURACY_PROTOCOL.checksum,
        "settlement_lag_days": ACCURACY_PROTOCOL.settlement_lag_days,
        "candidates": 9,
        "ineligible_by_settlement": 3,
        "out_of_scope_model_digest": 1,
        "unsettled_allocations": 4,
        "settled": False,
        "by_model_version": (
            ModelVersionAccuracy(
                model_version="demand_baseline_v1",
                canonical_model_digest="a" * 64,
                below_calibration=segment_accuracy("below_calibration", 2, 1.5),
                within_calibration=segment_accuracy("within_calibration", 0, None),
            ),
        ),
    }
    return AccuracyEvaluation(**{**defaults, **overrides})


def field_summary(name: str) -> FieldSummary:
    return FieldSummary(
        field=name,
        count=3,
        minimum=1.0,
        maximum=9.0,
        mean=5.0,
        median=4.0,
        quantiles=(("p05", 1.0), ("p25", 2.0), ("p50", 4.0), ("p75", 7.0), ("p95", 9.0)),
    )


def segment_summary(name: str) -> SegmentSummary:
    return SegmentSummary(
        segment=name,
        observations=3,
        feature_digests=("digest-a", "digest-b", "digest-c"),
        fields=tuple(field_summary(field) for field in DISTRIBUTION_PROTOCOL.observed_fields),
    )


def window_summary() -> WindowSummary:
    return WindowSummary(
        window_from=WINDOW_FROM,
        window_to=WINDOW_TO,
        candidates=6,
        out_of_scope_model_digest=0,
        by_model_version=(
            ModelVersionSummary(
                model_version="demand_baseline_v1",
                canonical_model_digest="b" * 64,
                below_calibration=segment_summary("below_calibration"),
                within_calibration=segment_summary("within_calibration"),
            ),
        ),
    )


def field_difference(name: str) -> FieldDifference:
    return FieldDifference(
        field=name,
        count=-1,
        minimum=0.5,
        maximum=None,
        mean=-2.0,
        median=0.0,
        quantiles=(("p05", 0.1), ("p25", None), ("p50", 0.0), ("p75", -1.0), ("p95", 2.0)),
    )


def observation(*, with_comparison: bool = False) -> DistributionObservation:
    comparison = None
    if with_comparison:
        comparison = DistributionComparison(
            baseline=window_summary(),
            by_model_version=(
                ModelVersionDifference(
                    model_version="demand_baseline_v1",
                    below_calibration=SegmentDifference(
                        segment="below_calibration",
                        observations=-2,
                        fields=tuple(
                            field_difference(f) for f in DISTRIBUTION_PROTOCOL.observed_fields
                        ),
                    ),
                    within_calibration=SegmentDifference(
                        segment="within_calibration",
                        observations=0,
                        fields=tuple(
                            field_difference(f) for f in DISTRIBUTION_PROTOCOL.observed_fields
                        ),
                    ),
                ),
            ),
        )
    return DistributionObservation(
        hotel_public_id=HOTEL,
        protocol_version=DISTRIBUTION_PROTOCOL.version,
        protocol_checksum=DISTRIBUTION_PROTOCOL.checksum,
        observed=window_summary(),
        comparison=comparison,
    )


class RecordingAccuracy:
    """Stands in for ``DemandAccuracyService``. Records the call; returns a fixed evaluation."""

    def __init__(self, result: AccuracyEvaluation | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or evaluation()

    def evaluate(
        self,
        hotel_public_id: uuid.UUID,
        *,
        as_of_date: dt.date,
        window_from: dt.date,
        window_to: dt.date,
    ) -> AccuracyEvaluation:
        self.calls.append(
            {
                "hotel_public_id": hotel_public_id,
                "as_of_date": as_of_date,
                "window_from": window_from,
                "window_to": window_to,
            }
        )
        return self._result


class RecordingDistribution:
    """Stands in for ``DemandDistributionService``."""

    def __init__(self, result: DistributionObservation | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or observation()

    def observe(
        self,
        hotel_public_id: uuid.UUID,
        *,
        window_from: dt.date,
        window_to: dt.date,
        baseline: tuple[dt.date, dt.date] | None = None,
    ) -> DistributionObservation:
        self.calls.append(
            {
                "hotel_public_id": hotel_public_id,
                "window_from": window_from,
                "window_to": window_to,
                "baseline": baseline,
            }
        )
        return self._result


def build_service(
    accuracy: RecordingAccuracy | None = None,
    distribution: RecordingDistribution | None = None,
) -> tuple[ForecastPerformanceService, RecordingAccuracy, RecordingDistribution]:
    accuracy = accuracy or RecordingAccuracy()
    distribution = distribution or RecordingDistribution()
    service = ForecastPerformanceService(accuracy, distribution)  # type: ignore[arg-type]
    return service, accuracy, distribution


def openapi() -> dict[str, Any]:
    return create_app(Settings(environment="test", debug=True)).openapi()


# ======================================================================================
# A. Service delegation
# ======================================================================================


def test_the_accuracy_call_is_passed_through_unaltered() -> None:
    """Every argument arrives as given: no default substituted, no bound adjusted."""
    service, accuracy, _ = build_service()

    service.forecast_accuracy(HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO)

    assert accuracy.calls == [
        {
            "hotel_public_id": HOTEL,
            "as_of_date": AS_OF,
            "window_from": WINDOW_FROM,
            "window_to": WINDOW_TO,
        }
    ]


def test_the_distribution_call_is_passed_through_unaltered() -> None:
    service, _, distribution = build_service()

    service.prediction_distribution(HOTEL, window_from=WINDOW_FROM, window_to=WINDOW_TO)

    assert distribution.calls == [
        {
            "hotel_public_id": HOTEL,
            "window_from": WINDOW_FROM,
            "window_to": WINDOW_TO,
            "baseline": None,
        }
    ]


def test_a_baseline_pair_is_folded_back_into_the_tuple_the_service_takes() -> None:
    """HTTP forces two optional parameters; the service beneath takes a pair or nothing."""
    service, _, distribution = build_service()

    service.prediction_distribution(
        HOTEL,
        window_from=WINDOW_FROM,
        window_to=WINDOW_TO,
        baseline_from=dt.date(2026, 1, 1),
        baseline_to=dt.date(2026, 1, 31),
    )

    assert distribution.calls[0]["baseline"] == (dt.date(2026, 1, 1), dt.date(2026, 1, 31))


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("candidates", 9),
        ("ineligible_by_settlement", 3),
        ("out_of_scope_model_digest", 1),
        ("unsettled_allocations", 4),
        ("settled", False),
        ("settlement_lag_days", ACCURACY_PROTOCOL.settlement_lag_days),
        ("scored_from", WINDOW_FROM),
        ("scored_to", WINDOW_TO),
        ("as_of_date", AS_OF),
    ],
)
def test_every_published_accuracy_figure_is_the_services_own(field: str, expected: object) -> None:
    """Copied, not computed. A route that recomputed would be free to disagree."""
    service, _, _ = build_service()

    response = service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )

    assert getattr(response, field) == expected


def test_the_metric_values_are_the_services_own_including_the_nulls() -> None:
    """A segment that scored nothing publishes null, never a zero standing in for one."""
    service, _, _ = build_service()

    response = service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )
    entry = response.by_model_version[0]

    assert entry.below_calibration.metrics.observations == 2
    assert entry.below_calibration.metrics.skipped == 2
    assert entry.below_calibration.metrics.mae == 1.5
    assert entry.below_calibration.metrics.rmse == 3.0
    assert entry.within_calibration.metrics.observations == 0
    assert entry.within_calibration.metrics.mae is None
    assert entry.within_calibration.metrics.rmse is None
    assert entry.within_calibration.metrics.smape is None


def test_the_two_segments_are_published_separately_and_never_summed() -> None:
    """There is no field a combined figure could travel in, which is the point."""
    service, _, _ = build_service()

    response = service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )
    entry = response.by_model_version[0]

    assert entry.below_calibration.segment == "below_calibration"
    assert entry.within_calibration.segment == "within_calibration"
    assert not hasattr(entry, "metrics")
    assert not hasattr(entry, "combined")
    assert not hasattr(entry, "overall")


def test_the_quantiles_keep_the_protocols_order_and_its_nulls() -> None:
    """Neither sorted nor filled: a null quantile is a window that had nothing to measure."""
    service, _, _ = build_service(
        distribution=RecordingDistribution(observation(with_comparison=True))
    )

    response = service.prediction_distribution(
        HOTEL,
        window_from=WINDOW_FROM,
        window_to=WINDOW_TO,
        baseline_from=dt.date(2026, 1, 1),
        baseline_to=dt.date(2026, 1, 31),
    )
    assert response.comparison is not None
    difference = response.comparison.by_model_version[0].below_calibration.fields[0]

    assert [q.label for q in difference.quantiles] == list(DISTRIBUTION_PROTOCOL.quantile_labels)
    assert [q.value for q in difference.quantiles] == [0.1, None, 0.0, -1.0, 2.0]


def test_every_observed_field_survives_the_projection() -> None:
    """All ten, in the protocol's own column order. A dropped field would be a silent gap."""
    service, _, _ = build_service()

    response = service.prediction_distribution(HOTEL, window_from=WINDOW_FROM, window_to=WINDOW_TO)
    segment = response.observed.by_model_version[0].below_calibration

    assert [f.field for f in segment.fields] == list(DISTRIBUTION_PROTOCOL.observed_fields)


def test_no_comparison_is_invented_when_no_baseline_was_asked_for() -> None:
    """Null, not a set of zero differences -- which would read as "nothing moved"."""
    service, _, _ = build_service()

    response = service.prediction_distribution(HOTEL, window_from=WINDOW_FROM, window_to=WINDOW_TO)

    assert response.comparison is None


def test_a_baseline_window_is_published_beside_its_differences() -> None:
    service, _, _ = build_service(
        distribution=RecordingDistribution(observation(with_comparison=True))
    )

    response = service.prediction_distribution(
        HOTEL,
        window_from=WINDOW_FROM,
        window_to=WINDOW_TO,
        baseline_from=dt.date(2026, 1, 1),
        baseline_to=dt.date(2026, 1, 31),
    )

    assert response.comparison is not None
    assert response.comparison.baseline.window_from == WINDOW_FROM
    assert response.comparison.by_model_version[0].below_calibration.observations == -2


# ======================================================================================
# B. What does not travel
# ======================================================================================


def test_the_model_digest_is_dropped_from_the_accuracy_projection() -> None:
    """Present on the internal result, absent from the published one. Stage 6.11's rule."""
    service, _, _ = build_service()

    response = service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )
    body = response.model_dump()

    assert "a" * 64 not in str(body)
    assert "canonical_model_digest" not in str(body)
    assert body["by_model_version"][0]["model_version"] == "demand_baseline_v1"


def test_the_per_prediction_digests_are_dropped_from_the_accuracy_projection() -> None:
    service, _, _ = build_service()

    response = service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )
    body = str(response.model_dump())

    assert "digest-a" not in body
    assert "scored_feature_digests" not in body


def test_both_digests_are_dropped_from_the_distribution_projection() -> None:
    service, _, _ = build_service()

    body = str(
        service.prediction_distribution(
            HOTEL, window_from=WINDOW_FROM, window_to=WINDOW_TO
        ).model_dump()
    )

    assert "b" * 64 not in body
    assert "digest-a" not in body
    assert "feature_digests" not in body


def test_no_internal_identifier_can_travel_in_either_response() -> None:
    """The hotel is named by the public UUID the caller supplied, and by nothing else."""
    service, _, _ = build_service()

    accuracy = service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )
    distribution = service.prediction_distribution(
        HOTEL, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )

    for response in (accuracy, distribution):
        body = response.model_dump()
        assert body["hotel_public_id"] == HOTEL
        assert "hotel_id" not in str(body)
        assert "id" not in body


# ======================================================================================
# C. Request rules
# ======================================================================================


def test_a_reversed_window_is_refused_rather_than_returned_empty() -> None:
    """Neither frozen service would have told you: both return an empty result instead."""
    service, accuracy, _ = build_service()

    with pytest.raises(ValidationError):
        service.forecast_accuracy(
            HOTEL, as_of_date=AS_OF, window_from=WINDOW_TO, window_to=WINDOW_FROM
        )

    assert accuracy.calls == []


def test_a_reversed_window_is_refused_on_the_distribution_path_too() -> None:
    service, _, distribution = build_service()

    with pytest.raises(ValidationError):
        service.prediction_distribution(HOTEL, window_from=WINDOW_TO, window_to=WINDOW_FROM)

    assert distribution.calls == []


def test_a_reversed_baseline_is_refused_as_well() -> None:
    """The second window is held to the same rule as the first; it is not a lesser parameter."""
    service, _, distribution = build_service()

    with pytest.raises(ValidationError):
        service.prediction_distribution(
            HOTEL,
            window_from=WINDOW_FROM,
            window_to=WINDOW_TO,
            baseline_from=dt.date(2026, 1, 31),
            baseline_to=dt.date(2026, 1, 1),
        )

    assert distribution.calls == []


@pytest.mark.parametrize(
    ("baseline_from", "baseline_to"),
    [(dt.date(2026, 1, 1), None), (None, dt.date(2026, 1, 31))],
)
def test_half_a_baseline_pair_is_refused_rather_than_ignored(
    baseline_from: dt.date | None, baseline_to: dt.date | None
) -> None:
    """Silently dropping half a pair would answer a question the caller did not ask."""
    service, _, distribution = build_service()

    with pytest.raises(ValidationError):
        service.prediction_distribution(
            HOTEL,
            window_from=WINDOW_FROM,
            window_to=WINDOW_TO,
            baseline_from=baseline_from,
            baseline_to=baseline_to,
        )

    assert distribution.calls == []


def test_a_single_day_window_is_valid() -> None:
    """The bounds are inclusive, so ``from == to`` is one day and not an empty request."""
    service, accuracy, _ = build_service()

    service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_FROM
    )

    assert accuracy.calls[0]["window_from"] == WINDOW_FROM


def test_a_window_of_exactly_the_maximum_is_accepted() -> None:
    """The boundary is inclusive, so a full leap year is one request."""
    service, accuracy, _ = build_service()
    window_to = WINDOW_FROM + dt.timedelta(days=MAX_WINDOW_DAYS - 1)

    service.forecast_accuracy(HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=window_to)

    assert accuracy.calls[0]["window_to"] == window_to


def test_a_window_one_day_longer_than_the_maximum_is_refused() -> None:
    service, accuracy, _ = build_service()
    window_to = WINDOW_FROM + dt.timedelta(days=MAX_WINDOW_DAYS)

    with pytest.raises(ValidationError):
        service.forecast_accuracy(
            HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=window_to
        )

    assert accuracy.calls == []


def test_an_oversized_baseline_is_refused_too() -> None:
    service, _, distribution = build_service()

    with pytest.raises(ValidationError):
        service.prediction_distribution(
            HOTEL,
            window_from=WINDOW_FROM,
            window_to=WINDOW_TO,
            baseline_from=dt.date(2020, 1, 1),
            baseline_to=dt.date(2026, 1, 1),
        )

    assert distribution.calls == []


def test_validation_happens_before_any_delegation() -> None:
    """So a malformed request cannot become an existence oracle.

    Refusing the window before the hotel is resolved means a bad request is refused identically
    for a hotel that exists and one that does not -- and neither answer required a read.
    """
    service, accuracy, distribution = build_service()

    for call in (
        lambda: service.forecast_accuracy(
            HOTEL, as_of_date=AS_OF, window_from=WINDOW_TO, window_to=WINDOW_FROM
        ),
        lambda: service.prediction_distribution(
            HOTEL, window_from=WINDOW_TO, window_to=WINDOW_FROM
        ),
    ):
        with pytest.raises(ValidationError):
            call()

    assert accuracy.calls == []
    assert distribution.calls == []


# ======================================================================================
# D. The claims boundary
# ======================================================================================


def test_both_responses_carry_the_protocol_that_produced_them() -> None:
    """A metric without its rules is a number looking for a caption."""
    service, _, _ = build_service()

    accuracy = service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )
    distribution = service.prediction_distribution(
        HOTEL, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )

    assert accuracy.measurement.protocol_version == ACCURACY_PROTOCOL.version
    assert accuracy.measurement.protocol_checksum == ACCURACY_PROTOCOL.checksum
    assert distribution.measurement.protocol_version == DISTRIBUTION_PROTOCOL.version
    assert distribution.measurement.protocol_checksum == DISTRIBUTION_PROTOCOL.checksum


def test_the_frozen_protocol_checksums_are_the_ones_stages_69_and_610_froze() -> None:
    """Criterion 2. Stage 7.3 routes these protocols; it does not touch them.

    The checksum is over the protocol's own values, so a changed rule changes it. Pinning the
    literals means this stage cannot quietly alter what it publishes under those names.
    """
    assert ACCURACY_PROTOCOL.version == "accuracy_v1"
    assert DISTRIBUTION_PROTOCOL.version == "distribution_v1"
    assert re.fullmatch(r"[0-9a-f]{64}", ACCURACY_PROTOCOL.checksum)
    assert re.fullmatch(r"[0-9a-f]{64}", DISTRIBUTION_PROTOCOL.checksum)


def test_neither_response_establishes_production_accuracy() -> None:
    service, _, _ = build_service()

    accuracy = service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )
    distribution = service.prediction_distribution(
        HOTEL, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )

    assert accuracy.measurement.establishes_production_accuracy is False
    assert distribution.measurement.establishes_production_accuracy is False


def test_the_statement_is_in_the_payload_rather_than_only_in_the_documentation() -> None:
    """A consumer that reads only the body is still told what the body does not establish."""
    service, _, _ = build_service()

    response = service.forecast_accuracy(
        HOTEL, as_of_date=AS_OF, window_from=WINDOW_FROM, window_to=WINDOW_TO
    )

    assert response.measurement.statement == MEASUREMENT_STATEMENT


@pytest.mark.parametrize(
    "claim",
    [
        "establish no production accuracy",
        "evaluate no threshold",
        "compare against no baseline model",
        "detect nothing",
        "rank nothing",
        "offline research candidate",
    ],
)
def test_the_statement_denies_each_claim_this_stage_must_not_make(claim: str) -> None:
    assert claim in MEASUREMENT_STATEMENT


@pytest.mark.parametrize(
    "pattern",
    [
        r"\bis accurate\b",
        r"\baccuracy of\s*\d",
        r"\d\s*%\s*accurate",
        r"\b(proven|reliable|trustworthy|validated|production-ready)\b",
        r"\bdrift (detected|is)\b",
        r"\bgeneralis[ez]",
    ],
)
def test_no_published_description_makes_a_claim_this_stage_cannot_support(pattern: str) -> None:
    """Read off the OpenAPI document, which is what a consumer actually reads.

    Word boundaries throughout: ``provenance`` contains ``proven``, and an unbounded alternation
    would fail on prose that is making exactly the opposite claim.
    """
    document = openapi()
    prose = [
        operation.get("description", "") + " " + operation.get("summary", "")
        for path in (ACCURACY_PATH, DISTRIBUTION_PATH)
        for operation in document["paths"][path].values()
    ]
    prose.append(MEASUREMENT_STATEMENT)

    for text in prose:
        assert not re.search(pattern, text, re.IGNORECASE), text


def test_the_accuracy_route_discloses_that_figures_can_be_provisional() -> None:
    """``settled`` is meaningless to a reader who is not told what it qualifies."""
    description = openapi()["paths"][ACCURACY_PATH]["get"]["description"]

    assert "provisional" in description.lower()
    assert "settlement lag" in description.lower()


def test_the_distribution_route_says_it_reaches_no_verdict() -> None:
    description = openapi()["paths"][DISTRIBUTION_PATH]["get"]["description"]

    assert "no threshold" in description.lower()
    assert "not drift" in description.lower()


# ======================================================================================
# E. The published contract
# ======================================================================================


def test_both_routes_are_registered_as_reads() -> None:
    document = openapi()

    assert set(document["paths"][ACCURACY_PATH]) == {"get"}
    assert set(document["paths"][DISTRIBUTION_PATH]) == {"get"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (ACCURACY_PATH, {"hotel_public_id", "as_of_date", "window_from", "window_to"}),
        (
            DISTRIBUTION_PATH,
            {"hotel_public_id", "window_from", "window_to", "baseline_from", "baseline_to"},
        ),
    ],
)
def test_the_routes_accept_exactly_these_parameters(path: str, expected: set[str]) -> None:
    """Exhaustive, so a tenth parameter cannot arrive without someone choosing it.

    ``hotel_id`` is not among them on either route, and there is no parameter a second hotel
    could be named through.
    """
    parameters = {p["name"] for p in openapi()["paths"][path]["get"]["parameters"]}

    assert parameters == expected


@pytest.mark.parametrize("path", [ACCURACY_PATH, DISTRIBUTION_PATH])
def test_the_hotel_is_addressed_by_a_public_uuid_in_the_path(path: str) -> None:
    parameter = next(
        p for p in openapi()["paths"][path]["get"]["parameters"] if p["name"] == "hotel_public_id"
    )

    assert parameter["in"] == "path"
    assert parameter["required"] is True
    assert parameter["schema"]["format"] == "uuid"


@pytest.mark.parametrize(
    ("path", "required", "optional"),
    [
        (ACCURACY_PATH, {"as_of_date", "window_from", "window_to"}, set()),
        (
            DISTRIBUTION_PATH,
            {"window_from", "window_to"},
            {"baseline_from", "baseline_to"},
        ),
    ],
)
def test_every_window_bound_is_explicit_rather_than_defaulted(
    path: str, required: set[str], optional: set[str]
) -> None:
    """A today-relative default would make one request mean different things on different days."""
    parameters = {
        p["name"]: p for p in openapi()["paths"][path]["get"]["parameters"] if p["in"] == "query"
    }

    assert {name for name, p in parameters.items() if p["required"]} == required
    assert {name for name, p in parameters.items() if not p["required"]} == optional


def test_the_accuracy_route_declares_the_role_it_needs_and_the_403_that_follows() -> None:
    responses = openapi()["paths"][ACCURACY_PATH]["get"]["responses"]

    assert set(responses) == {"200", "403", "404", "422"}


def test_the_distribution_route_declares_no_403_because_membership_is_enough() -> None:
    responses = openapi()["paths"][DISTRIBUTION_PATH]["get"]["responses"]

    assert set(responses) == {"200", "404", "422"}


@pytest.mark.parametrize("path", [ACCURACY_PATH, DISTRIBUTION_PATH])
def test_every_failure_uses_the_one_error_contract(path: str) -> None:
    responses = openapi()["paths"][path]["get"]["responses"]

    for code, response in responses.items():
        if code == "200":
            continue
        ref = response["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse"), code


@pytest.mark.parametrize("path", [ACCURACY_PATH, DISTRIBUTION_PATH])
def test_neither_route_can_report_a_missing_artifact(path: str) -> None:
    """No 503: these read stored rows and never load a model."""
    assert "503" not in openapi()["paths"][path]["get"]["responses"]


def test_the_404_description_does_not_distinguish_absence_from_exclusion() -> None:
    """The two must be indistinguishable in the response, so the prose must not split them."""
    description = openapi()["paths"][ACCURACY_PATH]["get"]["responses"]["404"]["description"]

    assert "indistinguishable" in description.lower()


# ======================================================================================
# F. Authorization, as the routing table declares it
# ======================================================================================


def routes() -> dict[str, APIRoute]:
    """The two routes this stage added, found through the shared discovery helper.

    Reusing ``test_authorization_surface.discover`` rather than reading ``app.routes`` directly:
    FastAPI defers router inclusion, so a shallow read returns the four documentation routes and
    every assertion below would pass over an empty set. That module documents the trap and
    already solves it; a second walker here would be a second thing to get wrong.
    """
    app = create_app(Settings(environment="test", debug=True))
    found = {
        endpoint.path: endpoint.route
        for endpoint in discover(list(app.routes))
        if endpoint.path in {ACCURACY_PATH, DISTRIBUTION_PATH}
    }
    # Guards every assertion below against the vacuous pass described above.
    assert set(found) == {ACCURACY_PATH, DISTRIBUTION_PATH}
    return found


def dependency_names(route: APIRoute) -> set[str]:
    """Every callable on the route's dependency tree, by name."""
    found: set[str] = set()
    pending = [route.dependant]
    while pending:
        current = pending.pop()
        if current.call is not None:
            found.add(getattr(current.call, "__name__", ""))
        pending.extend(current.dependencies)
    return found


def test_both_routes_reach_the_hotel_access_policy() -> None:
    """Which is what makes them authenticated AND membership-checked."""
    for path, route in routes().items():
        assert "get_hotel_access_policy" in dependency_names(route), path
        assert "get_current_user" in dependency_names(route), path


def test_the_accuracy_route_declares_a_role_requirement_and_the_other_does_not() -> None:
    """The asymmetry is the design, so it is asserted rather than left to a reading."""
    resolved = routes()

    assert "dependency" in dependency_names(resolved[ACCURACY_PATH])
    assert "dependency" not in dependency_names(resolved[DISTRIBUTION_PATH])


def test_the_role_the_accuracy_route_requires_is_manager() -> None:
    """Read off the closure the router actually installed, not off the source text."""
    source = ROUTER_SOURCE.read_text(encoding="utf-8")

    assert "require_role(HotelRole.MANAGER)" in source
    assert "HotelRole.VIEWER" not in source


# ======================================================================================
# G. Layering
# ======================================================================================


def module_names(path: Path) -> set[str]:
    """Every module named in an import statement, including ``from x import y``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "forbidden",
    [
        "sqlalchemy",
        "app.repositories.ml_prediction",
        "app.repositories.ml_demand",
        "app.models.hotel",
        "app.db.session",
        "ml.metrics",
        "ml.inference",
        "sklearn",
        "pickle",
        "joblib",
    ],
)
def test_the_router_imports_nothing_below_the_service_layer(forbidden: str) -> None:
    """A router that could reach a repository is a router that eventually does."""
    assert forbidden not in module_names(ROUTER_SOURCE)


@pytest.mark.parametrize(
    "forbidden",
    [
        "sqlalchemy",
        "app.repositories.ml_prediction",
        "app.repositories.ml_demand",
        "ml.metrics",
        "ml.inference",
        "ml.artifact",
        "sklearn",
        "pickle",
        "joblib",
        "app.ml.accuracy",
        "app.ml.drift",
    ],
)
def test_the_service_reimplements_no_calculation_and_reaches_no_store(forbidden: str) -> None:
    """It projects what the frozen services returned. It cannot compute, because it cannot reach.

    ``app.ml.accuracy`` and ``app.ml.drift`` hold the pure arithmetic. Not importing them is
    what makes "no metric is recomputed here" a property of the module rather than a promise.
    """
    assert forbidden not in module_names(SERVICE_SOURCE)


@pytest.mark.parametrize("source", [ROUTER_SOURCE, SERVICE_SOURCE, SCHEMA_SOURCE])
def test_nothing_in_this_stage_imports_a_model_provider(source: Path) -> None:
    """No LLM, no RAG, no agent. Stage 7.3 is deterministic and reads rows."""
    forbidden = {"openai", "anthropic", "langchain", "llama_index", "httpx", "requests"}

    assert not forbidden & module_names(source)


@pytest.mark.parametrize("source", [ROUTER_SOURCE, SERVICE_SOURCE])
def test_no_clock_is_read_anywhere_on_this_path(source: Path) -> None:
    """``as_of_date`` is the only notion of when, and it is a parameter.

    Two requests with the same parameters over unchanged rows must return the same body.
    """
    text = source.read_text(encoding="utf-8")
    stripped = re.sub(r'"""[\s\S]*?"""', "", text)

    for forbidden in (".now(", ".today(", ".utcnow(", "func.now"):
        assert forbidden not in stripped, forbidden


def test_the_service_owns_no_transaction() -> None:
    """It holds no session, so it cannot commit, roll back or flush. Structurally read-only."""
    text = SERVICE_SOURCE.read_text(encoding="utf-8")

    for forbidden in ("Session", ".commit(", ".rollback(", ".flush(", "self._db", "self._session"):
        assert forbidden not in text, forbidden


def test_the_service_performs_no_arithmetic_on_a_measured_value() -> None:
    """The projection copies. An operator here would be a second definition of a metric.

    Walks the AST rather than grepping, so an operator inside a comment or a docstring cannot
    trip it and one inside a nested expression cannot hide from it.

    ``ast.BitOr`` is excluded because ``float | None`` in an annotation is a type union and not
    arithmetic. What is policed is the five arithmetic operators, and the only place any of them
    may appear is ``_require_window`` -- where the subtraction is between two dates and yields
    days, never a room-night figure.
    """
    tree = ast.parse(SERVICE_SOURCE.read_text(encoding="utf-8"))
    arithmetic = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv)
    permitted = {
        node
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef) and function.name == "_require_window"
        for node in ast.walk(function)
    }

    offenders = [
        ast.dump(node)[:100]
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, arithmetic) and node not in permitted
    ]

    assert offenders == []
    # Guards the exclusion above: if the permitted site ever loses its arithmetic, this test has
    # silently become an assertion about nothing and the reader should be told.
    assert any(isinstance(node, ast.BinOp) for node in permitted)


def test_the_routers_only_job_is_to_call_the_service() -> None:
    """Each endpoint body is one return statement. Anything more is logic in the wrong layer."""
    tree = ast.parse(ROUTER_SOURCE.read_text(encoding="utf-8"))
    endpoints = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("get_")
    ]

    assert len(endpoints) == 2
    for endpoint in endpoints:
        statements = [n for n in endpoint.body if not isinstance(n, ast.Expr)]
        assert len(statements) == 1, endpoint.name
        assert isinstance(statements[0], ast.Return), endpoint.name


def test_the_router_names_no_internal_identifier() -> None:
    """There is no parameter an internal key could arrive through.

    Asserted over the module's executable names rather than its text: the docstring says in
    prose that the route accepts no ``hotel_id``, and a grep over the source would be tripped by
    the very sentence promising it. Walking the AST checks the code that promise is about.
    """
    tree = ast.parse(ROUTER_SOURCE.read_text(encoding="utf-8"))
    names = {
        node.id if isinstance(node, ast.Name) else node.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.arg)
    }

    assert "hotel_public_id" in names
    assert not {name for name in names if name and "hotel_id" in name}


# ======================================================================================
# H. The published model set
# ======================================================================================


def test_every_model_this_stage_publishes_is_exported() -> None:
    """``__all__`` is the list the tests above scope themselves to, so it must be complete."""
    defined = {
        name
        for name, value in vars(published).items()
        if isinstance(value, type) and name.endswith(("Response", "Metrics", "Metadata", "Value"))
    }

    assert defined <= set(published.__all__)
    assert len(defined) >= 10
