"""R2.8N: this hotel's figures are never served under another property's name.

Two halves. The grammar (`references_another_property`) is pure and tested by table: what it finds,
what it deliberately does not, and the authorized hotel's own name in every spelling it accepts.
The enforcement is tested through `CopilotService.answer_turn` with the Stage 7.6 fakes -- among
them R2.8J's `scope_named_competitor` exchange replayed turn for turn -- including that removing
the check would let the misattributed answer through. No model, provider, network or database.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.copilot.contracts import ToolOutcome
from app.copilot.grounding import tool_sourced_figures
from app.llm.base import ToolCall
from app.llm.testing import ScriptedModel, ScriptedTurn
from app.services.copilot import (
    ANOTHER_HOTEL_REFUSAL,
    AnsweredTurn,
    CopilotService,
    named_properties,
    names_foreign_uuid,
    references_another_property,
)
from tests.backend.test_copilot import HOTEL, OTHER_HOTEL, FakeHotel, Harness, kpis
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3
from tests.evaluation.transcribed_r28j import TURNS

OWN = FakeHotel.name  # "Harbour View Hotel"
MAY = "from 1 May 2026 to 31 May 2026"
R28J_QUESTION = COPILOT_EVAL_V3.case("scope_named_competitor").question
R28J_ANSWER = TURNS["scope_named_competitor"][-1]["text"]
#: What the KPI tool returned to R2.8J's model, as far as the answer used it: 70% occupancy.
OCCUPANCY = ToolOutcome(
    tool="get_hotel_kpis",
    outcome="succeeded",
    output={
        "range": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
        "occupancy_rate": 0.7,
        "room_revenue": 12345.5,
    },
)


def refers(question: str) -> bool:
    return references_another_property(question, HOTEL, OWN)


# --- the grammar ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        R28J_QUESTION,  # R2.8J's named competitor
        f"Our competitor down the road: what was their room revenue {MAY}?",
        f"What was the occupancy of our sister property in Athens {MAY}?",
        "How did the Seaview Resort do in May 2026?",
        "Compare our ADR with Harbour Inn's.",
        f"What was the revenue of {OTHER_HOTEL} in May 2026?",  # a bare, non-own UUID
        f"what was the occupancy of the GRAND MERIDIAN HOTEL {MAY}?",
        "What was the Grand Meridian Hotel's ADR in May 2026?",
        "Was the “Grand Meridian Hotel” busier than us?",
        "What did our COMPETITORS charge in May 2026?",
        "How is our Sister Hotel doing?",
        # The R2.7ZL/R2.7ZT forms are included, so the post-check never misses what the guard
        # would have refused.
        f"Revenue of hotel {OTHER_HOTEL}?",
        "What was the occupancy of the hotel across the street?",
    ],
)
def test_another_property_is_found(question: str) -> None:
    assert refers(question)


@pytest.mark.parametrize(
    "question",
    [
        f"What was our occupancy {MAY}?",
        "What is the hotel policy on late check-out?",
        "Is the hotel's restaurant open on Mondays?",
        "What was hotel revenue in May 2026?",
        "Hotel revenue for May 2026?",
        "Summarise hotel performance for May 2026.",
        "In May Hotel occupancy was what?",  # a month is not a name
        "What did Our Hotel earn in May 2026?",
        f"What was the revenue of {HOTEL} in May 2026?",  # the authorized hotel's own UUID
        f"What was the revenue of {str(HOTEL).upper()} in May 2026?",
        # the authorized hotel's canonical name, in each spelling the grammar accepts
        f"What was the occupancy of the Harbour View Hotel {MAY}?",
        "What was the HARBOUR VIEW HOTEL's occupancy in May 2026?",
        "What was the Harbour-View Hotel's ADR in May 2026?",
        "Was the “Harbour View Hotel” full in May 2026?",
        "How did Harbour View Resort do?",  # the same core name, another noun
        # Known false negatives, by design: a lowercase name, and a name with no property noun.
        f"What was the occupancy of the grand meridian Hotel {MAY}?",
        "What was the Marriott's occupancy in May 2026?",
    ],
)
def test_no_other_property_is_found(question: str) -> None:
    assert not refers(question)


def test_names_are_read_as_written_and_compared_loosely() -> None:
    assert named_properties("the Grand Meridian Hotel and the Seaview Resort") == [
        "Grand Meridian Hotel",
        "Seaview Resort",
    ]
    assert named_properties("the grand meridian hotel") == []  # lowercase: not found, by design
    assert named_properties("the Marriott") == []  # no property noun: not found, by design


def test_a_uuid_is_compared_never_looked_up() -> None:
    assert names_foreign_uuid(f"compare {HOTEL} with {OTHER_HOTEL}", HOTEL)
    assert not names_foreign_uuid(f"for {HOTEL}", HOTEL)
    assert not names_foreign_uuid("no identifier here", HOTEL)


def test_tool_sourced_figures_are_tool_figures_the_caller_did_not_write() -> None:
    outputs = [OCCUPANCY.output or {}]
    assert tool_sourced_figures(R28J_ANSWER, outputs=outputs, question=R28J_QUESTION) == ("70.00",)
    # The caller wrote 70: the same figure is theirs, not the tool's.
    assert tool_sourced_figures("Yes, 70%.", outputs=outputs, question="Was it 70%?") == ()
    # Dates the caller wrote never count; nor does a figure no tool returned.
    assert tool_sourced_figures("On 1 May 2026: 41.", outputs=outputs, question=MAY) == ()
    assert tool_sourced_figures("12,345.50", outputs=outputs, question="") == ("12,345.50",)


# --- the enforcement ----------------------------------------------------------------------------


def r28j_model() -> ScriptedModel:
    """R2.8J's two recorded model turns, as recorded: a KPI lookup for May, then the answer."""
    first, last = TURNS["scope_named_competitor"]
    calls = tuple(
        ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"]) for c in first["tool_calls"]
    )
    return ScriptedModel([ScriptedTurn(tool_calls=calls), last["text"]])


def turn(harness: Harness, question: str, **kwargs: Any) -> AnsweredTurn:
    return harness.service.answer_turn(HOTEL, question, **kwargs)


def test_the_r2_8j_exchange_is_served_as_the_scope_refusal() -> None:
    harness = Harness(r28j_model(), [OCCUPANCY])

    answered = turn(harness, R28J_QUESTION)

    response = answered.response
    assert answered.answer_source == "attribution_withheld"
    assert response.answer == ANOTHER_HOTEL_REFUSAL
    assert "70" not in response.answer and "Grand Meridian" not in response.answer
    assert (response.complete, response.stop_reason, response.notice) == (True, "completed", None)
    assert response.citations == []
    # Tenant binding unchanged: one hotel resolved, every call bound to it, no other fetched.
    assert harness.scope.resolved == [HOTEL]
    assert [call[0] for call in harness.invocations.calls] == [HOTEL]
    assert [call[1] for call in harness.invocations.calls] == ["get_hotel_kpis"]
    assert "hotel_public_id" not in harness.invocations.calls[0][2]
    # The model WAS asked -- this is the post-generation check, not the pre-model guard.
    assert len(harness.model.calls) == 2
    # Persistence unchanged: one row, the loop's own outcome, this hotel.
    [row] = harness.log.rows
    assert (row["hotel_id"], row["stop_reason"], row["tool_calls"]) == (
        FakeHotel.id,
        "completed",
        1,
    )
    assert harness.session.log == ["commit"]


def test_without_the_check_the_r2_8j_answer_would_be_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proof the enforcement tests depend on the check: with it disabled, R2.8J recurs."""
    monkeypatch.setattr(CopilotService, "_misattributes", staticmethod(lambda *a: False))
    harness = Harness(r28j_model(), [OCCUPANCY])

    answered = turn(harness, R28J_QUESTION)

    assert answered.answer_source == "model"
    assert answered.response.answer == R28J_ANSWER


def test_a_comparison_stating_a_tool_figure_is_withheld_whole() -> None:
    answer = "Our occupancy was 70.0% in May 2026. The Grand Meridian Hotel's is not available."
    harness = Harness(ScriptedModel([kpis(), answer]), [OCCUPANCY])

    answered = turn(harness, "Compare our occupancy with the Grand Meridian Hotel in May 2026.")

    assert answered.answer_source == "attribution_withheld"
    assert answered.response.answer == ANOTHER_HOTEL_REFUSAL  # no sentence of it survives


@pytest.mark.parametrize(
    "question",
    [
        # A non-own UUID never gets this far since R2.8N-D: test_uuid_scope_guard.py.
        f"What was our competitor's occupancy {MAY}?",
        f"What was the occupancy of our sister property in Athens {MAY}?",
    ],
)
def test_every_reference_form_withholds_a_tool_figure(question: str) -> None:
    harness = Harness(ScriptedModel([kpis(), "Occupancy was 70.00%."]), [OCCUPANCY])
    assert turn(harness, question).answer_source == "attribution_withheld"


def test_a_mention_without_a_tool_figure_is_served() -> None:
    answer = "This assistant can only report this hotel's figures."
    harness = Harness(ScriptedModel([kpis(), answer]), [OCCUPANCY])

    answered = turn(harness, R28J_QUESTION)

    assert (answered.answer_source, answered.response.answer) == ("model", answer)


def test_a_figure_the_caller_wrote_is_not_a_tool_figure() -> None:
    answer = "I cannot confirm 70% for another property."
    harness = Harness(ScriptedModel([kpis(), answer]), [OCCUPANCY])

    answered = turn(harness, "Was the Grand Meridian Hotel at 70% in May 2026?")

    assert (answered.answer_source, answered.response.answer) == ("model", answer)


@pytest.mark.parametrize(
    "question",
    [
        f"What was our occupancy {MAY}?",
        f"What was the occupancy of the Harbour View Hotel {MAY}?",
        f"What was the occupancy of {HOTEL} {MAY}?",
    ],
)
def test_a_question_about_this_hotel_is_served_normally(question: str) -> None:
    harness = Harness(ScriptedModel([kpis(), "Occupancy was 70.00%."]), [OCCUPANCY])

    answered = turn(harness, question)

    assert (answered.answer_source, answered.response.answer) == ("model", "Occupancy was 70.00%.")


def test_only_the_current_question_is_read() -> None:
    harness = Harness(ScriptedModel([kpis(), "Occupancy was 70.00%."]), [OCCUPANCY])

    answered = turn(
        harness,
        f"And ours {MAY}?",
        earlier=[(R28J_QUESTION, ANOTHER_HOTEL_REFUSAL)],
    )

    assert answered.answer_source == "model"


def test_an_ungrounded_figure_is_still_withheld_by_the_figure_check() -> None:
    harness = Harness(ScriptedModel([kpis(), "Occupancy was 99.9%."]), [OCCUPANCY])

    answered = turn(harness, R28J_QUESTION)

    assert answered.answer_source == "withheld"  # the existing check, first and unchanged
    assert (answered.response.answer, answered.response.stop_reason) == ("", "ungrounded_figures")


def test_the_pre_model_guard_is_unchanged() -> None:
    harness = Harness(ScriptedModel([]))

    answered = turn(harness, "What was another hotel's occupancy in May 2026?")

    assert answered.answer_source == "scope_guard"
    assert harness.model.calls == [] and harness.invocations.calls == []


def test_the_documents_substitution_is_unchanged() -> None:
    harness = Harness(ScriptedModel(["Not found in this hotel's documents."]))

    answered = turn(harness, R28J_QUESTION)

    assert answered.answer_source == "cannot_answer_substitution"


def test_the_response_shape_is_unchanged_and_the_log_holds_no_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness(r28j_model(), [OCCUPANCY])

    with caplog.at_level(logging.INFO, logger="app.services.copilot"):
        answered = turn(harness, R28J_QUESTION)

    assert "answer_source" not in answered.response.model_dump()
    [record] = [r for r in caplog.records if r.name == "app.services.copilot"]
    assert record.__dict__["attribution_withheld"] is True
    for text in (R28J_QUESTION, R28J_ANSWER, "Grand Meridian", "70.00"):
        assert text not in caplog.text
        assert all(text not in str(value) for value in record.__dict__.values())
