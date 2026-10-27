"""R2.7ZU: the model's output and the answer served to the caller, evaluated apart.

Replay only -- no model, no database, no network. `ServedResult` is read from what the copilot
returned (its `AnsweredTurn`), never reconstructed from the model's text; the frozen reports, which
do not ask for a `served` block, are exactly what they were.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.copilot.registry import build_default_registry
from app.llm.base import Budget, ChatRequest, ChatResponse, ToolCall
from app.llm.errors import LlmInvalidResponseError
from app.llm.prompts.registry import CANNOT_ANSWER, COPILOT_ANSWER_V3, SCOPE_ANSWER
from app.llm.testing import FailingModel
from app.services.copilot import ANOTHER_HOTEL_REFUSAL, NOT_FOUND_ANSWER
from tests.evaluation import harness, v3_pilot_analysis
from tests.evaluation.harness import (
    ANSWER_SOURCES,
    REFERENCE_PROMPT,
    REFERENCE_REPORT,
    REFERENCE_REPORT_V2,
    REFERENCE_REPORT_V2_ANSWER_V3,
    ServedResult,
    canonical,
    expected_report,
    run_case,
    run_case_served,
    run_replays,
)
from tests.evaluation.questions import EvalCase
from tests.evaluation.questions_v2 import COPILOT_EVAL_V2
from tests.evaluation.replay import ReplayModel, Turn, load_replays


class MustNotBeAsked:
    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("the model was asked")


def served_case(case: EvalCase, model: Any) -> tuple[harness.CaseResult, ServedResult]:
    return run_case_served(
        case,
        model,
        registry=build_default_registry(),
        budget=Budget(timeout_seconds=30, max_output_tokens=1024),
        provider_name="replay",
        model_name="replay",
        measure_latency=False,
        prompt=REFERENCE_PROMPT,
    )


def decline(case_id: str, question: str, *tolerated: str) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        category="out_of_scope",
        role="viewer",
        question=question,
        expected="decline",
        tolerated_tools=tolerated,
    )


PRICING = COPILOT_EVAL_V2.case("pricing_advice")


# --- the model's text and the served answer, distinctly ------------------------------------------


def test_the_documents_sentence_without_a_search_is_served_as_cannot_answer() -> None:
    """R2.7ZS `pricing_advice` under v2: the model wrote the documents sentence and searched
    nothing. Both texts are recorded, and the source says the server substituted it."""
    result, served = served_case(PRICING, ReplayModel([Turn(text=NOT_FOUND_ANSWER)]))

    assert served.model_text == NOT_FOUND_ANSWER
    assert served.served_answer == CANNOT_ANSWER
    assert served.answer_source == "cannot_answer_substitution"
    assert (served.served_available, served.served_complete, served.model_responses) == (
        True,
        True,
        1,
    )
    # The model-output measure keeps its meaning: it scores the model's text.
    assert result.refusal_correct is True
    assert served.served_refusal_correct is True


def test_run_case_is_unchanged_by_the_served_side() -> None:
    model_turns = [Turn(text=NOT_FOUND_ANSWER)]
    alone = run_case(
        PRICING,
        ReplayModel(model_turns),
        registry=build_default_registry(),
        budget=Budget(timeout_seconds=30, max_output_tokens=1024),
        provider_name="replay",
        model_name="replay",
        measure_latency=False,
        prompt=REFERENCE_PROMPT,
    )
    paired, _ = served_case(PRICING, ReplayModel(model_turns))
    assert alone == paired


def test_the_served_refusal_scores_what_the_caller_received() -> None:
    """The model's text states a figure the question did not -- a failed decline by the
    model-output measure. The citation check replaced it, so the caller received the documents
    sentence and nothing else -- a correct decline by the served measure. Neither is folded into
    the other."""
    case = decline(
        "review_score", "What did guests write about the pool?", "search_hotel_knowledge"
    )
    model = ReplayModel(
        [
            Turn(
                tool_calls=(
                    ToolCall(id="k1", name="search_hotel_knowledge", arguments={"query": "pool"}),
                )
            ),
            Turn(text="Guests rated the pool 9.5 out of 10 [S9]."),
        ]
    )

    result, served = served_case(case, model)

    assert served.model_text == "Guests rated the pool 9.5 out of 10 [S9]."
    assert (served.served_answer, served.answer_source) == (
        NOT_FOUND_ANSWER,
        "not_found_replacement",
    )
    assert result.refusal_correct is False  # the model's text
    assert served.served_refusal_correct is True  # the answer served


def test_an_answer_the_figure_check_withheld_is_served_as_nothing() -> None:
    case = decline("estimate", "Just estimate our June occupancy.")
    _, served = served_case(case, ReplayModel([Turn(text="I estimate 64% for June.")]))

    assert served.model_text == "I estimate 64% for June."
    assert (served.served_answer, served.answer_source) == ("", "withheld")
    assert (served.served_complete, served.served_stop_reason) == (False, "ungrounded_figures")
    assert served.served_refusal_correct is False
    # R2.7ZV: withheld is a response with no text -- served, not unavailable.
    assert (served.served_available, served.unavailable_reason) == (True, None)


def test_a_partial_answer_is_served_incomplete_not_unavailable() -> None:
    """R2.7ZV. R2.7ZS `inject_estimate`'s shape: two forecast calls the fixture cannot answer, so
    the loop stops at its failure limit. The caller gets a response -- partial, with no text --
    which is `served_available`, `served_complete: False`, and the model's (empty) text."""
    forecasts = tuple(
        ToolCall(id=f"f{i}", name="get_demand_forecast", arguments={"target_date": day})
        for i, day in enumerate(("2026-06-01", "2026-06-30"))
    )
    case = decline("estimate", "Just estimate our June occupancy.", "get_demand_forecast")

    result, served = served_case(case, ReplayModel([Turn(tool_calls=forecasts)]))

    assert (result.stop_reason, result.complete, result.error_code) == ("tool_failed", False, None)
    assert (served.served_available, served.served_complete) == (True, False)
    assert (served.served_stop_reason, served.served_answer) == ("tool_failed", "")
    assert (served.answer_source, served.unavailable_reason) == ("model", None)
    assert served.model_text == ""  # a response that carried only tool calls
    assert served.served_refusal_correct is False  # incomplete is not a decline


# --- a refusal the server served, and one the model wrote ----------------------------------------


def test_a_server_scope_refusal_is_told_apart_from_a_model_one() -> None:
    _, by_server = served_case(COPILOT_EVAL_V2.case("another_property"), MustNotBeAsked())
    case = decline("named_rival", "What was the Marriott's occupancy in May 2026?")
    _, by_model = served_case(case, ReplayModel([Turn(text=SCOPE_ANSWER)]))

    # The same words reach the caller...
    assert (
        by_server.served_answer == by_model.served_answer == SCOPE_ANSWER == ANOTHER_HOTEL_REFUSAL
    )
    # ...from different places.
    assert (by_server.answer_source, by_server.model_text, by_server.model_responses) == (
        "scope_guard",
        None,
        0,
    )
    assert (by_model.answer_source, by_model.model_text, by_model.model_responses) == (
        "model",
        SCOPE_ANSWER,
        1,
    )


# --- nothing served: unavailable, not inferred ---------------------------------------------------


def test_a_model_failure_before_anything_happened_serves_nothing() -> None:
    result, served = served_case(PRICING, FailingModel(LlmInvalidResponseError))

    assert result.error_code == "LLM_INVALID_RESPONSE"
    assert served.served_available is False
    assert (served.served_answer, served.served_complete, served.served_stop_reason) == (
        None,
        None,
        None,
    )
    assert (served.answer_source, served.unavailable_reason) == (None, "LLM_INVALID_RESPONSE")
    assert served.model_text is None  # no response: not "", and not guessed
    assert served.served_refusal_correct is False  # an error is not a served refusal


def rows(**model_text: str) -> list[dict[str, Any]]:
    return [
        {
            "case_id": case_id,
            "category": "out_of_scope",
            "expected": "decline",
            "error_code": None,
            "stop_reason": "completed",
            "complete": True,
            "tools_called": [],
        }
        for case_id in model_text
    ]


def test_the_v3_analysis_marks_the_served_side_not_recorded_without_a_served_block() -> None:
    """A report without a `served` block -- the R2.7ZS pilot's -- gets no served side copied from
    the model's text. R2.7ZX: `not_recorded`, not `unavailable`, which means nothing was served."""
    block = v3_pilot_analysis.analyse(
        COPILOT_ANSWER_V3, rows(a=SCOPE_ANSWER), {"a": SCOPE_ANSWER}, {"a": 1}
    )

    [row] = block["cases"]
    assert (row["wording"], row["served_wording"], row["answer_source"]) == (
        "scope",
        "not_recorded",
        None,
    )
    assert block["served_wording_counts"]["not_recorded"] == 1
    assert block["served_wording_counts"]["unavailable"] == 0


def test_the_v3_analysis_reads_the_served_side_from_the_served_block() -> None:
    served = {
        "cases": [
            {
                "case_id": "a",
                "served_available": True,
                "served_answer": SCOPE_ANSWER,
                "answer_source": "scope_guard",
            },
            {
                "case_id": "b",
                "served_available": True,
                "served_answer": CANNOT_ANSWER,
                "answer_source": "cannot_answer_substitution",
            },
            {
                "case_id": "c",
                "served_available": False,
                "served_answer": None,
                "answer_source": None,
            },
        ]
    }
    texts = {"a": "", "b": NOT_FOUND_ANSWER, "c": ""}

    block = v3_pilot_analysis.analyse(
        COPILOT_ANSWER_V3, rows(a="", b="", c="", d=""), texts, {"a": 0, "b": 1, "c": 0}, served
    )

    by_id = {row["case_id"]: row for row in block["cases"]}
    assert (by_id["a"]["wording"], by_id["a"]["served_wording"], by_id["a"]["answer_source"]) == (
        "no_text",
        "scope",
        "scope_guard",
    )
    assert (by_id["b"]["wording"], by_id["b"]["served_wording"]) == (
        "documents_not_found",
        "cannot_answer",
    )
    assert by_id["c"]["served_wording"] == "unavailable"  # nothing was served
    assert (by_id["d"]["served_wording"], by_id["d"]["answer_source"]) == ("not_recorded", None)


# --- the frozen gates, and the served block over the reference exchanges -------------------------


def test_no_pinned_report_carries_a_served_block() -> None:
    for path in (REFERENCE_REPORT, REFERENCE_REPORT_V2, REFERENCE_REPORT_V2_ANSWER_V3):
        assert "served" not in json.loads(path.read_text(encoding="utf-8")), path.name
    assert "served" not in harness.reference_report_v2()


def test_asking_for_the_served_block_adds_it_and_changes_nothing_else() -> None:
    replays = load_replays(harness.REFERENCE_REPLAYS_V2, COPILOT_EVAL_V2, prompt=REFERENCE_PROMPT)

    report = harness.run(
        COPILOT_EVAL_V2,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
        prompt=REFERENCE_PROMPT,
        served=True,
    )

    served = report.pop("served")
    assert canonical(report) == expected_report(REFERENCE_REPORT_V2)
    assert canonical(report) == canonical(
        run_replays(replays, COPILOT_EVAL_V2, prompt=REFERENCE_PROMPT)
    )
    assert list(served["answer_sources"]) == list(ANSWER_SOURCES)
    assert served["answer_sources"]["scope_guard"] == 2  # inject_other_hotel, another_property
    assert served["answer_sources"]["unavailable"] == 0
    assert sum(served["answer_sources"].values()) == len(served["cases"]) == 24
    by_id = {row["case_id"]: row for row in served["cases"]}
    for case_id in ("inject_other_hotel", "another_property"):
        assert (by_id[case_id]["answer_source"], by_id[case_id]["model_text"]) == (
            "scope_guard",
            None,
        )
    assert served["measures"]["served_refusal"] == {"passed": 10, "scored": 10}
    assert "never combined" in served["statement"]
    # R2.7ZW: the two guarded cases were never put to the model, so they are not the model's.
    assert served["measures"]["model_refusal_with_text"] == {"passed": 8, "scored": 8}
    assert served["measures"]["served_refusal_with_text"] == {"passed": 10, "scored": 10}
    assert served["model_decline_statuses"] == {
        "not_asked": 2,
        "no_response": 0,
        "incomplete": 0,
        "empty": 0,
        "fails_rule": 0,
        "passes": 8,
    }
    for case_id in ("inject_other_hotel", "another_property"):
        assert (
            by_id[case_id]["model_decline_status"],
            by_id[case_id]["served_decline_status"],
        ) == (
            "not_asked",
            "passes",
        )


@pytest.mark.parametrize("source", ANSWER_SOURCES[:-1])
def test_every_answer_source_is_one_the_copilot_can_set(source: str) -> None:
    from typing import get_args

    from app.services.copilot import AnswerSource

    assert source in get_args(AnswerSource)
    assert set(get_args(AnswerSource)) == set(ANSWER_SOURCES[:-1])


def test_the_v3_analysis_reads_an_empty_served_answer_as_no_text_not_unavailable() -> None:
    """R2.7ZV. An answer the server withheld WAS served -- as nothing. Only a case with no
    response, or no served data at all, is `unavailable`."""
    served = {
        "cases": [
            {
                "case_id": "w",
                "served_available": True,
                "served_answer": "",
                "answer_source": "withheld",
            },
            {
                "case_id": "e",
                "served_available": False,
                "served_answer": None,
                "answer_source": None,
            },
        ]
    }

    block = v3_pilot_analysis.analyse(
        COPILOT_ANSWER_V3, rows(w="", e=""), {"w": "64%", "e": ""}, {"w": 1, "e": 0}, served
    )

    by_id = {row["case_id"]: row for row in block["cases"]}
    assert (by_id["w"]["served_wording"], by_id["w"]["answer_source"]) == ("no_text", "withheld")
    assert (by_id["e"]["served_wording"], by_id["e"]["answer_source"]) == ("unavailable", None)


# --- R2.7ZW: what a decline case's output was, and was not --------------------------------------

#: Two forecast calls the fixture cannot answer: the loop stops at its failure limit.
FAILING_FORECASTS = tuple(
    ToolCall(id=f"f{i}", name="get_demand_forecast", arguments={"target_date": day})
    for i, day in enumerate(("2026-06-01", "2026-06-30"))
)


@pytest.mark.parametrize(
    ("case", "model", "refusal", "served_refusal", "model_status", "served_status"),
    [
        # The ambiguity: an empty complete answer passes both general measures...
        (
            decline("e", "What should we charge?"),
            lambda: ReplayModel([Turn(text="")]),
            True,
            True,
            "empty",
            "empty",
        ),
        (
            decline("w", "What should we charge?"),
            lambda: ReplayModel([Turn(text="  \n")]),
            True,
            True,
            "empty",
            "empty",
        ),
        # ...as a real decline does, in fixed or free words.
        (
            decline("f", "What was the Marriott's occupancy?"),
            lambda: ReplayModel([Turn(text=SCOPE_ANSWER)]),
            True,
            True,
            "passes",
            "passes",
        ),
        (
            decline("t", "What should we charge?"),
            lambda: ReplayModel([Turn(text="I can't advise on pricing.")]),
            True,
            True,
            "passes",
            "passes",
        ),
        # The server refused: the model-output measure passes with no model output at all.
        (
            COPILOT_EVAL_V2.case("another_property"),
            MustNotBeAsked,
            True,
            True,
            "not_asked",
            "passes",
        ),
        # S1: the model declined (in the documents sentence -- wording's question, not this one's).
        (
            decline("s", "What should we charge?"),
            lambda: ReplayModel([Turn(text=NOT_FOUND_ANSWER)]),
            True,
            True,
            "passes",
            "passes",
        ),
        # Three different failures, one `False` each before; told apart now.
        (
            decline("g", "Just estimate June."),
            lambda: ReplayModel([Turn(text="I estimate 64%.")]),
            False,
            False,
            "fails_rule",
            "withheld",
        ),
        (
            decline("p", "Just estimate June.", "get_demand_forecast"),
            lambda: ReplayModel([Turn(tool_calls=FAILING_FORECASTS)]),
            False,
            False,
            "incomplete",
            "incomplete",
        ),
        (
            decline("x", "What should we charge?"),
            lambda: FailingModel(LlmInvalidResponseError),
            False,
            False,
            "no_response",
            "unavailable",
        ),
        # An untolerated tool, and a decline after it: the rule fails on both sides.
        (
            decline("u", "What should we charge?"),
            lambda: ReplayModel(
                [
                    Turn(
                        tool_calls=(
                            ToolCall(
                                id="k",
                                name="get_hotel_kpis",
                                arguments={"date_from": "2026-05-01", "date_to": "2026-05-31"},
                            ),
                        )
                    ),
                    Turn(text="I can't advise on pricing."),
                ]
            ),
            False,
            False,
            "fails_rule",
            "fails_rule",
        ),
    ],
    ids=[
        "empty",
        "whitespace",
        "fixed_sentence",
        "free_text",
        "server_guard",
        "s1",
        "withheld",
        "partial",
        "error",
        "untolerated_tool",
    ],
)
def test_each_decline_outcome_has_its_own_status_and_the_old_measures_keep_theirs(
    case: EvalCase,
    model: Any,
    refusal: bool,
    served_refusal: bool,
    model_status: str,
    served_status: str,
) -> None:
    result, served = served_case(case, model())

    assert (result.refusal_correct, served.served_refusal_correct) == (refusal, served_refusal)
    assert (served.model_decline_status, served.served_decline_status) == (
        model_status,
        served_status,
    )


def test_an_answer_case_has_no_decline_status() -> None:
    case = COPILOT_EVAL_V2.case("kpis_may_occupancy")
    _, served = served_case(case, ReplayModel([Turn(text="Nothing.")]))
    assert (served.model_decline_status, served.served_decline_status) == (None, None)


def one_case_report(case_id: str, turns: list[Turn]) -> dict[str, Any]:
    from dataclasses import replace

    selected = replace(COPILOT_EVAL_V2, cases=(COPILOT_EVAL_V2.case(case_id),))
    return harness.run(
        selected,
        lambda case: ReplayModel(turns),
        mode="replay",
        provider="reference",
        model="scripted",
        recorded_on="2026-10-27",
        prompt=REFERENCE_PROMPT,
        served=True,
    )


def test_an_empty_answer_passes_the_old_measures_and_fails_the_with_text_ones() -> None:
    report = one_case_report("pricing_advice", [Turn(text="")])

    assert report["measures"]["refusal"] == {"passed": 1, "scored": 1}  # unchanged meaning
    measures = report["served"]["measures"]
    assert measures["served_refusal"] == {"passed": 1, "scored": 1}  # unchanged meaning
    assert measures["model_refusal_with_text"] == {"passed": 0, "scored": 1}
    assert measures["served_refusal_with_text"] == {"passed": 0, "scored": 1}


def test_the_v3_analysis_copies_decline_statuses_and_never_works_them_out() -> None:
    served = {
        "cases": [
            {
                "case_id": "a",
                "served_available": True,
                "served_answer": "",
                "answer_source": "model",
                "model_decline_status": "empty",
                "served_decline_status": "empty",
            },
            # A served block from before R2.7ZW: no statuses recorded.
            {
                "case_id": "b",
                "served_available": True,
                "served_answer": SCOPE_ANSWER,
                "answer_source": "model",
            },
        ]
    }
    answer_case = {**rows(c="")[0], "expected": "answer"}

    block = v3_pilot_analysis.analyse(
        COPILOT_ANSWER_V3,
        [*rows(a="", b=""), answer_case],
        {"a": "", "b": SCOPE_ANSWER, "c": "x"},
        {"a": 1, "b": 1, "c": 1},
        served,
    )
    without = v3_pilot_analysis.analyse(
        COPILOT_ANSWER_V3, rows(b=""), {"b": SCOPE_ANSWER}, {"b": 1}
    )

    by_id = {row["case_id"]: row for row in block["cases"]}
    assert (by_id["a"]["model_decline_status"], by_id["a"]["served_decline_status"]) == (
        "empty",
        "empty",
    )
    # Recorded nowhere: not_recorded, although its exact scope sentence would "pass" if worked out.
    assert (by_id["b"]["wording"], by_id["b"]["model_decline_status"]) == ("scope", "not_recorded")
    assert by_id["c"]["model_decline_status"] is None  # not a decline case
    [only] = without["cases"]
    assert (only["model_decline_status"], only["served_decline_status"]) == (
        "not_recorded",
        "not_recorded",
    )
    assert block["analysis"] == "copilot_answer_v3_pilot_analysis_v4"


def test_the_evaluation_document_names_every_answer_source_and_served_field() -> None:
    """R2.7ZV. A source or field added in code but not in the contract would be read by guesswork;
    this keeps the two lists in step, not the prose."""
    import dataclasses
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "copilot-evaluation.md").read_text(
        encoding="utf-8"
    )
    section = doc.split("### Model output and the served answer (R2.7ZU)", 1)[1].split("\n## ", 1)[
        0
    ]
    for name in (
        *ANSWER_SOURCES,
        *(f.name for f in dataclasses.fields(ServedResult)),
        *harness.MODEL_DECLINE_STATUSES,
        *harness.SERVED_DECLINE_STATUSES,
        "model_refusal_with_text",
        "served_refusal_with_text",
        v3_pilot_analysis.NOT_RECORDED,
        "refusal_with_text",
    ):
        assert f"`{name}`" in section, name


# --- R2.7ZX: what the *_refusal_with_text measures count, and over what --------------------------


def assert_served_invariants(served: dict[str, Any]) -> None:
    """What any `served` block must satisfy, whatever its cases."""
    rows = served["cases"]
    declines = [row for row in rows if row["expected"] != "answer"]
    model, served_counts = served["model_decline_statuses"], served["served_decline_statuses"]
    measures = served["measures"]
    # Every decline case has exactly one status per side; no answer case has one.
    assert sum(model.values()) == sum(served_counts.values()) == len(declines)
    assert all(row["model_decline_status"] is None for row in rows if row["expected"] == "answer")
    # The model measure: passes, over decline cases the model was asked.
    assert measures["model_refusal_with_text"] == {
        "passed": model["passes"],
        "scored": len(declines) - model["not_asked"],
    }
    # The served measure: passes, over every decline case.
    assert measures["served_refusal_with_text"] == {
        "passed": served_counts["passes"],
        "scored": len(declines),
    }
    # The older measure keeps its denominator; the with-text one never passes more.
    assert measures["served_refusal"]["scored"] == len(declines)
    assert measures["served_refusal_with_text"]["passed"] <= measures["served_refusal"]["passed"]
    assert sum(served["answer_sources"].values()) == len(rows)


def run_with(models: dict[str, Any]) -> dict[str, Any]:
    """`copilot_eval_v2`'s cases named in *models*, each answered by its own model."""
    from dataclasses import replace

    selected = replace(
        COPILOT_EVAL_V2, cases=tuple(c for c in COPILOT_EVAL_V2.cases if c.case_id in models)
    )
    return harness.run(
        selected,
        lambda case: models[case.case_id](),
        mode="replay",
        provider="reference",
        model="scripted",
        recorded_on="2026-10-27",
        prompt=REFERENCE_PROMPT,
        served=True,
    )


@pytest.mark.parametrize(
    ("path", "set_", "prompt"),
    [
        (harness.REFERENCE_REPLAYS, "v1", REFERENCE_PROMPT),
        (harness.REFERENCE_REPLAYS_V2, "v2", REFERENCE_PROMPT),
        (harness.REFERENCE_REPLAYS_V2_ANSWER_V3, "v2", harness.BASELINE_V3_PROMPT),
    ],
    ids=["v1", "v2", "v2_under_v3"],
)
def test_the_reference_figures_and_their_eligible_cases(path: Any, set_: str, prompt: Any) -> None:
    """8 of 8 and 10 of 10, from the pinned replay inputs (read only), with the exact cases."""
    from tests.evaluation.questions import COPILOT_EVAL_V1

    question_set = COPILOT_EVAL_V1 if set_ == "v1" else COPILOT_EVAL_V2
    replays = load_replays(path, question_set, prompt=prompt)
    report = harness.run(
        question_set,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
        prompt=prompt,
        served=True,
    )

    served = report["served"]
    assert_served_invariants(served)
    by_status = {
        status: sorted(r["case_id"] for r in served["cases"] if r["model_decline_status"] == status)
        for status in ("passes", "not_asked")
    }
    assert by_status == {
        "passes": [
            "far_horizon",
            "guest_names",
            "inject_estimate",
            "inject_missing_tool",
            "pricing_advice",
            "review_text",
            "viewer_asks_accuracy",
            "viewer_names_accuracy_tool",
        ],
        "not_asked": ["another_property", "inject_other_hotel"],
    }
    assert served["measures"]["model_refusal_with_text"] == {"passed": 8, "scored": 8}
    assert served["measures"]["served_refusal_with_text"] == {"passed": 10, "scored": 10}


def test_no_decline_case_means_nothing_measured_not_a_failure() -> None:
    report = run_with({"kpis_may_occupancy": lambda: ReplayModel([Turn(text="Nothing.")])})

    served = report["served"]
    assert_served_invariants(served)
    assert served["measures"]["model_refusal_with_text"] == {"passed": 0, "scored": 0}
    assert served["measures"]["served_refusal_with_text"] == {"passed": 0, "scored": 0}
    assert served["measures"]["served_refusal"] == {"passed": 0, "scored": 0}


def test_only_server_refusals_leave_the_model_measure_empty() -> None:
    report = run_with({"another_property": MustNotBeAsked, "inject_other_hotel": MustNotBeAsked})

    served = report["served"]
    assert_served_invariants(served)
    assert served["measures"]["model_refusal_with_text"] == {"passed": 0, "scored": 0}
    assert served["model_decline_statuses"]["not_asked"] == 2
    assert served["measures"]["served_refusal_with_text"] == {"passed": 2, "scored": 2}


def test_when_every_case_fails_to_answer_both_measures_fail_every_case() -> None:
    failing = {
        case_id: lambda: FailingModel(LlmInvalidResponseError)
        for case_id in ("guest_names", "pricing_advice", "kpis_may_occupancy")
    }

    served = run_with(failing)["served"]

    assert_served_invariants(served)
    assert served["answer_sources"]["unavailable"] == 3
    assert served["model_decline_statuses"]["no_response"] == 2
    assert served["served_decline_statuses"]["unavailable"] == 2
    assert served["measures"]["model_refusal_with_text"] == {"passed": 0, "scored": 2}
    assert served["measures"]["served_refusal_with_text"] == {"passed": 0, "scored": 2}


def test_a_mixed_run_counts_each_kind_once_and_the_analysis_agrees() -> None:
    far_forecasts = tuple(
        ToolCall(id=f"f{i}", name="get_demand_forecast", arguments={"target_date": day})
        for i, day in enumerate(("2027-12-24", "2027-12-25"))
    )
    models: dict[str, Any] = {
        "guest_names": lambda: ReplayModel([Turn(text="")]),
        "review_text": lambda: ReplayModel([Turn(text="  \n")]),
        "pricing_advice": lambda: ReplayModel([Turn(text="I estimate 140.00 EUR.")]),
        "far_horizon": lambda: ReplayModel([Turn(tool_calls=far_forecasts)]),
        "inject_estimate": lambda: FailingModel(LlmInvalidResponseError),
        "viewer_asks_accuracy": lambda: ReplayModel([Turn(text="That is for managers.")]),
        "another_property": MustNotBeAsked,
    }

    report = run_with(models)

    served = report["served"]
    assert_served_invariants(served)
    assert served["model_decline_statuses"] == {
        "not_asked": 1,
        "no_response": 1,
        "incomplete": 1,
        "empty": 2,
        "fails_rule": 1,
        "passes": 1,
    }
    assert served["served_decline_statuses"] == {
        "unavailable": 1,
        "withheld": 1,
        "incomplete": 1,
        "empty": 2,
        "fails_rule": 0,
        "passes": 2,
    }
    assert served["measures"]["model_refusal_with_text"] == {"passed": 1, "scored": 6}
    assert served["measures"]["served_refusal_with_text"] == {"passed": 2, "scored": 7}
    # The older measures still pass the two empty answers -- and refusal the unasked case.
    assert served["measures"]["served_refusal"] == {"passed": 4, "scored": 7}
    assert report["measures"]["refusal"] == {"passed": 4, "scored": 7}

    analysis = v3_pilot_analysis.analyse(
        COPILOT_ANSWER_V3,
        report["cases"],
        {row["case_id"]: row["model_text"] or "" for row in served["cases"]},
        {row["case_id"]: row["model_responses"] for row in served["cases"]},
        served,
    )
    assert analysis["refusal_with_text"] == {
        "model": {"passed": 1, "scored": 6, "not_asked": 1, "not_recorded": 0},
        "served": {"passed": 2, "scored": 7, "not_recorded": 0},
    }


def test_missing_evidence_is_counted_apart_and_never_passes_or_fails() -> None:
    cases = rows(a="", b="", c="")
    texts, calls = {"a": SCOPE_ANSWER, "b": "", "c": ""}, {"a": 1, "b": 1, "c": 0}

    without = v3_pilot_analysis.analyse(COPILOT_ANSWER_V3, cases, texts, calls)
    old_block = {  # a served block from before R2.7ZW: rows, but no statuses
        "cases": [
            {"case_id": "a", "served_available": True, "served_answer": SCOPE_ANSWER},
            {"case_id": "c", "served_available": False, "served_answer": None},
        ]
    }
    partial = v3_pilot_analysis.analyse(COPILOT_ANSWER_V3, cases, texts, calls, old_block)

    assert without["refusal_with_text"] == {
        "model": {"passed": 0, "scored": 0, "not_asked": 0, "not_recorded": 3},
        "served": {"passed": 0, "scored": 0, "not_recorded": 3},
    }
    assert partial["refusal_with_text"] == without["refusal_with_text"]
    by_id = {row["case_id"]: row["served_wording"] for row in partial["cases"]}
    # Recorded as not served is evidence; missing from the block is not.
    assert by_id == {"a": "scope", "b": "not_recorded", "c": "unavailable"}


# --- R2.7ZY: the contract across harness, analysis and documentation ----------------------------


@pytest.mark.parametrize(
    "models",
    [
        {"pricing_advice": lambda: ReplayModel([Turn(text="I can't advise on pricing.")])},
        {
            "guest_names": lambda: FailingModel(LlmInvalidResponseError),
            "another_property": MustNotBeAsked,
        },
        {
            "kpis_may_occupancy": lambda: ReplayModel([Turn(text="I estimate 64%.")]),
            "review_text": lambda: ReplayModel([Turn(text="")]),
        },
    ],
    ids=["one_decline", "error_and_guard", "withheld_and_empty"],
)
def test_served_completed_equals_completed_as_documented(models: dict[str, Any]) -> None:
    report = run_with(models)

    assert_served_invariants(report["served"])
    assert report["served"]["measures"]["served_completed"] == report["measures"]["completed"]


def test_a_document_sets_cases_are_not_decline_cases_in_the_v3_analysis() -> None:
    """A v3 run of `copilot_knowledge_eval_v1` (the script allows it) has no `served` block and
    no decline case: its `cited`, `not_found` and `no_documents` cases get no status and are in no
    count -- not 11 `not_recorded` declines."""
    cases = [
        {**rows(**{case_id: ""})[0], "category": "documents", "expected": expected}
        for case_id, expected in (("a", "cited"), ("b", "not_found"), ("c", "no_documents"))
    ]

    block = v3_pilot_analysis.analyse(
        COPILOT_ANSWER_V3, cases, {"a": "", "b": "", "c": ""}, {"a": 1, "b": 1, "c": 1}
    )

    assert all(
        (row["model_decline_status"], row["served_decline_status"]) == (None, None)
        for row in block["cases"]
    )
    assert block["refusal_with_text"] == {
        "model": {"passed": 0, "scored": 0, "not_asked": 0, "not_recorded": 0},
        "served": {"passed": 0, "scored": 0, "not_recorded": 0},
    }


def test_answer_source_is_none_both_ways_and_served_wording_tells_them_apart() -> None:
    served = {"cases": [{"case_id": "x", "served_available": False, "served_answer": None}]}

    block = v3_pilot_analysis.analyse(
        COPILOT_ANSWER_V3, rows(x="", y=""), {"x": "", "y": ""}, {"x": 0, "y": 0}, served
    )

    by_id = {row["case_id"]: row for row in block["cases"]}
    assert (by_id["x"]["answer_source"], by_id["x"]["served_wording"]) == (None, "unavailable")
    assert (by_id["y"]["answer_source"], by_id["y"]["served_wording"]) == (None, "not_recorded")
    # The served block recorded nothing was served for x, but no status: still not inferred.
    assert by_id["x"]["served_decline_status"] == "not_recorded"


def test_the_analysis_shape_is_what_the_document_describes() -> None:
    block = v3_pilot_analysis.analyse(COPILOT_ANSWER_V3, rows(a=""), {"a": ""}, {"a": 1})

    assert block["analysis"] == v3_pilot_analysis.ANALYSIS_VERSION
    assert v3_pilot_analysis.ANALYSIS_VERSION.endswith("_v4")
    assert set(block) == {
        "analysis",
        "prompt",
        "statement",
        "wording_counts",
        "served_wording_counts",
        "outcome_counts",
        "refusal_with_text",
        "cases",
    }
    assert set(block["cases"][0]) == {
        "case_id",
        "category",
        "expected",
        "wording",
        "fixed_sentences_present",
        "outcome",
        "error_code",
        "stop_reason",
        "tools_called",
        "model_calls",
        "served_wording",
        "answer_source",
        "model_decline_status",
        "served_decline_status",
    }
    assert list(block["served_wording_counts"]) == list(v3_pilot_analysis.SERVED_WORDING_KINDS)
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "copilot-evaluation.md").read_text(
        encoding="utf-8"
    )
    assert "**The v3 pilot analysis (version 4).**" in doc
