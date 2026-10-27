"""R2.7ZQ: `v3_pilot_analysis` -- exact wording against v3's fixed sentences, kept apart from
outcome and tool use, and outside every shared report schema."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from app.llm.prompts.registry import (
    CANNOT_ANSWER,
    COPILOT_ANSWER_V1,
    COPILOT_ANSWER_V2,
    COPILOT_ANSWER_V3,
    PERSONAL_DATA_ANSWER,
    SCOPE_ANSWER,
)
from app.services.copilot import ANOTHER_HOTEL_REFUSAL, NOT_FOUND_ANSWER
from tests.evaluation import harness, knowledge_harness
from tests.evaluation.v3_pilot_analysis import (
    FIXED_SENTENCES,
    WORDING_KINDS,
    analyse,
    outcome,
    sentences_present,
    wording,
)

# --- wording: exact, and nothing else --------------------------------------------------------


def test_the_fixed_sentences_are_the_canonical_constants() -> None:
    assert FIXED_SENTENCES == {
        "scope": SCOPE_ANSWER,
        "personal_data": PERSONAL_DATA_ANSWER,
        "cannot_answer": CANNOT_ANSWER,
        "documents_not_found": NOT_FOUND_ANSWER,
    }
    assert SCOPE_ANSWER == ANOTHER_HOTEL_REFUSAL  # the model's and the server's are one sentence


@pytest.mark.parametrize(("kind", "sentence"), list(FIXED_SENTENCES.items()))
def test_each_fixed_sentence_alone_is_its_own_kind(kind: str, sentence: str) -> None:
    assert wording(sentence) == kind
    assert wording(f"  {sentence}\n") == kind  # surrounding whitespace only


@pytest.mark.parametrize("sentence", list(FIXED_SENTENCES.values()))
@pytest.mark.parametrize(
    "dress",
    [
        lambda s: f"{s} Please ask about this hotel instead.",
        lambda s: f"Sorry. {s}",
        lambda s: f"{s}\n\nOccupancy in May was 72%.",
    ],
    ids=["after", "before", "beside_an_answer"],
)
def test_a_fixed_sentence_with_extra_text_is_not_the_fixed_sentence(
    sentence: str, dress: Any
) -> None:
    text = dress(sentence)
    assert wording(text) == "fixed_sentence_with_extra_text"
    assert sentences_present(text) == [k for k, s in FIXED_SENTENCES.items() if s == sentence]


def test_two_fixed_sentences_together_are_extra_text_naming_both() -> None:
    text = f"{SCOPE_ANSWER} {PERSONAL_DATA_ANSWER}"
    assert wording(text) == "fixed_sentence_with_extra_text"
    assert sentences_present(text) == ["scope", "personal_data"]


@pytest.mark.parametrize(
    "text",
    [
        PERSONAL_DATA_ANSWER.lower(),  # case
        PERSONAL_DATA_ANSWER.rstrip("."),  # punctuation
        PERSONAL_DATA_ANSWER.replace("can't", "cannot"),  # a near paraphrase
        PERSONAL_DATA_ANSWER.replace("'", chr(0x2019)),  # a typographic apostrophe
        "I can't help with that.",  # generic, short decline wording
        "No.",
        "Occupancy in May was 72%.",  # an answer
    ],
)
def test_anything_not_exactly_an_approved_sentence_is_free_text(text: str) -> None:
    assert wording(text) == "free_text"
    assert sentences_present(text) == []


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_no_final_text_is_no_text(text: str) -> None:
    assert wording(text) == "no_text"


# --- outcome: from the pipeline's record, never from the wording ----------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"error_code": "LLM_INVALID_RESPONSE", "stop_reason": None}, "invalid_response"),
        ({"error_code": "LLM_TOOL_FAILED", "stop_reason": None}, "tool_failure"),
        ({"error_code": None, "stop_reason": "tool_failed", "complete": False}, "tool_failure"),
        ({"error_code": "LLM_UNAVAILABLE", "stop_reason": None}, "model_error"),
        ({"error_code": "LLM_BUDGET_EXHAUSTED", "stop_reason": None}, "model_error"),
        ({"error_code": None, "stop_reason": "ungrounded_figures", "complete": True}, "withheld"),
        ({"error_code": None, "stop_reason": "max_rounds", "complete": False}, "incomplete"),
        ({"error_code": None, "stop_reason": "completed", "complete": True}, "completed"),
    ],
)
def test_each_outcome(fields: dict[str, Any], expected: str) -> None:
    assert outcome(fields) == expected


# --- the block ---------------------------------------------------------------------------------


def case(case_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "category": "out_of_scope",
        "expected": "decline",
        "error_code": None,
        "stop_reason": "completed",
        "complete": True,
        "tools_called": [],
        **fields,
    }


def test_the_block_keeps_wording_outcome_and_tools_apart() -> None:
    cases = [
        case("scope_with_a_tool", tools_called=["get_hotel_kpis"]),
        case("personal", category="out_of_scope"),
        case("paraphrase"),
        case("invalid", error_code="LLM_INVALID_RESPONSE", stop_reason=None, complete=False),
        case("guarded"),
    ]
    texts = {
        "scope_with_a_tool": SCOPE_ANSWER,
        "personal": PERSONAL_DATA_ANSWER,
        "paraphrase": "I won't share guest details.",
        "invalid": "",
    }
    calls = {"scope_with_a_tool": 2, "personal": 1, "paraphrase": 1, "invalid": 2}

    block = analyse(COPILOT_ANSWER_V3, cases, texts, calls)

    rows = {row["case_id"]: row for row in block["cases"]}
    # The exact sentence is reported as such even though a tool ran: tool use is beside it.
    assert (rows["scope_with_a_tool"]["wording"], rows["scope_with_a_tool"]["tools_called"]) == (
        "scope",
        ["get_hotel_kpis"],
    )
    assert rows["personal"]["wording"] == "personal_data"
    assert rows["paraphrase"]["wording"] == "free_text"
    assert (rows["invalid"]["wording"], rows["invalid"]["outcome"]) == (
        "no_text",
        "invalid_response",
    )
    assert (rows["guarded"]["wording"], rows["guarded"]["model_calls"]) == ("no_text", 0)
    assert block["prompt"] == {
        "identity": "copilot_answer@v3",
        "checksum": COPILOT_ANSWER_V3.checksum,
    }
    assert list(block["wording_counts"]) == list(WORDING_KINDS)
    assert sum(block["wording_counts"].values()) == len(cases)
    assert block["wording_counts"]["free_text"] == 1
    assert sum(block["outcome_counts"].values()) == len(cases)
    assert "not a score" in block["statement"]
    json.dumps(block)  # serialisable as it stands


@pytest.mark.parametrize("other", [COPILOT_ANSWER_V1, COPILOT_ANSWER_V2], ids=["v1", "v2"])
def test_only_a_v3_run_is_analysed(other: Any) -> None:
    with pytest.raises(ValueError, match="copilot_answer@v3"):
        analyse(other, [case("x")], {"x": SCOPE_ANSWER}, {"x": 1})


# --- the shared schemas stay as they were ------------------------------------------------------


def test_no_shared_case_result_gained_a_wording_field() -> None:
    for result in (harness.CaseResult, knowledge_harness.KnowledgeCaseResult):
        names = {field.name for field in dataclasses.fields(result)}
        assert not names & {"wording", "decline_kind", "v3_analysis"}


def test_the_frozen_reports_serialise_exactly_as_pinned() -> None:
    assert harness.canonical(harness.reference_report()) == harness.expected_report(
        harness.REFERENCE_REPORT
    )
    assert harness.canonical(harness.reference_report_v2()) == harness.expected_report(
        harness.REFERENCE_REPORT_V2
    )
    assert knowledge_harness.canonical(
        knowledge_harness.reference_report()
    ) == knowledge_harness.REFERENCE_REPORT.read_text(encoding="utf-8")
