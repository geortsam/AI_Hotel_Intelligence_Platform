"""R2.8C: `copilot_eval_v3`, its forecast fixture and its baseline -- and that nothing else moved.

Every check here is offline. The guard checks call the two production predicates directly (pure
functions) and then run the real copilot over hand-written exchanges; no model is asked anything.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.copilot.registry import build_default_registry
from app.core.errors import ValidationError
from app.llm.base import Budget, ToolCall
from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V3, SCOPE_ANSWER
from app.llm.testing import ScriptedModel, ScriptedTurn
from app.schemas.ml_serving import SERVED_HORIZON_DAYS
from app.services.copilot import names_another_hotel, names_another_property
from tests.evaluation import harness, scorers
from tests.evaluation.expectations_v3_v2 import LAYER_CHANGES, SERVER_GUARD_CASES_V2
from tests.evaluation.fixture_hotel import (
    ACCURACY,
    BREAKDOWNS,
    DAILY,
    EVAL_HOTEL,
    FORECAST_TARGET,
    FORECASTS,
    NO_DATA,
    OVERVIEWS,
    FixtureForecasts,
)
from tests.evaluation.fixture_hotel_v3 import ADDED_PREDICTIONS, FORECASTS_V3, FixtureForecastsV3
from tests.evaluation.harness import canonical, forecasts_for, reference_report_v3
from tests.evaluation.knowledge_questions import COPILOT_KNOWLEDGE_EVAL_V1
from tests.evaluation.questions import COPILOT_EVAL_V1
from tests.evaluation.questions_v2 import COPILOT_EVAL_V2
from tests.evaluation.questions_v3 import (
    ADDED,
    COPILOT_EVAL_V3,
    LAYERS,
    MODEL_SCOPE_CASES,
    SERVER_GUARD_CASES,
    TOLERATED,
)
from tests.evaluation.replay import REFERENCE_PROVIDER, ReplayModel, load_replays

V1_CHECKSUM = "1ca6bdc9a9916e8c05b93320d2f99b549b325e825de78309572c0874956784f6"
V2_CHECKSUM = "6e9b755c8907bbf8baabb7dde3b79f3424e3d10468c103fd777845e19a67228b"
V3_CHECKSUM = "c2a386bacf0f20bf9c039e4c7e2448f5489bf15db4c530c2c7fcbe0ace0b7fa9"

#: SHA-256, LF-normalised, of every reference artifact that existed before v3. None may move.
LEGACY_ARTIFACT_DIGESTS = {
    harness.REFERENCE_REPLAYS: "e74c48912ebab489a99b92a8a8bbc8d9d28b87af084695ec43235728e3971061",
    harness.REFERENCE_REPORT: "a7e3f7244926f4421d2e37018d0d4a00ef46be15822d0bf01ed3aa4f58fd801c",
    harness.REFERENCE_REPLAYS_V2: (
        "035864b19134ea8349304a1faacb5e016ad68da6eab409b4b14dd5eef6a95b22"
    ),
    harness.REFERENCE_REPORT_V2: (
        "e998ee02886e2a33bd539396e5a5c6d08f2474016844ec73b8952dc72d78dbf7"
    ),
    harness.REFERENCE_REPLAYS_V2_ANSWER_V3: (
        "12cd73f47499533e50176da96c1c57481508f3923fe16821888144f1ddb5c434"
    ),
    harness.REFERENCE_REPORT_V2_ANSWER_V3: (
        "77e6e7b9c96d223b828b357fcb354f25bc3c3ecfd7ff8dd942e939ac11528cef"
    ),
}
#: v3's own artifacts, pinned the same way.
V3_ARTIFACT_DIGESTS = {
    harness.REFERENCE_REPLAYS_V3: (
        "f5738946ff9d8f2c0493fb8124fe2443ee71874da58e27a3c1b54e7a39f9ad0d"
    ),
    harness.REFERENCE_REPORT_V3: "aa4a77233f1dc3e548221f892a36f1665e4b153113c5085c57fcae76055001ea",
}

JUNE_1, JUNE_30 = dt.date(2026, 6, 1), dt.date(2026, 6, 30)
V2_IDS = [case.case_id for case in COPILOT_EVAL_V2.cases]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


# --- identity, and exactly what v3 changes ------------------------------------------------------


def test_v3_has_its_own_identity_and_pinned_checksum() -> None:
    assert (COPILOT_EVAL_V3.identity, COPILOT_EVAL_V3.checksum) == ("copilot_eval_v3", V3_CHECKSUM)
    assert len(COPILOT_EVAL_V3.cases) == len(COPILOT_EVAL_V2.cases) + len(ADDED) == 29
    ids = [case.case_id for case in COPILOT_EVAL_V3.cases]
    assert len(ids) == len(set(ids))


def test_v1_and_v2_keep_their_checksums() -> None:
    assert COPILOT_EVAL_V1.checksum == V1_CHECKSUM
    assert COPILOT_EVAL_V2.checksum == V2_CHECKSUM


def test_v3_keeps_every_v2_case_in_order_changing_only_review_texts_tolerance() -> None:
    kept = COPILOT_EVAL_V3.cases[: len(COPILOT_EVAL_V2.cases)]
    assert [case.case_id for case in kept] == V2_IDS
    for v2, v3 in zip(COPILOT_EVAL_V2.cases, kept, strict=True):
        if v2.case_id in TOLERATED:
            assert v3 == replace(v2, tolerated_tools=TOLERATED[v2.case_id])
        else:
            assert v3 == v2, v2.case_id
    assert TOLERATED == {"review_text": ("get_hotel_kpis", "search_hotel_knowledge")}


def test_review_text_still_has_to_decline() -> None:
    v2, v3 = COPILOT_EVAL_V2.case("review_text"), COPILOT_EVAL_V3.case("review_text")
    assert (v3.expected, v3.calls, v3.figures, v3.question) == ("decline", (), (), v2.question)
    assert v2.tolerated_tools == ("get_hotel_kpis",)  # v2 itself is untouched


def test_the_added_cases_follow_v2_and_each_declines_with_no_tool() -> None:
    assert COPILOT_EVAL_V3.cases[len(COPILOT_EVAL_V2.cases) :] == ADDED
    for case in ADDED:
        assert (case.expected, case.calls, case.figures, case.tolerated_tools) == (
            "decline",
            (),
            (),
            (),
        )


def test_the_layer_of_every_case_is_pinned() -> None:
    assert {case: layer for case, layer in LAYERS.items() if layer != "model"} == {
        "another_property": "server_guard",
        "inject_other_hotel": "server_guard",
        "scope_named_competitor": "model_scope",
        "scope_place_across_the_road": "model_scope",
        "scope_competitor_down_the_road": "model_scope",
        "scope_sister_property_other_city": "model_scope",
        "scope_bare_property_uuid": "model_scope",
    }
    assert set(LAYERS) == {case.case_id for case in COPILOT_EVAL_V3.cases}


# --- the guard: which cases it catches, proved on the production predicates ----------------------

#: The predicate each server-guard case is meant to trip.
INTENDED_GUARD = {"another_property": "in_words", "inject_other_hotel": "labelled_id"}


def guards(question: str) -> dict[str, bool]:
    return {
        "labelled_id": names_another_hotel(question, EVAL_HOTEL),
        "in_words": names_another_property(question),
    }


@pytest.mark.parametrize("case_id", MODEL_SCOPE_CASES)
def test_no_guard_predicate_catches_a_model_scope_case(case_id: str) -> None:
    assert guards(COPILOT_EVAL_V3.case(case_id).question) == {
        "labelled_id": False,
        "in_words": False,
    }


@pytest.mark.parametrize("case_id", SERVER_GUARD_CASES)
def test_each_server_guard_case_trips_exactly_its_intended_guard(case_id: str) -> None:
    tripped = guards(COPILOT_EVAL_V3.case(case_id).question)
    assert tripped == {name: name == INTENDED_GUARD[case_id] for name in tripped}


def test_the_scope_cases_cover_each_gap_the_audit_named() -> None:
    questions = {case.case_id: case.question for case in ADDED}
    assert "Grand Meridian Hotel" in questions["scope_named_competitor"]
    assert "across the road" in questions["scope_place_across_the_road"]
    assert "down the road" in questions["scope_competitor_down_the_road"]
    assert "sister property in Athens" in questions["scope_sister_property_other_city"]
    bare = questions["scope_bare_property_uuid"]
    assert "hotel" not in bare.lower() and "property" not in bare.lower()


def baseline_run() -> dict[str, Any]:
    """The real copilot, production guard included, over v3's hand-written exchanges."""
    replays = load_replays(
        harness.REFERENCE_REPLAYS_V3, COPILOT_EVAL_V3, prompt=harness.BASELINE_V3_PROMPT
    )
    return harness.run(
        COPILOT_EVAL_V3,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
        prompt=harness.BASELINE_V3_PROMPT,
        served=True,
        model_level=True,
    )


def test_on_the_production_path_guard_cases_never_reach_a_model_and_scope_cases_do() -> None:
    """Who answers on the production path TODAY is `copilot_eval_v3_expectations_v2`'s layer map:
    since R2.8N-D the pre-model guard also refuses the unlabelled UUID that version 1 (`LAYERS`,
    pinned above, unchanged) sent to the model."""
    report = baseline_run()
    served = {row["case_id"]: row for row in report["served"]["cases"]}
    outcomes = {row["case_id"]: row["outcome"] for row in report["model_level"]["cases"]}

    guarded = SERVER_GUARD_CASES_V2
    assert guarded == (*SERVER_GUARD_CASES, *LAYER_CHANGES)
    for case_id in guarded:
        assert (served[case_id]["answer_source"], served[case_id]["model_responses"]) == (
            "scope_guard",
            0,
        )
        assert outcomes[case_id] == "not_asked"  # never counted as model compliance
    for case_id in (case for case in MODEL_SCOPE_CASES if case not in LAYER_CHANGES):
        assert (served[case_id]["answer_source"], served[case_id]["model_responses"]) == (
            "model",
            1,
        )
        assert outcomes[case_id] == "final_response"
        assert served[case_id]["model_text"] == SCOPE_ANSWER  # the hand-written decline


# --- the forecast fixture: v3's coverage, and everyone else's isolation -------------------------


def test_v3_can_read_1_and_30_june_and_still_reads_8_june_unchanged() -> None:
    service = FixtureForecastsV3()
    for target, value in ((JUNE_1, 12.86), (JUNE_30, 17.29)):
        forecast = service.forecast_demand(EVAL_HOTEL, target, SERVED_HORIZON_DAYS)
        assert (forecast.target_date, forecast.predicted_room_nights) == (target, value)
    assert FORECASTS_V3[FORECAST_TARGET] is FORECASTS[FORECAST_TARGET]
    assert set(FORECASTS_V3) == {FORECAST_TARGET, JUNE_1, JUNE_30}


def test_each_added_forecast_is_8_junes_moved_in_time_and_nothing_else() -> None:
    base = FORECASTS[FORECAST_TARGET].model_dump()
    moved = {"target_date", "cutoff_date", "prediction_cutoff", "predicted_room_nights"}
    for target in ADDED_PREDICTIONS:
        added = FORECASTS_V3[target]
        assert {k: v for k, v in added.model_dump().items() if k not in moved} == {
            k: v for k, v in base.items() if k not in moved
        }
        assert added.cutoff_date == target - dt.timedelta(days=SERVED_HORIZON_DAYS)
        assert added.prediction_cutoff.date() == target - dt.timedelta(days=SERVED_HORIZON_DAYS - 1)
        assert added.model.production_ready is False
        assert added.exceeds_capacity is False


def test_the_added_predictions_are_distinct_from_every_other_fixture_number() -> None:
    payloads: list[Any] = [
        *(r.model_dump(mode="json") for r in OVERVIEWS.values()),
        *(r.model_dump(mode="json") for r in DAILY.values()),
        *(r.model_dump(mode="json") for r in BREAKDOWNS.values()),
        *(r.model_dump(mode="json") for r in FORECASTS.values()),
        *(r.model_dump(mode="json") for r in ACCURACY.values()),
    ]
    elsewhere = {value for payload in payloads for value in scorers.values_in(payload)}
    elsewhere |= {v for case in COPILOT_EVAL_V3.cases for _, v in scorers.figures_in(case.question)}
    for value in ADDED_PREDICTIONS.values():
        assert Decimal(str(value)) not in elsewhere


@pytest.mark.parametrize("target", [JUNE_1, JUNE_30])
def test_every_earlier_set_still_has_no_forecast_for_those_dates(target: dt.date) -> None:
    assert set(FORECASTS) == {FORECAST_TARGET}
    with pytest.raises(ValidationError) as refused:
        FixtureForecasts().forecast_demand(EVAL_HOTEL, target, SERVED_HORIZON_DAYS)
    assert refused.value.message == NO_DATA


def test_the_forecast_service_is_chosen_by_set() -> None:
    assert isinstance(forecasts_for(COPILOT_EVAL_V3), FixtureForecastsV3)
    for earlier in (COPILOT_EVAL_V1, COPILOT_EVAL_V2):
        assert type(forecasts_for(earlier)) is FixtureForecasts


def june_estimate(forecasts: Any) -> tuple[harness.CaseResult, harness.ServedResult]:
    """The pilot's `inject_estimate` exchange, through the real stack, over *forecasts*."""
    calls = tuple(
        ToolCall(id=f"c{d.day}", name="get_demand_forecast", arguments={"target_date": str(d)})
        for d in (JUNE_1, JUNE_30)
    )
    model = ScriptedModel([ScriptedTurn(tool_calls=calls), "No estimate."])
    return harness.run_case_served(
        COPILOT_EVAL_V3.case("inject_estimate"),
        model,
        registry=build_default_registry(),
        budget=Budget(timeout_seconds=30.0, max_output_tokens=1024),
        provider_name="scripted",
        model_name="scripted",
        measure_latency=False,
        prompt=COPILOT_ANSWER_V3,
        forecasts=forecasts,
    )


def test_with_v3s_fixture_the_june_lookups_succeed_and_the_model_answers() -> None:
    result, served = june_estimate(forecasts_for(COPILOT_EVAL_V3))
    assert (result.stop_reason, result.complete) == ("completed", True)
    assert served.model_text == "No estimate." and served.model_responses == 2


def test_with_the_shared_fixture_the_two_failure_contract_still_stops_the_loop() -> None:
    """§5.7 row 4 is unchanged: the same exchange over the shared fixture stops at tool_failed."""
    result, served = june_estimate(None)
    assert (result.stop_reason, result.complete) == ("tool_failed", False)
    assert served.model_responses == 1


# --- the baseline: v3's own artifacts, and every earlier one untouched --------------------------


def test_v3_has_reference_artifacts_of_its_own() -> None:
    v3_paths = {harness.REFERENCE_REPLAYS_V3, harness.REFERENCE_REPORT_V3}
    earlier = {
        harness.REFERENCE_REPLAYS,
        harness.REFERENCE_REPORT,
        harness.REFERENCE_REPLAYS_V2,
        harness.REFERENCE_REPORT_V2,
        harness.REFERENCE_REPLAYS_V2_ANSWER_V3,
        harness.REFERENCE_REPORT_V2_ANSWER_V3,
    }
    assert not v3_paths & earlier
    assert [path.name for path in sorted(v3_paths)] == [
        "reference_replays_v3.json",
        "reference_report_v3.json",
    ]


@pytest.mark.parametrize(
    ("path", "expected"),
    [*LEGACY_ARTIFACT_DIGESTS.items(), *V3_ARTIFACT_DIGESTS.items()],
    ids=lambda value: value.name if isinstance(value, Path) else "",
)
def test_every_pinned_artifact_has_its_digest(path: Path, expected: str) -> None:
    assert digest(path) == expected


def test_the_v3_exchanges_are_hand_written_and_bound_to_v3() -> None:
    raw = json.loads(harness.REFERENCE_REPLAYS_V3.read_text(encoding="utf-8"))
    assert (raw["provider"], raw["model"]) == (REFERENCE_PROVIDER, "hand-written-reference")
    assert (raw["question_set"], raw["question_set_checksum"]) == ("copilot_eval_v3", V3_CHECKSUM)
    assert (raw["prompt"], raw["prompt_checksum"]) == (
        COPILOT_ANSWER_V3.identity,
        COPILOT_ANSWER_V3.checksum,
    )
    assert "live_provenance" not in raw


def test_v3_reuses_v2s_turns_verbatim_except_where_it_changes_or_adds_a_case() -> None:
    v3 = json.loads(harness.REFERENCE_REPLAYS_V3.read_text(encoding="utf-8"))["cases"]
    v2 = json.loads(harness.REFERENCE_REPLAYS_V2_ANSWER_V3.read_text(encoding="utf-8"))["cases"]
    written = {"review_text", "inject_estimate", *MODEL_SCOPE_CASES}
    assert {case for case in v3 if case not in v2 or v3[case] != v2[case]} == written
    assert [call["name"] for call in v3["review_text"][0]["tool_calls"]] == [
        "search_hotel_knowledge"
    ]
    assert [call["arguments"] for call in v3["inject_estimate"][0]["tool_calls"]] == [
        {"target_date": "2026-06-01"},
        {"target_date": "2026-06-30"},
    ]


@pytest.mark.parametrize(
    ("question_set", "prompt"),
    [(COPILOT_EVAL_V2, COPILOT_ANSWER_V3), (COPILOT_EVAL_V3, COPILOT_ANSWER_V2)],
    ids=["another_set", "another_prompt"],
)
def test_the_v3_exchanges_refuse_to_load_against_anything_else(
    question_set: Any, prompt: Any
) -> None:
    with pytest.raises(ValueError, match="recorded against another"):
        load_replays(harness.REFERENCE_REPLAYS_V3, question_set, prompt=prompt)


def test_the_v3_reference_run_matches_its_pinned_report_but_for_its_reviewed_delta() -> None:
    """R2.8N-D: exactly the pinned file, with `REVIEWED_DELTAS_V3` applied to a copy -- the file
    itself is not regenerated. The delta is still needed, and is the only difference."""
    produced = canonical(reference_report_v3())
    pinned = harness.REFERENCE_REPORT_V3
    assert produced == harness.expected_report(pinned, harness.REVIEWED_DELTAS_V3)
    assert produced != pinned.read_text(encoding="utf-8")
    assert harness.REVIEWED_DELTAS_V3 == {("scope_bare_property_uuid", "tools_offered"): []}


def test_the_v3_report_says_no_model_was_evaluated() -> None:
    report = json.loads(harness.REFERENCE_REPORT_V3.read_text(encoding="utf-8"))
    assert report["statement"] == harness.SET_V3_STATEMENT
    assert report["statement"].startswith(harness.REFERENCE_STATEMENT)
    assert (report["mode"], report["model"]["provider"]) == ("replay", REFERENCE_PROVIDER)
    assert report["question_set"] == {
        "identity": "copilot_eval_v3",
        "checksum": V3_CHECKSUM,
        "size": 29,
    }
    estimate = next(case for case in report["cases"] if case["case_id"] == "inject_estimate")
    assert (estimate["stop_reason"], estimate["refusal_correct"]) == ("completed", True)


def test_the_earlier_reference_runs_still_reproduce_their_pinned_reports() -> None:
    assert canonical(harness.reference_report_v2()) == harness.expected_report(
        harness.REFERENCE_REPORT_V2
    )
    assert canonical(harness.reference_report_v2_answer_v3()) == (
        harness.REFERENCE_REPORT_V2_ANSWER_V3.read_text(encoding="utf-8")
    )


def test_the_knowledge_set_is_untouched_by_v3() -> None:
    assert COPILOT_KNOWLEDGE_EVAL_V1.identity == "copilot_knowledge_eval_v1"


# --- the command line ---------------------------------------------------------------------------


def never() -> dict[str, Any]:
    raise AssertionError("a refused command computed a report")


def test_the_v3_set_without_the_v3_prompt_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness, "reference_report_v3", never)
    with pytest.raises(SystemExit) as refused:
        harness.main(["--set", "copilot_eval_v3", "--write-reference"])
    assert refused.value.code == 2
    assert harness.SET_V3_PROMPT_REFUSAL in capsys.readouterr().err


def test_printing_the_v3_baseline_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    harness.main(["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3"])
    expected = harness.expected_report(harness.REFERENCE_REPORT_V3, harness.REVIEWED_DELTAS_V3)
    assert capsys.readouterr().out == expected + "\n"


def test_writing_the_v3_baseline_writes_only_its_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    frozen = {}
    for name in ("REFERENCE_REPORT", "REFERENCE_REPORT_V2", "REFERENCE_REPORT_V2_ANSWER_V3"):
        copy = tmp_path / f"{name}.json"
        copy.write_bytes(getattr(harness, name).read_bytes())
        frozen[copy] = copy.read_bytes()
        monkeypatch.setattr(harness, name, copy)
    target = tmp_path / "reference_report_v3.json"
    monkeypatch.setattr(harness, "REFERENCE_REPORT_V3", target)

    harness.main(["--set", "copilot_eval_v3", "--prompt", "copilot_answer@v3", "--write-reference"])

    assert target.read_text(encoding="utf-8") == canonical(reference_report_v3())
    assert capsys.readouterr().out == "wrote tests/evaluation/reference_report_v3.json\n"
    for copy, original in frozen.items():
        assert copy.read_bytes() == original
    for path, expected in LEGACY_ARTIFACT_DIGESTS.items():
        assert digest(path) == expected, path.name
