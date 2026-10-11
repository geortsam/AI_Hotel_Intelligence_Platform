"""R2.2b: the ``hotel_transfer_v1`` protocol, its diagnostics and its arithmetic.

Synthetic panels only (R2.2's builder, which goes through the Stage 6.1 contract). Nothing here
scores the real validation or test partitions: the protocol and these tests were committed before
that happened. Diagnostic arithmetic is checked against values computed by hand.
"""

from __future__ import annotations

import dataclasses

import pytest

from ml import hotel_transfer as ht
from ml.frozen_holdout import FrozenHoldoutError
from ml.manifests import content_checksum
from tests.ml.test_frozen_holdout import SYNTHETIC_SHA256, panel, scored

# --- the protocol ------------------------------------------------------------------------------


def test_the_protocol_settings_hash_to_the_committed_checksum() -> None:
    ht.assert_protocol_frozen()
    assert ht.protocol_digest() == ht.PROTOCOL_SHA256


def test_any_change_to_the_protocol_settings_stops_the_experiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ht, "LAG_RANGES", ((0.0, None),))
    with pytest.raises(ht.HotelTransferError, match="not the frozen protocol"):
        ht.assert_protocol_frozen()


def test_the_four_experiments_are_two_controls_and_two_transfers() -> None:
    assert [(e.train_hotel, e.evaluate_hotel, e.kind) for e in ht.EXPERIMENTS] == [
        ("city_hotel", "city_hotel", "same_hotel"),
        ("resort_hotel", "resort_hotel", "same_hotel"),
        ("city_hotel", "resort_hotel", "cross_hotel"),
        ("resort_hotel", "city_hotel", "cross_hotel"),
    ]


def test_the_protocol_makes_no_claim_and_names_no_capacity() -> None:
    settings = ht.protocol_settings()
    assert settings["claims"] == dict.fromkeys(ht.CLAIMS, False)
    assert "not hotel capacity" in str(settings["observed_maxima_note"])
    assert "prediction - actual" in str(settings["sign_convention"])


# --- the signed-error convention ---------------------------------------------------------------


def test_signed_error_is_prediction_minus_actual() -> None:
    rows = scored((100, 110.0, 95.0), (100, 90.0, 104.0), (100, 100.0, 100.0))
    assert ht.signed_errors(rows, "learned") == [10.0, -10.0, 0.0]
    assert ht.signed_errors(rows, "baseline") == [-5.0, 4.0, 0.0]
    summary = ht.prediction_summary(rows, "learned", train_min=0.0, train_max=500.0)
    assert summary["mean_signed_error"] == 0.0
    assert (summary["over_predictions"], summary["under_predictions"], summary["exact"]) == (
        1,
        1,
        1,
    )


def test_a_systematic_over_prediction_has_a_positive_mean_signed_error() -> None:
    summary = ht.prediction_summary(
        scored((100, 130.0, 100.0), (200, 220.0, 200.0)),
        "learned",
        train_min=0.0,
        train_max=500.0,
    )
    assert summary["mean_signed_error"] == pytest.approx((30 + 20) / 2)


def test_signed_errors_use_only_the_paired_rows() -> None:
    rows = scored((100, 150.0, None), (100, None, 90.0), (100, 104.0, 98.0))
    assert ht.signed_errors(rows, "learned") == [4.0]
    assert ht.signed_errors(rows, "baseline") == [-2.0]


# --- the training-range check ------------------------------------------------------------------


def test_predictions_outside_the_training_range_are_counted_strictly() -> None:
    rows = scored((100, 40.0, 1.0), (100, 50.0, 1.0), (100, 100.0, 1.0), (100, 120.0, 1.0))
    summary = ht.prediction_summary(rows, "learned", train_min=50.0, train_max=100.0)
    assert summary["below_training_range"] == 1
    assert summary["above_training_range"] == 1
    assert summary["fraction_outside_training_range"] == 0.5
    assert (summary["prediction_min"], summary["prediction_max"]) == (40.0, 120.0)


def test_an_empty_pair_has_no_diagnostic_values() -> None:
    summary = ht.prediction_summary(
        scored((100, None, 90.0)), "learned", train_min=0.0, train_max=1.0
    )
    assert summary["paired_observations"] == 0
    assert summary["mean_signed_error"] is None
    assert summary["fraction_outside_training_range"] is None


# --- groups and lag ranges ---------------------------------------------------------------------


def test_lag_ranges_are_half_open() -> None:
    row = scored((100, 1.0, 1.0))[0].row

    def with_lag(value: float) -> str | None:
        return ht.lag_range_of(dataclasses.replace(row, features={"demand_lag_7": value}))

    assert with_lag(59.999) == "[0,60)"
    assert with_lag(60.0) == "[60,100)"
    assert with_lag(189.0) == "[170,190)"
    assert with_lag(190.0) == "[190,inf)"
    assert with_lag(10_000.0) == "[190,inf)"
    assert ht.lag_range_of(dataclasses.replace(row, features={})) is None


def test_every_lag_range_is_reported_and_small_ones_carry_no_metric() -> None:
    rows = list(scored(*[(100, 101.0, 99.0)] * 30))
    rows = [
        dataclasses.replace(s, row=dataclasses.replace(s.row, features={"demand_lag_7": 150.0}))
        for s in rows
    ] + [
        dataclasses.replace(s, row=dataclasses.replace(s.row, features={"demand_lag_7": 50.0}))
        for s in scored(*[(100, 101.0, 99.0)] * 29)
    ]
    groups = ht.by_lag_range(rows, train_min=0.0, train_max=500.0)
    assert list(groups) == [
        "[0,60)",
        "[60,100)",
        "[100,140)",
        "[140,170)",
        "[170,190)",
        "[190,inf)",
    ]
    low = groups["[0,60)"]
    mid = groups["[140,170)"]
    assert isinstance(low, dict) and isinstance(mid, dict)
    assert low["sufficient_coverage"] is False and low["paired_observations"] == 29
    assert "diagnostics" not in low
    assert mid["sufficient_coverage"] is True
    assert mid["paired"]["observations"] == 30
    assert mid["diagnostics"]["learned"]["mean_signed_error"] == 1.0


def test_a_group_compares_both_methods_on_identical_rows() -> None:
    group = ht.evaluate_group(
        scored(
            *[(100, 103.0, 98.0)] * 29, (100, None, 90.0), (100, 104.0, None), (100, 99.0, 99.0)
        ),
        train_min=0.0,
        train_max=500.0,
    )
    paired = group["paired"]
    assert isinstance(paired, dict)
    assert paired["observations"] == 30
    assert group["diagnostics"]["learned"]["paired_observations"] == 30  # type: ignore[index]
    assert group["diagnostics"]["baseline"]["paired_observations"] == 30  # type: ignore[index]


# --- experiments and the record ----------------------------------------------------------------


def test_a_same_hotel_control_trains_and_evaluates_on_one_hotel() -> None:
    result = ht.run_experiment(panel(), ht.Experiment("city_hotel", "city_hotel"))
    assert result["kind"] == "same_hotel"
    training = result["training"]
    periods = result["periods"]
    assert isinstance(training, dict) and isinstance(periods, dict)
    assert training["fitted_rows"] + training["skipped_rows"] == training["partition_rows"]
    assert training["target_min"] <= training["target_max"]
    for name in ("validation", "test"):
        period = periods[name]
        assert period["coverage"]["hotels"] == ["city_hotel"]
        assert period["excluded_rows"] == 0
        assert list(period["by_hotel"]) == ["city_hotel"]


def test_a_cross_hotel_experiment_never_fits_on_the_evaluated_hotel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fitted: list[set[str]] = []
    original = ht.LearnedModel.fit

    def spy(rows, feature_names, config):  # type: ignore[no-untyped-def]
        fitted.append({row.hotel_key for row in rows} | {row.partition for row in rows})
        return original(rows, feature_names, config)

    monkeypatch.setattr(ht.LearnedModel, "fit", spy)
    ht.run_experiment(panel(), ht.Experiment("resort_hotel", "city_hotel"))
    assert fitted == [{"resort_hotel", "train"}]


def test_the_record_is_deterministic_and_complete() -> None:
    dataset = panel()
    first = ht.build_record(dataset, expected_dataset_sha256=SYNTHETIC_SHA256)
    second = ht.build_record(dataset, expected_dataset_sha256=SYNTHETIC_SHA256)
    assert content_checksum(first) == content_checksum(second)
    assert first["protocol"] == {"version": ht.PROTOCOL_VERSION, "sha256": ht.PROTOCOL_SHA256}
    experiments = first["experiments"]
    assert isinstance(experiments, list)
    assert [e["name"] for e in experiments] == [e.name for e in ht.EXPERIMENTS]
    for experiment in experiments:
        assert set(experiment["periods"]) == {"validation", "test"}
        overall = experiment["periods"]["test"]["overall"]
        assert "mean_signed_error" in overall["diagnostics"]["learned"]
    assert "generation" not in first
    assert first["claims"] == dict(ht.CLAIMS)


def test_the_record_refuses_another_dataset() -> None:
    with pytest.raises(FrozenHoldoutError, match="hashes to"):
        ht.build_record(panel())
