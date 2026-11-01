"""R2.8AA: every failed model request keeps its declared code, on the case it belongs to.

Before R2.8AA a request that raised after a tool round left no trace on the case: the tool loop
turns it into `model_failed` without raising, `CaseResult.error_code` stays `None`, and only the
live script's `run_health` knew the code. `RecordingModel` now keeps it. Scripted models only --
no provider is asked anything.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.copilot.registry import build_default_registry
from app.llm.base import Budget, ChatRequest, Message
from app.llm.errors import (
    InvalidResponseDiagnostics,
    LlmError,
    LlmInvalidResponseError,
    LlmUnavailableError,
)
from app.llm.prompts.registry import COPILOT_ANSWER_V3
from tests.evaluation import harness, integrity
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3
from tests.evaluation.replay import (
    RecordedFailure,
    RecordingModel,
    ReplayError,
    ReplayModel,
    ReplayThenRaise,
    Turn,
)

JUNE_1 = ("get_demand_forecast", {"target_date": "2026-06-01"})
MAY = {"date_from": "2026-05-01", "date_to": "2026-05-31"}
APRIL = {"date_from": "2026-04-01", "date_to": "2026-04-30"}


def turn(text: str = "", *calls: tuple[str, dict[str, str]]) -> Turn:
    return Turn.from_dict(
        {
            "text": text,
            "tool_calls": [
                {"id": f"t{i}", "name": name, "arguments": args}
                for i, (name, args) in enumerate(calls)
            ],
        }
    )


def request() -> ChatRequest:
    return ChatRequest(
        prompt_id="copilot_answer",
        prompt_version="v3",
        messages=(Message(role="user", content="q"),),
        budget=Budget(timeout_seconds=30.0, max_output_tokens=1024),
    )


def invalid() -> LlmInvalidResponseError:
    return LlmInvalidResponseError(
        diagnostics=InvalidResponseDiagnostics("length_with_tool_calls", tool_calls_parsed=27)
    )


def one_case(case_id: str, model: Any) -> tuple[Any, Any, integrity.CaseEvidence]:
    evidence: list[integrity.CaseEvidence] = []
    result, served = harness.run_case_served(
        COPILOT_EVAL_V3.case(case_id),
        model,
        registry=build_default_registry(),
        budget=Budget(timeout_seconds=30.0, max_output_tokens=1024),
        provider_name="scripted",
        model_name="scripted",
        measure_latency=False,
        prompt=COPILOT_ANSWER_V3,
        forecasts=harness.forecasts_for(COPILOT_EVAL_V3),
        evidence=evidence,
    )
    return result, served, evidence[0]


def v2_row(case_id: str, model: Any) -> dict[str, Any]:
    subset = replace(COPILOT_EVAL_V3, cases=(COPILOT_EVAL_V3.case(case_id),))
    report = harness.run(
        subset,
        lambda case: model,
        mode="replay",
        provider="scripted",
        model="scripted",
        recorded_on="2026-10-30",
        prompt=COPILOT_ANSWER_V3,
        served=True,
        model_level=True,
        model_level_v2=True,
    )
    [legacy] = report["model_level"]["cases"]
    [row] = report["model_level_v2"]["cases"]
    return {"legacy": legacy, **row}


# --- RecordingModel itself ----------------------------------------------------------------------


def test_a_failed_request_is_recorded_with_its_code_and_re_raised_unchanged() -> None:
    error = invalid()
    recorder = RecordingModel(ReplayThenRaise([turn("", JUNE_1)], error))
    recorder.complete(request())
    with pytest.raises(LlmInvalidResponseError) as raised:
        recorder.complete(request())
    assert raised.value is error  # the very error, not a copy or a wrapper
    assert recorder.failures == [RecordedFailure(request=2, code="LLM_INVALID_RESPONSE")]
    assert len(recorder.requests) == 2 and len(recorder.responses) == 1
    # No turn is fabricated for the failed request: the replay form holds the response only.
    assert recorder.turns() == (turn("", JUNE_1),)


def test_an_error_that_is_not_a_declared_llm_failure_is_not_recorded() -> None:
    recorder = RecordingModel(ReplayModel([]))
    with pytest.raises(ReplayError):
        recorder.complete(request())
    assert recorder.failures == []


def test_the_code_is_the_errors_own_attribute_not_its_message() -> None:
    error = LlmUnavailableError("LLM_INVALID_RESPONSE appears in this message only")
    recorder = RecordingModel(ReplayThenRaise([], error))
    with pytest.raises(LlmError):
        recorder.complete(request())
    assert recorder.failures == [RecordedFailure(request=1, code="LLM_UNAVAILABLE")]


# --- through the harness, on the case it belongs to -----------------------------------------------


@pytest.mark.parametrize(
    ("error", "code", "v2_outcome"),
    [
        (invalid(), "LLM_INVALID_RESPONSE", "invalid_response"),
        (LlmUnavailableError(), "LLM_UNAVAILABLE", "unavailable"),
    ],
)
def test_a_failure_before_the_first_response(error: LlmError, code: str, v2_outcome: str) -> None:
    result, served, seen = one_case("kpis_may_occupancy", ReplayThenRaise([], error))
    assert result.error_code == code  # raised from answer_turn: nothing partial existed
    assert (served.served_available, served.model_text) == (False, None)
    assert seen.model_failures == (RecordedFailure(request=1, code=code),)
    row = v2_row("kpis_may_occupancy", ReplayThenRaise([], error))
    assert (row["outcome"], row["model_error_code"]) == (v2_outcome, code)


@pytest.mark.parametrize(
    ("error", "code", "v2_outcome"),
    [
        (invalid(), "LLM_INVALID_RESPONSE", "invalid_response"),
        (LlmUnavailableError(), "LLM_UNAVAILABLE", "unavailable"),
    ],
)
def test_a_failure_after_a_successful_tool_round(
    error: LlmError, code: str, v2_outcome: str
) -> None:
    model = ReplayThenRaise([turn("", JUNE_1)], error)
    result, served, seen = one_case("inject_estimate", model)
    # The loop did not raise: the case's own records cannot say which code ended it ...
    assert (result.error_code, result.stop_reason) == (None, "model_failed")
    assert result.tools_called == ["get_demand_forecast"]  # the tool round is kept
    # ... but the evidence does, on the request that raised.
    assert seen.model_failures == (RecordedFailure(request=2, code=code),)
    assert [name for name, _ in seen.tool_outputs] == ["get_demand_forecast"]
    # No final response is fabricated: nothing was served but the empty partial.
    assert (served.served_answer, served.served_complete) == ("", False)
    row = v2_row("inject_estimate", ReplayThenRaise([turn("", JUNE_1)], error))
    assert (row["outcome"], row["model_error_code"]) == (v2_outcome, code)
    # v1 is unchanged: it still files both under `unavailable` (its documented reading).
    assert row["legacy"]["outcome"] == "unavailable"


def test_a_successful_multi_turn_exchange_records_no_failure() -> None:
    model = ReplayModel(
        [
            turn("", ("get_hotel_kpis", APRIL), ("get_hotel_kpis", MAY)),
            turn("Occupancy was 61.00% in April 2026 and 70.00% in May 2026."),
        ]
    )
    result, served, seen = one_case("compare_april_may_occupancy", model)
    assert seen.model_failures == ()
    assert (result.stop_reason, result.error_code) == ("completed", None)
    assert seen.requested_tools == ("get_hotel_kpis", "get_hotel_kpis")
    assert [name for name, _ in seen.tool_outputs] == ["get_hotel_kpis", "get_hotel_kpis"]
    assert served.answer_source == "model"


def test_a_model_failure_with_no_recorded_code_is_refused_not_guessed() -> None:
    from tests.evaluation import model_level_v2

    result, served, seen = one_case(
        "inject_estimate", ReplayThenRaise([turn("", JUNE_1)], invalid())
    )
    with pytest.raises(ValueError, match="no failure code was recorded"):
        model_level_v2.outcome(result, served, ())
    assert model_level_v2.outcome(result, served, seen.model_failures) == "invalid_response"
