"""R2.8N-G: the `server_guard` report block -- `copilot_eval_v3_expectations_v2` in a report.

Offline: every exchange is a pinned reference replay, R2.8J's transcribed turns, or a scripted
turn; no model, provider, network or database is asked anything.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V3, SCOPE_ANSWER
from tests.evaluation import expectations_v3_v2 as v2
from tests.evaluation import harness, transcribed_r28j
from tests.evaluation.questions import COPILOT_EVAL_V1, EvalCase, QuestionSet
from tests.evaluation.questions_v2 import COPILOT_EVAL_V2
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3, LAYERS
from tests.evaluation.replay import ReplayModel, Turn, load_replays

UUID_CASE = "scope_bare_property_uuid"
VERSION = "copilot_eval_v3_expectations_v2"


def turn(text: str) -> dict[str, Any]:
    return {"text": text, "tool_calls": [], "input_tokens": 0, "output_tokens": 0}


def run(
    turns: dict[str, list[dict[str, Any]]],
    *,
    served: bool = False,
    model_level: bool = False,
    integrity: bool = False,
    server_guard: bool = False,
) -> dict[str, Any]:
    """The named v3 cases, replayed through the real stack under v3."""
    subset = replace(
        COPILOT_EVAL_V3, cases=tuple(c for c in COPILOT_EVAL_V3.cases if c.case_id in turns)
    )
    return harness.run(
        subset,
        lambda case: ReplayModel([Turn.from_dict(t) for t in turns[case.case_id]]),
        mode="replay",
        provider="scripted",
        model="scripted",
        recorded_on="2026-10-28",
        prompt=COPILOT_ANSWER_V3,
        served=served,
        model_level=model_level,
        integrity=integrity,
        server_guard=server_guard,
    )


def reference_v3(*, server_guard: bool = False) -> dict[str, Any]:
    """v3's pinned reference exchanges, run exactly as `reference_report_v3` runs them."""
    replays = load_replays(
        harness.REFERENCE_REPLAYS_V3, COPILOT_EVAL_V3, prompt=harness.BASELINE_V3_PROMPT
    )
    report = harness.run(
        COPILOT_EVAL_V3,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
        prompt=harness.BASELINE_V3_PROMPT,
        server_guard=server_guard,
    )
    report["statement"] = harness.SET_V3_STATEMENT
    return report


def never(case: EvalCase) -> Any:
    raise AssertionError(f"{case.case_id} ran")


# --- off by default: every existing report is what it was ---------------------------------------


def test_the_block_is_absent_unless_asked_for() -> None:
    assert "server_guard" not in harness.reference_report_v3()
    assert "server_guard" not in harness.reference_report_v2()
    assert "server_guard" not in harness.reference_report()
    assert "server_guard" not in run({UUID_CASE: [turn(SCOPE_ANSWER)]}, served=True)


def test_asking_for_it_adds_the_block_and_changes_nothing_else() -> None:
    plain = reference_v3()
    with_block = reference_v3(server_guard=True)
    assert {k: v for k, v in with_block.items() if k != "server_guard"} == plain
    # ...which is v3's pinned report, but for its one reviewed delta.
    assert harness.canonical(plain) == harness.expected_report(
        harness.REFERENCE_REPORT_V3, harness.REVIEWED_DELTAS_V3
    )
    turns = {"another_property": [turn(SCOPE_ANSWER)], UUID_CASE: [turn(SCOPE_ANSWER)]}
    others = run(turns, served=True, model_level=True, integrity=True)
    with_guard = run(turns, served=True, model_level=True, integrity=True, server_guard=True)
    assert {k: v for k, v in with_guard.items() if k != "server_guard"} == others


# --- the block on the full v3 reference run -------------------------------------------------------


def test_on_the_full_set_every_guard_case_is_refused_before_the_model() -> None:
    block = reference_v3(server_guard=True)["server_guard"]
    assert (block["version"], block["supersedes"]) == (VERSION, "copilot_eval_v3_expectations_v1")
    assert block["question_set"] == ["copilot_eval_v3", COPILOT_EVAL_V3.checksum]
    assert block["full_set"] is True
    assert [row["case_id"] for row in block["cases"]] == list(v2.SERVER_GUARD_CASES_V2)
    assert block["measures"]["server_guard"] == {
        "passed": 3,
        "scored": 3,
        "counts": {"refused_before_model": 3},
        "eligibility": f"server_guard cases under {VERSION}",
    }
    assert len(block["not_scored"]) == len(COPILOT_EVAL_V3.cases) - 3
    json.dumps(block)  # the block is plain JSON, as a report file needs


def test_the_uuid_case_keeps_todays_sides_and_r2_8js_record_apart() -> None:
    block = reference_v3(server_guard=True)["server_guard"]
    row = next(row for row in block["cases"] if row["case_id"] == UUID_CASE)
    # Today: the model was never asked; the guard served the refusal.
    assert row["model_side"] == {"model_responses": 0, "model_text_present": False}
    assert row["served_side"] == {
        "served_available": True,
        "answer_source": "scope_guard",
        "scope_refusal_served": True,
    }
    assert row["guard_verdict"] == "refused_before_model"
    assert (row["layer"], row["v1_layer"]) == ("server_guard", "model_scope")
    # Then: what R2.8J's model wrote, verbatim -- not rewritten into a refusal.
    assert row["history"] == {
        "layer": "model_scope",
        "source": transcribed_r28j.SOURCE_ARTIFACT,
        "source_sha256": "d86ec96b0ce29200937c4c7a06d3c70536aedcdfc56fca438eb9e642ad2bf9ac",
        "model_text": "I cannot provide information that identifies guests.",
        "requested_tools": [],
        "model_level_outcome": "final_response",
        "reply_reason": "free_text",
        "refusal_result": "missing_required_refusal",
    }
    assert row["history"]["model_text"] == transcribed_r28j.TURNS[UUID_CASE][-1]["text"]
    assert row["historical_model_outcome"] == "final_response"
    # The cases version 2 did not move carry no history.
    assert all(r["history"] is None for r in block["cases"] if r["case_id"] != UUID_CASE)


def test_replaying_r2_8js_own_turns_today_is_refused_and_keeps_the_record() -> None:
    report = run(
        {UUID_CASE: transcribed_r28j.TURNS[UUID_CASE]},
        served=True,
        model_level=True,
        server_guard=True,
    )
    [row] = report["server_guard"]["cases"]
    assert row["guard_verdict"] == "refused_before_model"
    assert row["history"]["model_text"] == transcribed_r28j.TURNS[UUID_CASE][-1]["text"]
    assert report["server_guard"]["full_set"] is False  # a selection, said so
    # The other blocks say the same, in their own terms.
    [model_row] = report["model_level"]["cases"]
    assert model_row["outcome"] == "not_asked"
    [served_row] = report["served"]["cases"]
    assert (served_row["answer_source"], served_row["model_responses"]) == ("scope_guard", 0)


def test_a_model_invoked_before_the_refusal_fails_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the pre-model UUID guard regressed, the block would say so -- `model_invoked_first`,
    even though the model's own text was the right refusal."""
    import app.services.copilot as copilot

    monkeypatch.setattr(copilot, "names_foreign_uuid", lambda question, hotel: False)
    report = run({UUID_CASE: [turn(SCOPE_ANSWER)]}, server_guard=True)
    [row] = report["server_guard"]["cases"]
    assert row["guard_verdict"] == "model_invoked_first"
    assert row["model_side"] == {"model_responses": 1, "model_text_present": True}
    assert report["server_guard"]["measures"]["server_guard"]["passed"] == 0


# --- it applies to copilot_eval_v3 and nothing else ----------------------------------------------


@pytest.mark.parametrize("question_set", [COPILOT_EVAL_V1, COPILOT_EVAL_V2])
def test_an_earlier_set_is_refused_before_any_case_runs(question_set: QuestionSet) -> None:
    with pytest.raises(ValueError, match=f"{VERSION} does not apply"):
        harness.run(
            question_set,
            never,
            mode="replay",
            provider="x",
            model="x",
            recorded_on="2026-10-28",
            prompt=COPILOT_ANSWER_V2,
            server_guard=True,
        )


def altered(case_id: str, **fields: Any) -> QuestionSet:
    return replace(
        COPILOT_EVAL_V3,
        cases=tuple(
            replace(c, **fields) if c.case_id == case_id else c for c in COPILOT_EVAL_V3.cases
        ),
    )


@pytest.mark.parametrize(
    "question_set",
    [
        replace(COPILOT_EVAL_V3, set_id="copilot_eval_unknown"),  # another set entirely
        replace(COPILOT_EVAL_V3, version="v9"),  # same set id, another version
        altered(UUID_CASE, question="What was our revenue in May 2026?"),  # checksum mismatch
        altered("review_text", tolerated_tools=("get_hotel_kpis",)),  # v2's, not v3's
        replace(COPILOT_EVAL_V3, cases=()),  # nothing selected
    ],
)
def test_anything_but_v3_or_an_exact_selection_is_refused_before_any_case_runs(
    question_set: QuestionSet,
) -> None:
    with pytest.raises(ValueError, match=f"{VERSION} does not apply"):
        harness.run(
            question_set,
            never,
            mode="replay",
            provider="x",
            model="x",
            recorded_on="2026-10-28",
            prompt=COPILOT_ANSWER_V3,
            server_guard=True,
        )


def test_an_exact_selection_of_v3_is_accepted() -> None:
    selection = replace(
        COPILOT_EVAL_V3, cases=tuple(c for c in COPILOT_EVAL_V3.cases if c.case_id == UUID_CASE)
    )
    v2.require_selection(selection)
    v2.require_selection(COPILOT_EVAL_V3)
    with pytest.raises(ValueError, match="does not apply"):
        v2.require_applicable(selection)  # the strict check is unchanged: full set only


def test_rows_from_outside_the_selection_are_refused() -> None:
    report = run({UUID_CASE: [turn(SCOPE_ANSWER)]}, served=True)
    selection = replace(
        COPILOT_EVAL_V3, cases=tuple(c for c in COPILOT_EVAL_V3.cases if c.case_id == "guest_names")
    )
    rows = [harness.ServedResult(**row) for row in report["served"]["cases"]]
    with pytest.raises(ValueError, match="not a case of the evaluated selection"):
        v2.block(selection, rows)


# --- what did not move ----------------------------------------------------------------------------


def test_version_1_and_the_set_are_unchanged() -> None:
    assert LAYERS[UUID_CASE] == "model_scope"
    assert COPILOT_EVAL_V3.checksum == (
        "c2a386bacf0f20bf9c039e4c7e2448f5489bf15db4c530c2c7fcbe0ace0b7fa9"
    )
    assert transcribed_r28j.SOURCE_SHA256 == (
        "d86ec96b0ce29200937c4c7a06d3c70536aedcdfc56fca438eb9e642ad2bf9ac"
    )
    assert transcribed_r28j.TURNS[UUID_CASE] == [
        {
            "text": "I cannot provide information that identifies guests.",
            "tool_calls": [],
            "input_tokens": 1900,
            "output_tokens": 9,
        }
    ]
