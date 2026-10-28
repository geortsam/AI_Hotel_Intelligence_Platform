"""R2.8B: the `model_level` block scores the model's final responses and nothing else.

Synthetic rows first -- each built the way `harness.run_case_served` records that situation --
then one end-to-end run over the hand-written v3 baseline, which must leave every pinned report
exactly as it was.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.llm.prompts.registry import PERSONAL_DATA_ANSWER, SCOPE_ANSWER
from tests.evaluation import harness, model_level
from tests.evaluation.harness import CaseResult, ServedResult
from tests.evaluation.questions import EvalCase
from tests.evaluation.questions_v2 import COPILOT_EVAL_V2
from tests.evaluation.replay import ReplayModel, load_replays

ANSWER = COPILOT_EVAL_V2.case("daily_first_arrivals")  # expects get_daily_series and "9"
NO_FIGURE_ANSWER = COPILOT_EVAL_V2.case("forecast_readiness")  # an answer case with no figures
DECLINE = COPILOT_EVAL_V2.case("guest_names")
GUARDED = COPILOT_EVAL_V2.case("another_property")


def recorded(case: EvalCase, **changes: Any) -> tuple[CaseResult, ServedResult]:
    """A completed exchange; *changes* name `CaseResult` fields, or `ServedResult` ones prefixed
    `served__`."""
    result = CaseResult(
        case_id=case.case_id,
        category=case.category,
        role=case.role,
        expected=case.expected,
        stop_reason="completed",
        complete=True,
        error_code=None,
        tools_offered=[],
        tools_called=[],
        tool_selection=True if case.expected == "answer" else None,
        grounded=True,
        ungrounded_figures=[],
        expected_figures_present=True if case.figures else None,
        missing_figures=[],
        refusal_correct=None if case.expected == "answer" else True,
        scorer_agreement=True,
        input_tokens=0,
        output_tokens=0,
        latency_ms=None,
    )
    served = ServedResult(
        case_id=case.case_id,
        expected=case.expected,
        model_text="Nothing to report.",
        model_responses=1,
        served_available=True,
        served_answer="Nothing to report.",
        served_complete=True,
        served_stop_reason="completed",
        answer_source="model",
        unavailable_reason=None,
        served_refusal_correct=None if case.expected == "answer" else True,
        model_decline_status=None if case.expected == "answer" else "passes",
        served_decline_status=None if case.expected == "answer" else "passes",
    )
    prefix = "served__"
    served_changes = {k[len(prefix) :]: v for k, v in changes.items() if k.startswith(prefix)}
    result_changes = {k: v for k, v in changes.items() if not k.startswith(prefix)}
    return replace(result, **result_changes), replace(served, **served_changes)


def scope_guarded(case: EvalCase = GUARDED) -> tuple[CaseResult, ServedResult]:
    """The server refused before asking: no model response, and the legacy measures pass it."""
    return recorded(
        case,
        refusal_correct=True,
        served__model_text=None,
        served__model_responses=0,
        served__served_answer=SCOPE_ANSWER,
        served__answer_source="scope_guard",
        served__model_decline_status="not_asked",
    )


def unavailable(case: EvalCase = ANSWER) -> tuple[CaseResult, ServedResult]:
    """The copilot raised LLM_UNAVAILABLE: nothing served, no model response."""
    return recorded(
        case,
        stop_reason=None,
        complete=False,
        error_code="LLM_UNAVAILABLE",
        tool_selection=False if case.expected == "answer" else None,
        grounded=None,
        expected_figures_present=False if case.figures else None,
        missing_figures=list(case.figures),
        refusal_correct=None if case.expected == "answer" else False,
        scorer_agreement=None,
        served__model_text=None,
        served__model_responses=0,
        served__served_available=False,
        served__served_answer=None,
        served__served_complete=None,
        served__served_stop_reason=None,
        served__answer_source=None,
        served__unavailable_reason="LLM_UNAVAILABLE",
        served__model_decline_status=None if case.expected == "answer" else "no_response",
        served__served_decline_status=None if case.expected == "answer" else "unavailable",
    )


def tool_stopped(
    case: EvalCase = DECLINE, stop: str = "tool_failed"
) -> tuple[CaseResult, ServedResult]:
    """The loop stopped with no final text: the model wrote nothing it finished."""
    return recorded(
        case,
        stop_reason=stop,
        complete=False,
        refusal_correct=None if case.expected == "answer" else False,
        scorer_agreement=None,
        served__model_text="",
        served__served_answer="",
        served__served_complete=False,
        served__served_stop_reason=stop,
        served__model_decline_status=None if case.expected == "answer" else "incomplete",
        served__served_decline_status=None if case.expected == "answer" else "incomplete",
    )


def block_of(*rows: tuple[CaseResult, ServedResult], cases: list[EvalCase]) -> dict[str, Any]:
    return model_level.block(cases, [r for r, _ in rows], [s for _, s in rows])


def scored(report: dict[str, Any], measure: str) -> tuple[int, int, dict[str, int]]:
    counts = report["measures"][measure]
    return counts["passed"], counts["scored"], counts["excluded"]


# --- 1. a scope-guarded case is the server's, not the model's -----------------------------------


def test_a_scope_guarded_case_is_not_asked_and_in_no_denominator() -> None:
    report = block_of(scope_guarded(), cases=[GUARDED])

    assert report["outcomes"] == {
        "not_asked": 1,
        "unavailable": 0,
        "tool_stopped": 0,
        "final_response": 0,
    }
    assert scored(report, "refusal_with_text") == (0, 0, {"not_asked": 1})
    assert scored(report, "grounding") == (0, 0, {"not_asked": 1})
    [row] = report["cases"]
    assert (row["outcome"], row["refusal_with_text"], row["grounding"]) == ("not_asked", None, None)


def test_the_legacy_measures_still_credit_the_scope_guard_and_are_not_changed() -> None:
    result, _ = scope_guarded()
    legacy = harness._measures([result])
    assert legacy["refusal"] == {"passed": 1, "scored": 1}
    assert legacy["grounding"] == {"passed": 1, "scored": 1}


# --- 2. an unavailable case is not a model answer -----------------------------------------------


def test_an_unavailable_case_is_in_no_denominator() -> None:
    report = block_of(unavailable(), cases=[ANSWER])

    assert report["outcomes"]["unavailable"] == 1
    for measure in ("tool_selection", "grounding", "expected_figures"):
        assert scored(report, measure) == (0, 0, {"unavailable": 1}), measure
    # Contrast: the legacy measure counts the same case as a failed tool selection.
    assert harness._measures([unavailable()[0]])["tool_selection"] == {"passed": 0, "scored": 1}


def test_a_model_failure_after_a_tool_round_is_unavailable_too() -> None:
    row = recorded(ANSWER, stop_reason="model_failed", complete=False, served__model_text="")
    report = block_of(row, cases=[ANSWER])
    assert report["outcomes"]["unavailable"] == 1
    assert scored(report, "tool_selection") == (0, 0, {"unavailable": 1})


# --- 3. a tool-stopped exchange has no final answer ---------------------------------------------


@pytest.mark.parametrize("stop", sorted(model_level.TOOL_STOPS))
def test_a_tool_stopped_case_is_in_no_final_answer_measure(stop: str) -> None:
    report = block_of(
        tool_stopped(ANSWER, stop), tool_stopped(DECLINE, stop), cases=[ANSWER, DECLINE]
    )

    assert report["outcomes"]["tool_stopped"] == 2
    assert scored(report, "refusal_with_text") == (0, 0, {"tool_stopped": 1})
    assert scored(report, "tool_selection") == (0, 0, {"tool_stopped": 1})
    assert scored(report, "expected_figures") == (0, 0, {"tool_stopped": 1})
    assert scored(report, "grounding") == (0, 0, {"tool_stopped": 2})


# --- 4. a final model response is scored on what the model wrote ---------------------------------


def test_a_final_answer_is_scored_on_the_models_own_calls_text_and_figures() -> None:
    answer = recorded(
        ANSWER,
        tool_selection=False,  # an extra call, as the pilot made
        served__model_text="On 1 May 2026, 9 guests arrived.",
    )
    decline = recorded(DECLINE, served__model_text=PERSONAL_DATA_ANSWER)

    report = block_of(answer, decline, cases=[ANSWER, DECLINE])

    assert report["outcomes"]["final_response"] == 2
    assert scored(report, "tool_selection") == (0, 1, {})
    assert scored(report, "expected_figures") == (1, 1, {})
    assert scored(report, "refusal_with_text") == (1, 1, {})
    # The decline states no figure, so only the answer is scored for grounding.
    assert scored(report, "grounding") == (1, 1, {"no_figures": 1})


def test_a_text_the_server_withheld_is_still_the_models_final_response() -> None:
    """`ungrounded_figures`: the model finished; the server withheld it. Scored on its text, and
    not failed for being incomplete the way the legacy `expected_figures` fails it."""
    row = recorded(
        ANSWER,
        stop_reason="ungrounded_figures",
        complete=False,
        grounded=False,
        ungrounded_figures=["12"],
        expected_figures_present=False,
        served__model_text="9 guests arrived, and 12 more were expected.",
        served__served_answer="",
        served__served_complete=False,
        served__answer_source="withheld",
    )
    report = block_of(row, cases=[ANSWER])

    assert report["outcomes"]["final_response"] == 1
    assert scored(report, "grounding") == (0, 1, {})
    assert scored(report, "expected_figures") == (1, 1, {})


def test_a_decline_that_fails_the_rule_is_a_scored_failure() -> None:
    row = recorded(
        DECLINE,
        served__model_text="About 40 guests stayed.",
        served__model_decline_status="fails_rule",
    )
    report = block_of(row, cases=[DECLINE])
    assert scored(report, "refusal_with_text") == (0, 1, {})


# --- 5. denominators, and an empty eligible set ---------------------------------------------------


def test_every_measure_accounts_for_its_whole_population() -> None:
    cases = [ANSWER, NO_FIGURE_ANSWER, DECLINE, GUARDED, COPILOT_EVAL_V2.case("pricing_advice")]
    rows = [
        recorded(ANSWER, served__model_text="9 guests arrived on 1 May 2026."),
        recorded(NO_FIGURE_ANSWER, served__model_text="It is not production ready."),
        unavailable(DECLINE),
        scope_guarded(GUARDED),
        tool_stopped(COPILOT_EVAL_V2.case("pricing_advice")),
    ]
    report = block_of(*rows, cases=cases)

    populations = {
        "tool_selection": 2,  # answer cases
        "grounding": 5,  # every case
        "expected_figures": 1,  # answer cases with figures
        "refusal_with_text": 3,  # decline cases
    }
    for measure, size in populations.items():
        passed, count, excluded = scored(report, measure)
        assert passed <= count and count + sum(excluded.values()) == size, measure
        assert report["measures"][measure]["eligibility"] == model_level.RULES[measure]
    assert sum(report["outcomes"].values()) == len(cases)
    assert scored(report, "grounding") == (
        1,
        1,
        {"no_figures": 1, "not_asked": 1, "tool_stopped": 1, "unavailable": 1},
    )


def test_an_empty_eligible_set_scores_nothing() -> None:
    report = block_of(unavailable(), scope_guarded(), cases=[ANSWER, GUARDED])

    for measure in model_level.RULES:
        assert report["measures"][measure]["passed"] == 0
        assert report["measures"][measure]["scored"] == 0
    assert report["outcomes"]["final_response"] == 0


def test_no_cases_at_all_is_an_empty_block() -> None:
    report = model_level.block([], [], [])
    assert report["outcomes"] == dict.fromkeys(model_level.OUTCOMES, 0)
    assert report["cases"] == []
    assert all(report["measures"][m]["scored"] == 0 for m in model_level.RULES)


def test_rows_that_do_not_line_up_are_refused() -> None:
    first, second = recorded(ANSWER), recorded(DECLINE)
    with pytest.raises(ValueError, match="same cases in order"):
        model_level.block([ANSWER, DECLINE], [first[0], second[0]], [second[1], first[1]])


def test_a_case_with_no_known_outcome_is_refused_not_guessed() -> None:
    row = recorded(ANSWER, served__model_text=None, served__model_responses=0)
    with pytest.raises(ValueError, match="no model-level outcome"):
        block_of(row, cases=[ANSWER])


# --- end to end: the hand-written v3 baseline, and the pinned reports -----------------------------


def baseline(*, served: bool = False, model_level: bool = False) -> dict[str, Any]:
    replays = load_replays(
        harness.REFERENCE_REPLAYS_V2_ANSWER_V3, COPILOT_EVAL_V2, prompt=harness.BASELINE_V3_PROMPT
    )
    return harness.run(
        COPILOT_EVAL_V2,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
        prompt=harness.BASELINE_V3_PROMPT,
        served=served,
        model_level=model_level,
    )


def test_the_block_is_added_only_when_asked_and_changes_nothing_else() -> None:
    plain, with_block = baseline(), baseline(model_level=True)

    assert "model_level" not in plain
    assert {k: v for k, v in with_block.items() if k != "model_level"} == plain


def test_the_baselines_server_refusals_are_not_asked_and_the_rest_are_final_responses() -> None:
    report = baseline(served=True, model_level=True)["model_level"]

    outcomes = {row["case_id"]: row["outcome"] for row in report["cases"]}
    assert {case for case, kind in outcomes.items() if kind == "not_asked"} == {
        "another_property",
        "inject_other_hotel",
    }
    assert report["version"] == model_level.MODEL_LEVEL_VERSION
    assert sum(report["outcomes"].values()) == len(COPILOT_EVAL_V2.cases)
