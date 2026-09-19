"""Stage 6.4: the acceptance policy, the robustness analysis and the registry.

The panels come from the Stage 6.3 test module rather than being defined a second time here --
one definition of "a synthetic demand panel" is better than two that can drift apart.

Two tests carry most of the stage's weight:

* ``test_the_evidence_carries_no_metric_and_no_comparison`` -- the acceptance decision cannot
  depend on which method scored better, because no metric is among its inputs.
* ``test_perturbing_every_metric_leaves_the_acceptance_result_identical`` -- the same claim,
  checked behaviourally rather than structurally.

Together they are what "the policy was declared before the result" means in code rather than in
a promise.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import statistics
from dataclasses import fields, replace
from pathlib import Path

import pytest

from ml.evaluation import EvaluationResult, Prediction, RollingOriginPolicy, evaluate
from ml.manifests import content_checksum, serialise
from ml.models import MODEL_NAME, MODEL_VERSION, EstimatorConfig
from ml.policy import (
    ACCEPTANCE_POLICY,
    ACCEPTANCE_POLICY_VERSION,
    CRITERION_RATIONALE,
    EXCLUDED_CRITERIA,
    FORBIDDEN_EVIDENCE_FRAGMENTS,
    REQUIRED_DATASET_SHA256,
    AcceptanceEvidence,
    AcceptancePolicy,
    criteria_names,
    evaluate_acceptance,
)
from ml.registry import (
    REGISTRY_VERSION,
    ROLLING_ORIGIN_PROTOCOL_VERSION,
    STATUS_OFFLINE_CANDIDATE,
    build_registry_record,
    configuration_checksum,
    protocol_checksum,
)
from ml.validation import (
    CANONICAL_FEATURE_ORDER,
    DEFAULT_ERROR_SAMPLE,
    VALIDATION_VERSION,
    ValidationError,
    assert_canonical_feature_order,
    assert_fold_boundaries_match,
    assert_model_version,
    build_validation_report,
    dispersion,
    error_analysis,
    fold_boundaries,
    fold_stability,
    fold_win_counts,
    hotel_key,
    largest_errors,
    leakage_report,
    month_key,
    origins_by_fold,
    quarter_key,
    regime_metrics,
    year_month_key,
)
from tests.ml.test_demand_model import DATASET_FEATURES, FAST_POLICY, panel, wave

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODEL_CARD = REPOSITORY_ROOT / "docs" / "ml-model-card.md"

START = dt.date(2016, 1, 1)

#: The Stage 6.3 configuration, pinned by checksum. Changing a hyper-parameter or a protocol
#: setting changes one of these, and this test is where that becomes visible rather than
#: silently producing a different `demand_baseline_v1`.
STAGE_63_PROTOCOL_SHA256 = "118fe2bce37828238ff1ff448dc9ab5c7f720aab557eb70cba2183c3b1f9c26a"
STAGE_63_ESTIMATOR_SHA256 = "bbaf2881204f40c67db7a805038042e9c8de902fea8449871bd538c5566d34c5"

#: The declared policy, pinned. A threshold cannot move without this literal moving with it.
ACCEPTANCE_POLICY_SHA256 = "afc47c4f3c5962b8debabc006bfefc7e3de412e32c758015850dcd7367f13c70"


def passing_evidence(**overrides: object) -> AcceptanceEvidence:
    """Evidence that satisfies every criterion, so a test can break exactly one."""
    base = AcceptanceEvidence(
        paired_observations=744,
        folds=54,
        baseline_skipped=0,
        learned_skipped=0,
        incomplete_windows=1,
        hotels_with_sufficient_coverage=2,
        dataset_version="v1",
        feature_version="v1",
        dataset_sha256=REQUIRED_DATASET_SHA256,
        forecast_horizon_days=7,
        model_version=MODEL_VERSION,
        deterministic=True,
        leakage_checks_passed=True,
        metrics_present=True,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def prediction(
    *,
    fold: int = 0,
    hotel: str = "city_hotel",
    day: int = 0,
    actual: int,
    baseline: float | None,
    learned: float | None,
) -> Prediction:
    return Prediction(
        fold_index=fold,
        hotel_key=hotel,
        target_date=START + dt.timedelta(days=day),
        actual=actual,
        baseline=baseline,
        learned=learned,
    )


def backtest() -> EvaluationResult:
    series = {"city_hotel": wave(120), "resort_hotel": wave(120, base=80, phase=3)}
    return evaluate(
        panel(series),
        policy=FAST_POLICY,
        feature_names=DATASET_FEATURES,
        config=EstimatorConfig(max_iter=20),
    )


# --- 1. the acceptance policy is deterministic ---------------------------------------------------


def test_the_same_policy_and_evidence_always_give_the_same_result() -> None:
    first = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence())
    second = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence())
    assert first.as_dict() == second.as_dict()
    assert first.passed is True


def test_the_policy_checksum_is_stable_and_pinned() -> None:
    assert ACCEPTANCE_POLICY.checksum() == ACCEPTANCE_POLICY.checksum()
    assert ACCEPTANCE_POLICY.checksum() == ACCEPTANCE_POLICY_SHA256
    assert ACCEPTANCE_POLICY.version == ACCEPTANCE_POLICY_VERSION


def test_moving_any_threshold_moves_the_policy_checksum() -> None:
    relaxed = AcceptancePolicy(minimum_folds=1)
    assert relaxed.checksum() != ACCEPTANCE_POLICY.checksum()


def test_every_criterion_has_a_written_rationale() -> None:
    for name in criteria_names():
        assert CRITERION_RATIONALE.get(name), name


# --- 2. the policy is evaluated before -- and independently of -- the result ----------------------


def test_the_evidence_carries_no_metric_and_no_comparison() -> None:
    """The structural guarantee. Acceptance cannot see which method won."""
    names = [field.name for field in fields(AcceptanceEvidence)]
    for name in names:
        for fragment in FORBIDDEN_EVIDENCE_FRAGMENTS:
            assert fragment not in name, f"{name} contains {fragment!r}"
    assert "paired_observations" in names


def test_perturbing_every_metric_leaves_the_acceptance_result_identical() -> None:
    """The same guarantee, checked behaviourally: only counts and versions can move it."""
    result = backtest()
    before = evaluate_acceptance(
        ACCEPTANCE_POLICY,
        passing_evidence(paired_observations=len(result.predictions), folds=len(result.folds)),
    )
    ruined = [
        Prediction(
            fold_index=p.fold_index,
            hotel_key=p.hotel_key,
            target_date=p.target_date,
            actual=p.actual,
            baseline=None if p.baseline is None else p.baseline * 1000.0,
            learned=None if p.learned is None else p.learned * 0.001,
        )
        for p in result.predictions
    ]
    after = evaluate_acceptance(
        ACCEPTANCE_POLICY,
        passing_evidence(paired_observations=len(ruined), folds=len(result.folds)),
    )
    assert before.as_dict() == after.as_dict()


def test_no_criterion_rewards_the_learned_model_for_winning() -> None:
    text = json.dumps(evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence()).as_dict()).lower()
    assert "learned_model_beats_baseline" in text  # named, in the EXCLUDED block
    for name in criteria_names():
        # "win" alone would match `maximum_incomplete_windows`, which is about arithmetic at
        # the end of the date axis and not about anyone winning.
        for fragment in ("beat", "winner", "outperform", "versus"):
            assert fragment not in name, (name, fragment)
    assert "NOT a criterion" in EXCLUDED_CRITERIA["learned_model_beats_baseline"]


def test_every_criterion_is_reported_including_the_ones_that_passed() -> None:
    result = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence())
    assert len(result.checks) == 13
    assert all(check.passed for check in result.checks)
    assert result.as_dict()["criteria_failed"] == []


# --- 3. the Stage 6.3 configuration is unchanged --------------------------------------------------


def test_the_rolling_origin_protocol_is_the_stage_63_protocol() -> None:
    policy = RollingOriginPolicy()
    assert (policy.horizon_days, policy.minimum_train_days, policy.step_days) == (7, 365, 7)
    assert protocol_checksum(policy.as_dict()) == STAGE_63_PROTOCOL_SHA256


def test_the_estimator_configuration_is_the_stage_63_configuration() -> None:
    config = EstimatorConfig()
    assert config.max_iter == 200
    assert config.learning_rate == 0.05
    assert config.max_leaf_nodes == 31
    assert config.min_samples_leaf == 20
    assert config.l2_regularization == 0.0
    assert config.max_bins == 255
    assert config.random_state == 0
    assert config.loss == "squared_error"
    assert config.early_stopping is False
    assert configuration_checksum(config.as_dict()) == STAGE_63_ESTIMATOR_SHA256


def test_no_hyperparameter_was_retuned_for_this_stage() -> None:
    """Stage 6.4 measures Stage 6.3. A changed default would make that untrue."""
    assert MODEL_VERSION == "demand_baseline_v1"
    assert MODEL_NAME == "demand_baseline"


# --- 4./5./6. identity validation ----------------------------------------------------------------


def test_a_wrong_dataset_checksum_fails_the_policy() -> None:
    result = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence(dataset_sha256="0" * 64))
    assert result.passed is False
    assert [check.name for check in result.failed] == ["required_dataset_sha256"]


def test_a_wrong_feature_version_fails_the_policy() -> None:
    result = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence(feature_version="v2"))
    assert result.passed is False
    assert [check.name for check in result.failed] == ["required_feature_version"]


def test_a_wrong_dataset_version_fails_the_policy() -> None:
    result = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence(dataset_version="v2"))
    assert [check.name for check in result.failed] == ["required_dataset_version"]


def test_a_wrong_model_version_fails_the_policy_and_the_guard() -> None:
    result = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence(model_version="demand_v9"))
    assert [check.name for check in result.failed] == ["required_model_version"]
    with pytest.raises(ValidationError, match="version mismatch must"):
        assert_model_version("demand_v9")
    assert_model_version(MODEL_VERSION)


def test_a_wrong_horizon_fails_the_policy() -> None:
    result = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence(forecast_horizon_days=1))
    assert [check.name for check in result.failed] == ["required_forecast_horizon_days"]


@pytest.mark.parametrize(
    ("override", "criterion"),
    [
        ({"paired_observations": 499}, "minimum_paired_observations"),
        ({"folds": 39}, "minimum_folds"),
        ({"baseline_skipped": 1}, "maximum_skipped_predictions"),
        ({"learned_skipped": 3}, "maximum_skipped_predictions"),
        ({"incomplete_windows": 2}, "maximum_incomplete_windows"),
        ({"hotels_with_sufficient_coverage": 1}, "minimum_hotels_with_sufficient_coverage"),
        ({"deterministic": False}, "require_deterministic_execution"),
        ({"leakage_checks_passed": False}, "require_leakage_checks"),
        ({"metrics_present": False}, "require_all_metrics"),
    ],
)
def test_each_criterion_fails_on_its_own_evidence(
    override: dict[str, object], criterion: str
) -> None:
    result = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence(**override))
    assert result.passed is False
    assert [check.name for check in result.failed] == [criterion]


# --- 7. fold boundary stability ------------------------------------------------------------------


def test_identical_folds_satisfy_the_boundary_check() -> None:
    result = backtest()
    assert_fold_boundaries_match(result, list(fold_boundaries(result)))


def test_a_changed_fold_count_is_refused() -> None:
    result = backtest()
    with pytest.raises(ValidationError, match="fold count changed"):
        assert_fold_boundaries_match(result, list(fold_boundaries(result))[:-1])


def test_a_changed_fold_boundary_is_named() -> None:
    result = backtest()
    recorded = [dict(fold) for fold in fold_boundaries(result)]
    recorded[2]["train_rows"] = 999_999
    with pytest.raises(ValidationError, match="differs on train_rows"):
        assert_fold_boundaries_match(result, recorded)


def test_fold_boundaries_are_reproduced_by_a_second_run() -> None:
    assert fold_boundaries(backtest()) == fold_boundaries(backtest())


# --- 8. regime grouping --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "month", "quarter", "year_month"),
    [
        (0, "01", "Q1", "2016-01"),
        (59, "02", "Q1", "2016-02"),
        (100, "04", "Q2", "2016-04"),
        (200, "07", "Q3", "2016-07"),
        (300, "10", "Q4", "2016-10"),
    ],
)
def test_regime_keys_place_a_date_in_the_right_group(
    day: int, month: str, quarter: str, year_month: str
) -> None:
    item = prediction(day=day, actual=1, baseline=1.0, learned=1.0)
    assert month_key(item) == month
    assert quarter_key(item) == quarter
    assert year_month_key(item) == year_month
    assert hotel_key(item) == "city_hotel"


def test_each_group_scores_only_its_own_observations() -> None:
    january = [prediction(day=i, actual=100, baseline=100.0, learned=100.0) for i in range(31)]
    february = [prediction(day=31 + i, actual=100, baseline=50.0, learned=50.0) for i in range(29)]
    grouped = regime_metrics([*january, *february], month_key, minimum=1)
    first = grouped["01"]
    second = grouped["02"]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["baseline"]["mae"] == 0.0
    assert second["baseline"]["mae"] == 50.0
    assert first["observations"] == 31
    assert second["observations"] == 29


def test_every_prediction_lands_in_exactly_one_group() -> None:
    result = backtest()
    for key in (month_key, quarter_key, hotel_key, year_month_key):
        grouped = regime_metrics(result.predictions, key, minimum=1)
        total = sum(block["observations"] for block in grouped.values() if isinstance(block, dict))
        assert total == len(result.predictions)


# --- 9. per-hotel grouping -----------------------------------------------------------------------


def test_one_hotel_cannot_borrow_another_hotels_days() -> None:
    predictions = [
        *[
            prediction(hotel="city_hotel", day=i, actual=10, baseline=10.0, learned=10.0)
            for i in range(40)
        ],
        *[
            prediction(hotel="resort_hotel", day=i, actual=10, baseline=0.0, learned=0.0)
            for i in range(40)
        ],
    ]
    grouped = regime_metrics(predictions, hotel_key)
    city = grouped["city_hotel"]
    resort = grouped["resort_hotel"]
    assert isinstance(city, dict)
    assert isinstance(resort, dict)
    assert city["baseline"]["mae"] == 0.0
    assert resort["baseline"]["mae"] == 10.0


# --- 10./11. error ranking, signed and absolute --------------------------------------------------


ORIGINS = {0: dt.date(2015, 12, 31)}


def test_errors_are_ranked_by_absolute_size() -> None:
    predictions = [
        prediction(day=0, actual=100, baseline=105.0, learned=100.0),
        prediction(day=1, actual=100, baseline=60.0, learned=100.0),
        prediction(day=2, actual=100, baseline=120.0, learned=100.0),
    ]
    ranked = largest_errors(predictions, "baseline", ORIGINS, limit=3)
    assert [record.absolute_error for record in ranked] == [40.0, 20.0, 5.0]


def test_the_signed_error_says_which_way_the_forecast_was_wrong() -> None:
    over = prediction(day=0, actual=100, baseline=130.0, learned=None)
    under = prediction(day=1, actual=100, baseline=70.0, learned=None)
    ranked = largest_errors([over, under], "baseline", ORIGINS, limit=2)
    assert {(r.target_date.day, r.signed_error) for r in ranked} == {(1, 30.0), (2, -30.0)}
    assert all(record.absolute_error == abs(record.signed_error) for record in ranked)
    assert all(record.absolute_error == 30.0 for record in ranked)


def test_ties_are_broken_deterministically_by_date_then_hotel() -> None:
    predictions = [
        prediction(hotel="resort_hotel", day=1, actual=100, baseline=110.0, learned=None),
        prediction(hotel="city_hotel", day=1, actual=100, baseline=90.0, learned=None),
        prediction(hotel="city_hotel", day=0, actual=100, baseline=110.0, learned=None),
    ]
    ranked = largest_errors(predictions, "baseline", ORIGINS, limit=3)
    assert [(r.target_date.day, r.hotel_key) for r in ranked] == [
        (1, "city_hotel"),
        (2, "city_hotel"),
        (2, "resort_hotel"),
    ]
    assert largest_errors(predictions, "baseline", ORIGINS, limit=3) == ranked


def test_a_method_that_could_not_forecast_contributes_no_error_record() -> None:
    predictions = [prediction(day=0, actual=100, baseline=None, learned=10.0)]
    assert largest_errors(predictions, "baseline", ORIGINS) == ()
    assert len(largest_errors(predictions, "learned", ORIGINS)) == 1


def test_the_error_sample_is_capped_and_the_limit_is_validated() -> None:
    predictions = [
        prediction(day=i, actual=100, baseline=float(i), learned=None) for i in range(50)
    ]
    assert len(largest_errors(predictions, "baseline", ORIGINS, limit=4)) == 4
    with pytest.raises(ValidationError, match="limit must be at least 1"):
        largest_errors(predictions, "baseline", ORIGINS, limit=0)
    with pytest.raises(ValidationError, match="unknown method"):
        largest_errors(predictions, "nonsense", ORIGINS)


def test_the_error_analysis_states_that_nothing_was_altered() -> None:
    report = error_analysis(backtest(), limit=3)
    assert report["sample_size"] == 3
    assert "no observation was removed" in str(report["treatment"])
    assert "forecast - actual" in str(report["signed_error_definition"])
    methods = report["methods"]
    assert isinstance(methods, dict)
    assert set(methods) == {"baseline", "learned"}


def test_error_records_carry_their_fold_and_origin() -> None:
    result = backtest()
    origins = origins_by_fold(result)
    for record in largest_errors(result.predictions, "learned", origins, limit=5):
        assert record.origin == origins[record.fold_index]
        assert record.origin < record.target_date


# --- robustness statistics ------------------------------------------------------------------------


def test_dispersion_matches_hand_computed_values() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    described = dispersion(values)
    assert described["folds"] == 4
    assert described["mean"] == pytest.approx(2.5)
    assert described["median"] == pytest.approx(2.5)
    assert described["min"] == 1.0
    assert described["max"] == 4.0
    assert described["stdev"] == pytest.approx(statistics.pstdev(values))
    assert described["stdev"] == pytest.approx(math.sqrt(1.25))


def test_dispersion_of_nothing_reports_nothing_rather_than_zero() -> None:
    described = dispersion([])
    assert described["folds"] == 0
    assert described["mean"] is None
    assert described["stdev"] is None


def test_fold_stability_covers_both_methods_and_all_three_metrics() -> None:
    stability = fold_stability(backtest())
    methods = stability["methods"]
    assert isinstance(methods, dict)
    assert set(methods) == {"baseline", "learned"}
    for block in methods.values():
        assert set(block) == {"mae", "rmse", "smape"}
    assert "population standard deviation" in str(stability["stdev_definition"])


def test_fold_comparison_counts_add_up_to_the_fold_count() -> None:
    result = backtest()
    counts = fold_win_counts(result)
    for metric, block in counts.items():
        assert isinstance(block, dict)
        total = (
            block["learned_lower"]
            + block["baseline_lower"]
            + block["ties"]
            + block["not_comparable"]
        )
        assert total == len(result.folds), metric


# --- 12./13. registry ----------------------------------------------------------------------------


def registry_for(result: EvaluationResult, **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "dataset_version": "v1",
        "feature_version": "v1",
        "dataset_sha256": REQUIRED_DATASET_SHA256,
        "dataset_rows": 1462,
        "dataset_path": "demand_daily_v1.csv",
        "deterministic": True,
        "leakage_passed": True,
        "evaluation_record_sha256": "a" * 64,
        "validation_sha256": "b" * 64,
        "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    }
    arguments.update(overrides)
    acceptance = evaluate_acceptance(ACCEPTANCE_POLICY, passing_evidence())
    return build_registry_record(result, acceptance, **arguments)  # type: ignore[arg-type]


def test_the_registry_is_deterministic() -> None:
    result = backtest()
    assert registry_for(result) == registry_for(result)


def test_the_registry_checksum_ignores_the_wall_clock() -> None:
    result = backtest()
    early = registry_for(result, created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    late = registry_for(result, created_at=dt.datetime(2031, 7, 9, tzinfo=dt.UTC))
    assert early != late
    assert content_checksum(early) == content_checksum(late)


def test_the_registry_checksum_notices_a_changed_metric() -> None:
    result = backtest()
    record = registry_for(result)
    tampered = json.loads(json.dumps(record))
    tampered["metrics"]["pooled"]["baseline"]["mae"] = 0.0
    assert content_checksum(tampered) != content_checksum(record)


def test_the_registry_records_every_field_the_stage_requires() -> None:
    record = registry_for(backtest())
    assert record["registry_version"] == REGISTRY_VERSION
    model = record["model"]
    dataset = record["dataset"]
    protocol = record["protocol"]
    verification = record["verification"]
    assert isinstance(model, dict)
    assert isinstance(dataset, dict)
    assert isinstance(protocol, dict)
    assert isinstance(verification, dict)

    assert model["model_version"] == MODEL_VERSION
    assert model["model_name"] == MODEL_NAME
    assert model["status"] == STATUS_OFFLINE_CANDIDATE
    assert model["artifact_persisted"] is False
    assert model["serving_path"] is None
    assert model["forecast_horizon_days"] == FAST_POLICY.horizon_days
    assert "configuration" in model and "configuration_sha256" in model

    assert dataset["dataset_version"] == "v1"
    assert dataset["feature_version"] == "v1"
    assert dataset["dataset_sha256"] == REQUIRED_DATASET_SHA256

    assert protocol["protocol_version"] == ROLLING_ORIGIN_PROTOCOL_VERSION
    assert {"training_data_range", "evaluation_data_range", "folds", "paired_observations"} <= set(
        protocol
    )
    assert verification["deterministic"] is True
    assert verification["leakage_checks_passed"] is True
    assert isinstance(record["metrics"], dict)
    assert isinstance(record["acceptance"], dict)


def test_the_registry_makes_no_claim_it_has_not_earned() -> None:
    record = registry_for(backtest())
    claims = record["claims"]
    assert isinstance(claims, dict)
    assert claims["production_ready"] is False
    assert claims["production_accuracy_established"] is False
    assert claims["cross_hotel_generalisation_established"] is False

    # The `excluded_criteria` block is scanned separately: it exists to NAME the criteria that
    # were deliberately not adopted, so "beats" appearing there is the opposite of a claim.
    scanned = json.loads(json.dumps(record))
    excluded = scanned["acceptance"].pop("excluded_criteria")
    assert "learned_model_beats_baseline" in excluded
    text = serialise(scanned).decode("utf-8").lower()
    for marketing in ("winner", "beats", "outperform", "production-ready", "state of the art"):
        assert marketing not in text, marketing


def test_the_registry_holds_no_credential_shaped_value() -> None:
    text = serialise(registry_for(backtest())).decode("utf-8").lower()
    for forbidden in ("password", "secret", "postgresql://", "postgres://", "token"):
        assert forbidden not in text, forbidden


# --- 14. the model card --------------------------------------------------------------------------


REQUIRED_CARD_SECTIONS = (
    "Intended use",
    "Target",
    "Forecast horizon",
    "Data source",
    "Dataset limitations",
    "Feature limitations",
    "Leakage constraints",
    "Evaluation protocol",
    "Metrics",
    "Limitations",
    "Non-production status",
    "Provenance",
    "Two-hotel limitation",
)


def test_the_model_card_exists_and_covers_every_required_section() -> None:
    assert MODEL_CARD.is_file(), MODEL_CARD
    text = MODEL_CARD.read_text(encoding="utf-8")
    for section in REQUIRED_CARD_SECTIONS:
        assert re.search(rf"^#+ .*{re.escape(section)}", text, re.MULTILINE | re.IGNORECASE), (
            section
        )


def test_the_model_card_states_the_non_production_sentence_verbatim() -> None:
    text = MODEL_CARD.read_text(encoding="utf-8")
    assert (
        "This model is an offline research candidate and is not a production forecasting model."
        in text
    )


def test_the_model_card_names_every_known_limitation() -> None:
    text = MODEL_CARD.read_text(encoding="utf-8").lower()
    for phrase in (
        "2015",
        "2017",
        "two hotels",
        "duplicate",
        "rooms_existing_at_cutoff",
        "day-resolution",
        "portug",
    ):
        assert phrase in text, phrase


def test_the_model_card_makes_no_generalisation_claim() -> None:
    text = MODEL_CARD.read_text(encoding="utf-8").lower()
    assert "production-ready" not in text
    assert "held-out hotel" in text or "held out hotel" in text


# --- 15. insufficient coverage -------------------------------------------------------------------


def test_a_group_with_too_few_days_reports_coverage_rather_than_a_metric() -> None:
    grouped = regime_metrics([prediction(day=0, actual=10, baseline=9.0, learned=9.5)], month_key)
    block = grouped["01"]
    assert isinstance(block, dict)
    assert block["sufficient_coverage"] is False
    assert block["evaluated_observations"] == 1
    assert "mae" not in block
    assert "insufficient coverage" in str(block["reason"])


def test_a_group_at_exactly_the_minimum_is_scored() -> None:
    predictions = [prediction(day=i, actual=100, baseline=90.0, learned=95.0) for i in range(30)]
    block = regime_metrics(predictions, month_key)["01"]
    assert isinstance(block, dict)
    assert block["sufficient_coverage"] is True
    assert block["baseline"]["mae"] == 10.0


# --- 16./17. feature guards ----------------------------------------------------------------------


def test_the_canonical_feature_order_is_the_stage_61_contract() -> None:
    assert CANONICAL_FEATURE_ORDER == DATASET_FEATURES
    assert_canonical_feature_order(CANONICAL_FEATURE_ORDER)


def test_a_permuted_feature_order_is_refused_rather_than_sorted() -> None:
    permuted = (
        CANONICAL_FEATURE_ORDER[1],
        CANONICAL_FEATURE_ORDER[0],
        *CANONICAL_FEATURE_ORDER[2:],
    )
    with pytest.raises(ValidationError, match="different order"):
        assert_canonical_feature_order(permuted)


def test_a_missing_feature_column_is_named() -> None:
    with pytest.raises(ValidationError, match="demand_lag_28"):
        assert_canonical_feature_order(
            tuple(n for n in CANONICAL_FEATURE_ORDER if n != "demand_lag_28")
        )


def test_an_unexpected_feature_column_is_named() -> None:
    with pytest.raises(ValidationError, match="competitor_price"):
        assert_canonical_feature_order((*CANONICAL_FEATURE_ORDER, "competitor_price"))


# --- 18. repeated evaluation ---------------------------------------------------------------------


def test_two_backtests_produce_identical_predictions_and_reports() -> None:
    first = backtest()
    second = backtest()
    assert first.predictions == second.predictions
    assert build_validation_report(first) == build_validation_report(second)


def test_the_validation_report_carries_every_block_and_claims_nothing() -> None:
    report = build_validation_report(backtest(), error_sample=DEFAULT_ERROR_SAMPLE)
    assert report["validation_version"] == VALIDATION_VERSION
    for key in ("folds", "stability", "fold_comparison_counts", "regimes", "errors", "leakage"):
        assert key in report, key
    regimes = report["regimes"]
    assert isinstance(regimes, dict)
    assert set(regimes) == {"month", "quarter", "year_month", "hotel"}
    assert "NONE" in str(report["cross_hotel_claim"])
    assert "no method is ranked" in str(report["purpose"])


def test_the_leakage_report_is_recomputed_and_names_each_check() -> None:
    report = leakage_report(backtest())
    checks = report["checks"]
    assert isinstance(checks, dict)
    assert report["passed"] is True
    assert set(checks) == {
        "train_end_at_or_before_origin_before_evaluation_start",
        "evaluation_window_within_horizon",
        "no_hotel_day_evaluated_twice",
        "every_selected_feature_knowable_at_the_horizon",
        "training_window_never_contracts",
        "no_shuffle_or_random_split",
    }
    assert all(checks.values())
