"""Stage 7.14 -- multi-horizon forecasting, offline: the rules, the datasets and the records.

Four groups, in the order they matter:

1. **V1 is untouched.** The one change to shipped code is a defaulted ``dataset_horizon_days``
   parameter; with the default it must answer exactly what Stage 6.3/6.4 answered, and every V1
   pin must still hold.
2. **No horizon can see its future.** Lead times follow the dataset's own cutoff; lags shorter
   than the horizon are refused; and the leakage tests mutate the future at every horizon and
   require the past to come back identical.
3. **The datasets and the protocol are the frozen ones.** Digests, column orders, floors and
   statements are pinned; ``multi_horizon_v1`` and ``acceptance_v2`` hash to their literals.
4. **The committed records say only what was measured.** Acceptance is metric-blind, every claim
   is false, forecasts are uncapped, and nothing ranks the horizons.

Re-measuring the committed records end to end is in ``test_multi_horizon_integration.py``.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import json
from pathlib import Path

import pytest

from app.ml.dataset import DatasetContractError, build_feature_specs
from app.ml.serving import APPROVED_MODEL
from ml.evaluation import RollingOriginPolicy
from ml.horizons import (
    ACCEPTANCE_POLICY_VERSION,
    CAPACITY_STATEMENT,
    CLAIMS,
    COMPARISON_RULE,
    HORIZONS,
    ON_BOOKS_STATEMENT,
    PROTOCOL_SHA256,
    PROTOCOL_VERSION,
    HorizonError,
    HorizonSpec,
    acceptance_policy,
    acceptance_v2_digest,
    assert_dataset_identity,
    assert_protocol_frozen,
    baseline_lag_for,
    build_horizon_dataset,
    evaluate_acceptance_v2,
    probe_matrix,
    protocol_digest,
    protocol_settings,
    side_by_side,
    spec_for,
)
from ml.loading import load_dataset_manifest, load_processed_dataset
from ml.manifests import content_checksum
from ml.models import (
    CALENDAR_FEATURES,
    EstimatorConfig,
    ModelContractError,
    feature_lead_days,
    select_model_features,
)
from ml.pipelines.offline_demand import OfflineDataset, build_offline_dataset, on_books_by_date
from ml.policy import (
    ACCEPTANCE_POLICY,
    FORBIDDEN_EVIDENCE_FRAGMENTS,
    REQUIRED_DATASET_SHA256,
    AcceptanceEvidence,
)
from ml.registry import configuration_checksum, protocol_checksum
from tests.backend.test_production_packaging import ml_import_closure
from tests.ml.test_demand_model_validation import (
    ACCEPTANCE_POLICY_SHA256,
    STAGE_63_ESTIMATOR_SHA256,
    STAGE_63_PROTOCOL_SHA256,
)
from tests.ml.test_offline_demand import BASE, booking, fixture_source, parse, spread

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: The frozen protocol and policy, pinned. Neither may move without these literals moving.
MULTI_HORIZON_PROTOCOL_SHA256 = "d8408e188e74867a99ad5eca24540e22636c3084089a84f5b2bf9a9970a0fdaf"
ACCEPTANCE_V2_SHA256 = "22d127e703732f3a99bf2f509e5ac74535bbb1c5b092c586351c9db2bf946ad5"

#: The served model's identity, as Stage 6.7 approved it.
V1_CANONICAL_DIGEST = "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"

V1_FEATURES = tuple(spec.name for spec in build_feature_specs())
V1_SEVEN_DAY_SELECTION = (*CALENDAR_FEATURES, "demand_lag_7", "demand_lag_14", "demand_lag_28")

EXPECTED_SELECTED = {
    7: (
        *CALENDAR_FEATURES,
        "demand_lag_7",
        "demand_lag_14",
        "demand_lag_28",
        "demand_rolling_mean_7",
        "demand_rolling_mean_14",
        "demand_rolling_mean_28",
        "on_books_room_nights_at_cutoff",
    ),
    14: (
        *CALENDAR_FEATURES,
        "demand_lag_14",
        "demand_lag_28",
        "demand_rolling_mean_7",
        "demand_rolling_mean_14",
        "demand_rolling_mean_28",
        "on_books_room_nights_at_cutoff",
    ),
    28: (
        *CALENDAR_FEATURES,
        "demand_lag_28",
        "demand_rolling_mean_7",
        "demand_rolling_mean_14",
        "demand_rolling_mean_28",
        "on_books_room_nights_at_cutoff",
    ),
}

RECORDS = ("metrics.json", "validation.json", "registry.json", "artifact.json")


def horizon_ids(spec: HorizonSpec) -> str:
    return f"h{spec.horizon_days}"


def record(spec: HorizonSpec, name: str) -> dict[str, object]:
    return json.loads((spec.model_directory / name).read_text(encoding="utf-8"))


def build_at(spec: HorizonSpec, rows: list[dict[str, str]]) -> OfflineDataset:
    """A synthetic source built exactly as this horizon's dataset is."""
    return build_offline_dataset(
        parse(rows),
        horizon_days=spec.horizon_days,
        lag_days=spec.lag_days,
        rolling_windows=spec.rolling_windows,
    )


# --- 1. V1 is untouched ------------------------------------------------------------------------


@pytest.mark.parametrize("name", V1_FEATURES)
def test_the_default_dataset_horizon_is_stage_63_for_every_v1_feature(name: str) -> None:
    assert feature_lead_days(name) == feature_lead_days(name, dataset_horizon_days=1)


def test_stage_63_lead_times_are_unchanged_by_default() -> None:
    assert feature_lead_days("demand_rolling_mean_7") == 1
    assert feature_lead_days("on_books_room_nights_at_cutoff") == 1
    assert feature_lead_days("demand_lag_1") == 1
    assert feature_lead_days("demand_lag_28") == 28
    assert feature_lead_days("day_of_week") is None


def test_the_v1_seven_day_selection_is_still_the_nine_features() -> None:
    by_default = select_model_features(V1_FEATURES, horizon_days=7)
    explicit = select_model_features(V1_FEATURES, horizon_days=7, dataset_horizon_days=1)
    assert by_default == explicit
    assert by_default.selected == V1_SEVEN_DAY_SELECTION
    # The record shape Stage 6.3 committed does not gain a field.
    assert set(by_default.as_dict()) == {"horizon_days", "selected", "excluded"}


def test_every_v1_pin_still_holds() -> None:
    assert protocol_checksum(RollingOriginPolicy().as_dict()) == STAGE_63_PROTOCOL_SHA256
    assert configuration_checksum(EstimatorConfig().as_dict()) == STAGE_63_ESTIMATOR_SHA256
    assert ACCEPTANCE_POLICY.checksum() == ACCEPTANCE_POLICY_SHA256
    assert ACCEPTANCE_POLICY.version == "acceptance_v1"
    assert ACCEPTANCE_POLICY.required_forecast_horizon_days == 7
    assert REQUIRED_DATASET_SHA256 == (
        "904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d"
    )


def test_the_served_model_is_still_the_approved_v1() -> None:
    assert APPROVED_MODEL.model_version == "demand_baseline_v1"
    assert APPROVED_MODEL.canonical_model_digest == V1_CANONICAL_DIGEST
    assert APPROVED_MODEL.forecast_horizon_days == 7
    assert APPROVED_MODEL.feature_columns == V1_SEVEN_DAY_SELECTION
    assert APPROVED_MODEL.dataset_sha256 == REQUIRED_DATASET_SHA256


def test_the_v1_dataset_is_untouched() -> None:
    dataset = load_processed_dataset(REPOSITORY_ROOT / "ml/data/processed/demand_daily_v1.csv")
    assert dataset.sha256 == REQUIRED_DATASET_SHA256


def test_nothing_stage_714_added_ships_in_the_image() -> None:
    closure = ml_import_closure()
    for offline_only in (
        "ml/horizons.py",
        "ml/pipelines/build_horizon_datasets.py",
        "ml/pipelines/evaluate_horizons.py",
    ):
        assert offline_only not in closure
    # And no shipped module reaches it, directly or otherwise.
    for path in closure:
        text = (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
        assert "ml.horizons" not in text, path


def test_a_dataset_horizon_below_one_day_is_refused() -> None:
    with pytest.raises(ModelContractError):
        feature_lead_days("demand_rolling_mean_7", dataset_horizon_days=0)


# --- 2. no horizon can see its future ---------------------------------------------------------


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_cutoff_features_are_knowable_at_the_datasets_own_cutoff(spec: HorizonSpec) -> None:
    h = spec.horizon_days
    for name in (
        "demand_rolling_mean_7",
        "demand_rolling_mean_14",
        "demand_rolling_mean_28",
        "on_books_room_nights_at_cutoff",
        "rooms_existing_at_cutoff",
    ):
        assert feature_lead_days(name, dataset_horizon_days=h) == h
    for lag in (1, 7, 14, 28):
        # A lag is relative to the target date, whatever the dataset's horizon.
        assert feature_lead_days(f"demand_lag_{lag}", dataset_horizon_days=h) == lag


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_each_horizon_selects_exactly_its_admissible_features(spec: HorizonSpec) -> None:
    selection = select_model_features(
        spec.feature_order, horizon_days=spec.horizon_days, dataset_horizon_days=spec.horizon_days
    )
    assert selection.selected == EXPECTED_SELECTED[spec.horizon_days]
    excluded = dict(selection.excluded)
    assert set(excluded) == {"rooms_existing_at_cutoff"}
    assert "room inventory" in excluded["rooms_existing_at_cutoff"]
    for name in selection.selected:
        lead = feature_lead_days(name, dataset_horizon_days=spec.horizon_days)
        assert lead is None or lead >= spec.horizon_days, name


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_a_one_day_dataset_would_not_admit_the_cutoff_features(spec: HorizonSpec) -> None:
    """The reason the horizon-matched datasets exist: at the Stage 6.2 cutoff they are refused."""
    selection = select_model_features(spec.feature_order, horizon_days=spec.horizon_days)
    assert "on_books_room_nights_at_cutoff" not in selection.selected
    assert not any(name.startswith("demand_rolling_mean") for name in selection.selected)


@pytest.mark.parametrize(
    ("horizon", "short_lags"),
    [
        (7, (1,)),
        (7, (6, 7)),
        (14, (7, 14)),
        (14, (13, 14)),
        (28, (14, 28)),
        (28, (27, 28)),
    ],
)
def test_a_lag_shorter_than_the_horizon_is_rejected(
    horizon: int, short_lags: tuple[int, ...]
) -> None:
    with pytest.raises(DatasetContractError):
        build_offline_dataset(parse(spread(90)), horizon_days=horizon, lag_days=short_lags)


def test_a_spec_with_a_lag_shorter_than_its_horizon_is_refused() -> None:
    with pytest.raises(HorizonError):
        dataclasses.replace(spec_for(14), lag_days=(7, 14, 28))


def test_a_spec_with_the_wrong_baseline_is_refused() -> None:
    with pytest.raises(HorizonError):
        dataclasses.replace(spec_for(14), baseline_lag_days=28)


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_the_future_after_the_cutoff_never_reaches_a_rows_features(spec: HorizonSpec) -> None:
    """Flood every day after the cutoff with same-day bookings; the row must not notice."""
    h = spec.horizon_days
    quiet = spread(150)
    target = BASE + dt.timedelta(days=120)
    cutoff = target - dt.timedelta(days=h)
    after_cutoff = [
        booking(arrival=cutoff + dt.timedelta(days=offset), nights=1, lead_time=0)
        for offset in range(1, h + 1)
        for _ in range(333)
    ]
    before = build_at(spec, quiet)
    after = build_at(spec, quiet + after_cutoff)

    row_before = next(row for row in before.rows if row.target_date == target)
    row_after = next(row for row in after.rows if row.target_date == target)
    assert row_after.target_room_nights == row_before.target_room_nights + 333
    assert row_after.features == row_before.features
    assert row_after.prediction_cutoff == row_before.prediction_cutoff


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_the_last_known_day_does_reach_the_features(spec: HorizonSpec) -> None:
    """The positive control: demand ON the cutoff day is known, so the rolling mean moves."""
    h = spec.horizon_days
    quiet = spread(150)
    target = BASE + dt.timedelta(days=120)
    cutoff = target - dt.timedelta(days=h)
    on_cutoff = [booking(arrival=cutoff, nights=1, lead_time=0) for _ in range(70)]
    before = build_at(spec, quiet)
    after = build_at(spec, quiet + on_cutoff)
    a = next(row for row in before.rows if row.target_date == target)
    b = next(row for row in after.rows if row.target_date == target)
    assert b.features["demand_rolling_mean_7"] != a.features["demand_rolling_mean_7"]


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_a_booking_entered_after_the_cutoff_is_not_on_the_books(spec: HorizonSpec) -> None:
    h = spec.horizon_days
    target = dt.date(2016, 6, 1)
    at_cutoff = booking(arrival=target, nights=1, lead_time=h)
    after_cutoff = booking(arrival=target, nights=1, lead_time=h - 1)
    series = on_books_by_date(parse([at_cutoff, after_cutoff]).bookings, horizon_days=h)
    assert series["city_hotel"][target] == 1


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_a_booking_cancelled_by_the_cutoff_is_not_on_the_books(spec: HorizonSpec) -> None:
    h = spec.horizon_days
    target = dt.date(2016, 6, 1)
    cancelled_on_cutoff = booking(
        arrival=target,
        nights=1,
        lead_time=h + 30,
        status="Canceled",
        status_date=target - dt.timedelta(days=h),
    )
    series = on_books_by_date(parse([cancelled_on_cutoff]).bookings, horizon_days=h)
    assert series.get("city_hotel", {}).get(target) is None


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_a_booking_cancelled_after_the_cutoff_was_still_on_the_books(spec: HorizonSpec) -> None:
    """Dropping it would leak the knowledge that it was going to be cancelled."""
    h = spec.horizon_days
    target = dt.date(2016, 6, 1)
    cancelled_later = booking(
        arrival=target,
        nights=1,
        lead_time=h + 30,
        status="Canceled",
        status_date=target - dt.timedelta(days=h - 1),
    )
    series = on_books_by_date(parse([cancelled_later]).bookings, horizon_days=h)
    assert series["city_hotel"][target] == 1


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_capacity_is_absent_from_every_built_row(spec: HorizonSpec) -> None:
    built = build_horizon_dataset(fixture_source(), spec, source_checksum="x" * 64)
    assert all(row.features["rooms_existing_at_cutoff"] is None for row in built.dataset.rows)


# --- 3. the datasets and the frozen protocol ------------------------------------------------------


def test_the_horizons_are_seven_fourteen_and_twenty_eight_in_that_order() -> None:
    assert [spec.horizon_days for spec in HORIZONS] == [7, 14, 28]
    assert [spec.dataset_name for spec in HORIZONS] == [
        "demand_daily_h7_v1",
        "demand_daily_h14_v1",
        "demand_daily_h28_v1",
    ]
    assert [spec.model_version for spec in HORIZONS] == [
        "demand_h7_v1",
        "demand_h14_v1",
        "demand_h28_v1",
    ]


@pytest.mark.parametrize(
    ("horizon", "lag"), [(1, 7), (7, 7), (8, 14), (14, 14), (15, 21), (28, 28)]
)
def test_the_baseline_is_the_same_weekday_the_fewest_weeks_back(horizon: int, lag: int) -> None:
    assert baseline_lag_for(horizon) == lag


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_each_horizon_fixes_its_lags_baseline_floor_and_origins(spec: HorizonSpec) -> None:
    expected = {
        7: ((7, 14, 28), 7, 40),
        14: ((14, 28), 14, 20),
        28: ((28,), 28, 10),
    }[spec.horizon_days]
    assert (spec.lag_days, spec.baseline_lag_days, spec.minimum_folds) == expected
    assert spec.rolling_windows == (7, 14, 28)
    origin = spec.rolling_origin
    assert origin.horizon_days == origin.step_days == spec.horizon_days
    assert origin.minimum_train_days == 365
    assert origin.windows_tile


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_each_committed_dataset_is_the_one_the_protocol_names(spec: HorizonSpec) -> None:
    dataset = load_processed_dataset(spec.dataset_path)
    manifest = load_dataset_manifest(spec.manifest_path)
    block = manifest["dataset"]
    assert isinstance(block, dict)
    assert dataset.sha256 == spec.dataset_sha256 == block["processed_sha256"]
    assert block["name"] == spec.dataset_name
    assert block["horizon_days"] == spec.horizon_days
    assert block["lag_days"] == list(spec.lag_days)
    assert block["dataset_version"] == block["feature_version"] == "v1"
    assert dataset.feature_names == spec.feature_order
    assert len(dataset.rows) == 1462
    assert {row.horizon_days for row in dataset.rows} == {spec.horizon_days}


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_every_committed_row_is_cut_off_exactly_h_days_before_its_target(spec: HorizonSpec) -> None:
    with spec.dataset_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            target = dt.date.fromisoformat(row["target_date"])
            cutoff = dt.datetime.fromisoformat(row["prediction_cutoff"])
            # Midnight UTC at the END of target - h: facts dated on or before that day only.
            assert cutoff == dt.datetime.combine(
                target - dt.timedelta(days=spec.horizon_days - 1), dt.time.min, tzinfo=dt.UTC
            )
            assert row["rooms_existing_at_cutoff"] == ""


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_two_builds_from_the_same_bytes_are_byte_identical(spec: HorizonSpec) -> None:
    first = build_horizon_dataset(fixture_source(), spec, source_checksum="x" * 64)
    second = build_horizon_dataset(fixture_source(), spec, source_checksum="x" * 64)
    assert first.processed == second.processed
    assert first.sha256 == second.sha256


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_the_manifest_states_the_horizon_matching_and_its_limits(spec: HorizonSpec) -> None:
    manifest = load_dataset_manifest(spec.manifest_path)
    block = manifest["horizon_matching"]
    assert isinstance(block, dict)
    assert block["dataset_horizon_days"] == spec.horizon_days
    assert block["capacity"] == CAPACITY_STATEMENT
    assert block["on_books"] == ON_BOOKS_STATEMENT
    generation = manifest["generation"]
    assert isinstance(generation, dict)
    assert generation["model_trained"] is False


def test_the_protocol_is_frozen_at_its_pinned_checksum() -> None:
    assert PROTOCOL_VERSION == "multi_horizon_v1"
    assert protocol_digest() == PROTOCOL_SHA256 == MULTI_HORIZON_PROTOCOL_SHA256
    assert_protocol_frozen()


def test_the_protocol_states_every_limit_it_measures_under() -> None:
    settings = protocol_settings()
    assert "uncapped" in str(settings["capacity"])
    assert "must declare its own capacity-capping rule" in str(settings["capacity"])
    assert "offline approximation" in str(settings["on_books"])
    assert "No winner" in COMPARISON_RULE and "not ranked" in COMPARISON_RULE
    assert "aggregated across horizons" in COMPARISON_RULE
    assert settings["tuning"] == "none; the Stage 6.3 configuration at every horizon"
    assert settings["estimator_sha256"] == STAGE_63_ESTIMATOR_SHA256
    assert "no interval" in str(settings["uncertainty"])
    assert settings["claims"] == dict(CLAIMS)
    assert all(value is False for value in CLAIMS.values())
    assert settings["acceptance_policy_digest"] == ACCEPTANCE_V2_SHA256


# --- acceptance_v2 -------------------------------------------------------------------------------


def evidence_for(spec: HorizonSpec, **overrides: object) -> AcceptanceEvidence:
    values: dict[str, object] = {
        "paired_observations": 500,
        "folds": spec.minimum_folds,
        "baseline_skipped": 0,
        "learned_skipped": 0,
        "incomplete_windows": 1,
        "hotels_with_sufficient_coverage": 2,
        "dataset_version": "v1",
        "feature_version": "v1",
        "dataset_sha256": spec.dataset_sha256,
        "forecast_horizon_days": spec.horizon_days,
        "model_version": spec.model_version,
        "deterministic": True,
        "leakage_checks_passed": True,
        "metrics_present": True,
    }
    values.update(overrides)
    return AcceptanceEvidence(**values)  # type: ignore[arg-type]


def test_acceptance_v2_is_pinned_and_leaves_acceptance_v1_alone() -> None:
    assert ACCEPTANCE_POLICY_VERSION == "acceptance_v2"
    assert acceptance_v2_digest() == ACCEPTANCE_V2_SHA256
    assert ACCEPTANCE_POLICY.checksum() == ACCEPTANCE_POLICY_SHA256


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_acceptance_v2_holds_every_stage_64_floor_and_this_horizons_identity(
    spec: HorizonSpec,
) -> None:
    policy = acceptance_policy(spec)
    assert policy.version == "acceptance_v2"
    assert policy.minimum_folds == spec.minimum_folds
    assert policy.minimum_paired_observations == 500
    assert policy.maximum_skipped_predictions == 0
    assert policy.maximum_incomplete_windows == 1
    assert policy.minimum_hotels_with_sufficient_coverage == 2
    assert policy.required_dataset_sha256 == spec.dataset_sha256
    assert policy.required_forecast_horizon_days == spec.horizon_days
    assert policy.required_model_version == spec.model_version
    assert policy.require_deterministic_execution is True
    assert policy.require_leakage_checks is True
    assert policy.require_all_metrics is True


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_acceptance_v2_passes_at_every_floor_exactly(spec: HorizonSpec) -> None:
    result = evaluate_acceptance_v2(spec, evidence_for(spec))
    assert result.passed
    assert len(result.checks) == 13
    assert result.policy_version == "acceptance_v2"


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("paired_observations", 499),
        ("baseline_skipped", 1),
        ("learned_skipped", 1),
        ("incomplete_windows", 2),
        ("hotels_with_sufficient_coverage", 1),
        ("deterministic", False),
        ("leakage_checks_passed", False),
        ("metrics_present", False),
        ("feature_version", "v2"),
    ],
)
def test_acceptance_v2_fails_any_single_breach(
    spec: HorizonSpec, field: str, value: object
) -> None:
    assert not evaluate_acceptance_v2(spec, evidence_for(spec, **{field: value})).passed


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_acceptance_v2_fails_one_fold_below_this_horizons_floor(spec: HorizonSpec) -> None:
    result = evaluate_acceptance_v2(spec, evidence_for(spec, folds=spec.minimum_folds - 1))
    assert [check.name for check in result.failed] == ["minimum_folds"]


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_acceptance_v2_refuses_another_horizons_dataset_model_or_horizon(spec: HorizonSpec) -> None:
    other = next(candidate for candidate in HORIZONS if candidate != spec)
    for field, value in (
        ("dataset_sha256", other.dataset_sha256),
        ("model_version", other.model_version),
        ("forecast_horizon_days", other.horizon_days),
        ("dataset_sha256", REQUIRED_DATASET_SHA256),
    ):
        assert not evaluate_acceptance_v2(spec, evidence_for(spec, **{field: value})).passed


def test_acceptance_v2_cannot_see_which_method_performed_better() -> None:
    """Structural: the evidence it reads has no field a metric or a comparison could live in."""
    names = [field.name for field in dataclasses.fields(AcceptanceEvidence)]
    for name in names:
        for fragment in FORBIDDEN_EVIDENCE_FRAGMENTS:
            assert fragment not in name, name
    spec = HORIZONS[0]
    # Same evidence, same verdict: nothing else is an input.
    assert (
        evaluate_acceptance_v2(spec, evidence_for(spec)).as_dict()
        == evaluate_acceptance_v2(spec, evidence_for(spec)).as_dict()
    )


def test_the_acceptance_evidence_is_built_from_no_metric() -> None:
    """Source-level: the one place evidence is assembled reads counts, digests and booleans.

    The dataclass cannot carry a metric (see above), but a metric could still be smuggled into a
    boolean -- ``deterministic=learned.mae < baseline.mae`` would type-check. This reads the call.
    """
    source = (REPOSITORY_ROOT / "ml" / "horizons.py").read_text(encoding="utf-8")
    start = source.index("evidence = AcceptanceEvidence(")
    call = source[start : source.index("\n    )\n", start)]
    for fragment in ("mae", "rmse", "smape", "comparison", "difference", "<", ">"):
        assert fragment not in call.lower(), fragment


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_a_dataset_the_protocol_does_not_name_is_refused(spec: HorizonSpec) -> None:
    v1 = load_processed_dataset(REPOSITORY_ROOT / "ml/data/processed/demand_daily_v1.csv")
    with pytest.raises(HorizonError):
        assert_dataset_identity(spec, v1)
    other = next(candidate for candidate in HORIZONS if candidate != spec)
    with pytest.raises(HorizonError):
        assert_dataset_identity(spec, load_processed_dataset(other.dataset_path))


def test_acceptance_v2_explains_its_own_floors() -> None:
    result = evaluate_acceptance_v2(HORIZONS[2], evidence_for(HORIZONS[2]))
    rationale = {check.name: check.rationale for check in result.checks}
    assert "40 / 20 / 10" in rationale["minimum_folds"]
    assert "seven, the Stage 6.3 horizon" not in rationale["required_forecast_horizon_days"]


# --- 4. the committed records -----------------------------------------------------------------


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_every_record_is_committed_and_its_content_checksum_holds(spec: HorizonSpec) -> None:
    for name in RECORDS:
        stored = record(spec, name)
        generation = stored["generation"]
        assert isinstance(generation, dict)
        assert generation["content_sha256"] == content_checksum(stored), name


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_the_registry_names_this_horizon_protocol_dataset_and_baseline(spec: HorizonSpec) -> None:
    registry = record(spec, "registry.json")
    model = registry["model"]
    protocol = registry["protocol"]
    dataset = registry["dataset"]
    assert isinstance(model, dict) and isinstance(protocol, dict) and isinstance(dataset, dict)
    assert model["model_version"] == spec.model_version
    assert model["model_name"] == "demand_horizon"
    assert model["forecast_horizon_days"] == spec.horizon_days
    assert model["methods"] == {
        "baseline": f"seasonal_naive_{spec.baseline_lag_days}",
        "learned": "hist_gradient_boosting",
    }
    assert model["configuration_sha256"] == STAGE_63_ESTIMATOR_SHA256
    assert model["features"]["selected"] == list(EXPECTED_SELECTED[spec.horizon_days])
    assert model["predictions"] == "raw, uncapped room nights"
    assert model["serving_path"] is None
    assert protocol["protocol_version"] == PROTOCOL_VERSION
    assert protocol["protocol_sha256"] == MULTI_HORIZON_PROTOCOL_SHA256
    assert protocol["acceptance_policy_digest"] == ACCEPTANCE_V2_SHA256
    assert protocol["folds"] >= spec.minimum_folds
    assert protocol["skipped_folds"] == 0
    assert dataset["dataset_sha256"] == spec.dataset_sha256
    assert dataset["name"] == spec.dataset_name


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_acceptance_v2_passed_on_counts_versions_and_digests(spec: HorizonSpec) -> None:
    acceptance = record(spec, "registry.json")["acceptance"]
    assert isinstance(acceptance, dict)
    assert acceptance["policy_version"] == "acceptance_v2"
    assert acceptance["result"] == "PASS"
    assert acceptance["criteria_passed"] == acceptance["criteria_total"] == 13
    evidence = acceptance["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["baseline_skipped"] == evidence["learned_skipped"] == 0
    assert evidence["incomplete_windows"] <= 1
    assert evidence["deterministic"] is True
    assert evidence["leakage_checks_passed"] is True
    for key in evidence:
        for fragment in FORBIDDEN_EVIDENCE_FRAGMENTS:
            assert fragment not in key


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_the_leakage_recheck_passed_at_the_datasets_own_horizon(spec: HorizonSpec) -> None:
    leakage = record(spec, "validation.json")["leakage"]
    assert isinstance(leakage, dict)
    assert leakage["passed"] is True
    assert all(leakage["checks"].values())


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_every_claim_is_false_and_every_limit_is_stated(spec: HorizonSpec) -> None:
    for name in ("registry.json", "artifact.json"):
        claims = record(spec, name)["claims"]
        assert isinstance(claims, dict)
        for key in CLAIMS:
            assert claims[key] is False, (name, key)
    registry_claims = record(spec, "registry.json")["claims"]
    assert isinstance(registry_claims, dict)
    assert registry_claims["capacity"] == CAPACITY_STATEMENT
    assert registry_claims["on_books"] == ON_BOOKS_STATEMENT


@pytest.mark.parametrize("spec", HORIZONS, ids=horizon_ids)
def test_the_artifact_is_described_but_never_committed(spec: HorizonSpec) -> None:
    artifact = record(spec, "artifact.json")
    block = artifact["artifact"]
    protocol = artifact["protocol"]
    assert isinstance(block, dict) and isinstance(protocol, dict)
    assert block["committed"] is False
    assert len(str(block["canonical_model_digest"])) == 64
    assert block["canonical_model_digest"] != V1_CANONICAL_DIGEST
    assert protocol["protocol_sha256"] == MULTI_HORIZON_PROTOCOL_SHA256
    assert protocol["validation_record_sha256"] == content_checksum(record(spec, "validation.json"))
    assert protocol["registry_record_sha256"] == content_checksum(record(spec, "registry.json"))


def test_no_model_binary_can_be_committed() -> None:
    ignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "ml/models/*/*" in ignore
    assert "!ml/models/*/model.pkl" not in ignore
    for spec in HORIZONS:
        assert f"!ml/data/processed/{spec.dataset_name}.csv" in ignore


def test_the_probe_grid_extends_stage_65s_rule_over_each_models_own_columns() -> None:
    from ml.artifact import probe_matrix as stage_65_probe_matrix

    # Over the V1 columns the rule reproduces Stage 6.5's grid exactly.
    assert probe_matrix(V1_SEVEN_DAY_SELECTION) == stage_65_probe_matrix()
    for spec in HORIZONS:
        grid = probe_matrix(EXPECTED_SELECTED[spec.horizon_days])
        assert all(len(row) == len(EXPECTED_SELECTED[spec.horizon_days]) for row in grid)
    with pytest.raises(HorizonError):
        probe_matrix(("rooms_existing_at_cutoff",))


def test_the_report_is_one_horizon_at_a_time() -> None:
    """There is no combined figure: each report is one horizon beside its own baseline."""
    for spec in HORIZONS:
        report = side_by_side(record(spec, "registry.json"))
        assert report["forecast_horizon_days"] == spec.horizon_days
        assert report["baseline_method"] == spec.baseline_method
        assert set(report) == {
            "forecast_horizon_days",
            "model_version",
            "baseline_method",
            "paired_observations",
            "baseline",
            "learned",
        }


def _keys(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key in value] + [k for item in value.values() for k in _keys(item)]
    if isinstance(value, list):
        return [k for item in value for k in _keys(item)]
    return []


def test_no_record_ranks_or_aggregates_across_horizons() -> None:
    """Checked on structure, not prose: the records' own disclaimers say "no winner" in words.

    No field anywhere names a winner, a rank, a best or an aggregate, and no horizon's record
    mentions another horizon's model -- so no record can carry a cross-horizon figure.
    """
    for spec in HORIZONS:
        others = [other.model_version for other in HORIZONS if other != spec]
        for name in RECORDS:
            stored = record(spec, name)
            for key in _keys(stored):
                lowered = key.lower()
                for word in ("winner", "rank", "best", "aggregate", "all_horizons", "overall"):
                    assert word not in lowered, (spec.model_version, name, key)
            text = json.dumps(stored)
            for other in others:
                assert other not in text, (spec.model_version, name, other)


def test_the_datasets_parse_as_committed_text() -> None:
    for spec in HORIZONS:
        payload = spec.dataset_path.read_bytes()
        assert payload.endswith(b"\n") and b"\r\n" not in payload
        header = next(csv.reader(io.StringIO(payload.decode("utf-8"))))
        assert tuple(header[7:]) == spec.feature_order


def test_the_documented_figures_are_the_committed_ones() -> None:
    """The pooled figures docs/ml-multi-horizon.md quotes must be the registry's own."""
    doc = (REPOSITORY_ROOT / "docs" / "ml-multi-horizon.md").read_text(encoding="utf-8")
    for spec in HORIZONS:
        report = side_by_side(record(spec, "registry.json"))
        section = doc.split(f"— `{spec.model_version}`", 1)[1].split("###", 1)[0]
        for method, label in (
            ("baseline", spec.baseline_method),
            ("learned", "hist_gradient_boosting"),
        ):
            scores = report[method]
            assert isinstance(scores, dict)
            row = f"| `{label}` | {scores['mae']} | {scores['rmse']} | {scores['smape']} |"
            assert row in section, (spec.model_version, row)
        metrics = record(spec, "metrics.json")["metrics"]
        assert isinstance(metrics, dict)
        assert f"`{spec.model_version}` · {len(metrics['per_fold'])} folds" in doc
        assert spec.dataset_sha256[:8] in doc and spec.dataset_sha256[-6:] in doc
    assert PROTOCOL_SHA256 in doc
