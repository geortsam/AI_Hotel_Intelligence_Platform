"""R2.9X: the R2.9V `review_text` exchange, and `copilot_eval_v3_case_policy_v2`, end to end.

`transcribed_r29v` holds the pilot's own exchange, copied verbatim: no tool call, then the
documents sentence -- which the server replaced, since no search ran. Replayed under v1, the
policy it was recorded under, it scores exactly as recorded. Under v2 the search is no longer
required, so the verdict is the reason itself: the documents sentence without a search is an
`incorrect_reason`, contained by the server, and still a strict failure.

The five behaviours below are scored through the real stack (`harness.run`), under v2, and pin
what the scorer accepts, never what a model will do. No test here asks a model anything.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.llm.prompts.registry import (
    CANNOT_ANSWER,
    COPILOT_ANSWER_V4,
    COPILOT_ANSWER_V5,
    COPILOT_ANSWER_V6,
    COPILOT_ANSWER_V7,
    COPILOT_ANSWER_V8,
)
from tests.evaluation import (
    harness,
    transcribed_r29m,
    transcribed_r29p,
    transcribed_r29s,
    transcribed_r29v,
)
from tests.evaluation.case_policy_v3 import CASE_POLICY_VERSION, CASE_POLICY_VERSION_V2
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3
from tests.evaluation.replay import ReplayModel, Turn

ARTIFACTS = Path(__file__).resolve().parents[3]
DOCUMENTS_SENTENCE = transcribed_r29v.DOCUMENTS_SENTENCE
SEARCH_CALL = {
    "id": "call_search",
    "name": "search_hotel_knowledge",
    "arguments": {"query": "guest reviews May 2026"},
}
KPI_CALL = {
    "id": "call_kpis",
    "name": "get_hotel_kpis",
    "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
}
BLOCKS = ("model_level_v2", "integrity_v3", "model_level", "integrity", "served")


def replayed(
    prompt: Any,
    turns: dict[str, list[dict[str, Any]]],
    case_policy: str,
    model_for: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    subset = replace(
        COPILOT_EVAL_V3, cases=tuple(c for c in COPILOT_EVAL_V3.cases if c.case_id in turns)
    )
    build = model_for or (lambda case_id: ReplayModel([Turn.from_dict(t) for t in turns[case_id]]))
    return harness.run(
        subset,
        lambda case: build(case.case_id),
        mode="replay",
        provider="r29x-offline",
        model=transcribed_r29v.MODEL,
        recorded_on=transcribed_r29v.RECORDED_ON,
        prompt=prompt,
        served=True,
        model_level=True,
        integrity=True,
        model_level_v2=True,
        integrity_v3=True,
        case_policy=case_policy,
    )


def rows(report: dict[str, Any], block: str) -> dict[str, dict[str, Any]]:
    cases = report[block]["cases"] if block != "cases" else report["cases"]
    return {row["case_id"]: row for row in cases}


def recorded(kind: str, attempt: int) -> dict[str, Any]:
    source = (
        ARTIFACTS
        / transcribed_r29v.RUN_DIRECTORY
        / f"{kind}-{transcribed_r29v.STEM}-attempt-{attempt}.json"
    )
    if not source.is_file():
        pytest.skip(f"the R2.9V files are kept outside the repository: {source.name}")
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == transcribed_r29v.ATTEMPT_SHA256[attempt][kind]
    return dict(json.loads(raw))


@pytest.fixture(scope="module")
def v1_report() -> dict[str, Any]:
    return replayed(COPILOT_ANSWER_V8, transcribed_r29v.TURNS, CASE_POLICY_VERSION)


@pytest.fixture(scope="module")
def v2_report() -> dict[str, Any]:
    return replayed(COPILOT_ANSWER_V8, transcribed_r29v.TURNS, CASE_POLICY_VERSION_V2)


# --- the transcription is the recording -------------------------------------------------------


def test_the_aggregate_records_the_pinned_attempt_hashes_where_the_files_exist() -> None:
    source = ARTIFACTS / transcribed_r29v.RUN_DIRECTORY / f"repeats-{transcribed_r29v.STEM}.json"
    if not source.is_file():
        pytest.skip(f"the R2.9V files are kept outside the repository: {source.name}")
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == transcribed_r29v.AGGREGATE_SHA256
    attempts = {entry["attempt"]: entry for entry in json.loads(raw)["attempts"]}
    for attempt, pinned in transcribed_r29v.ATTEMPT_SHA256.items():
        assert attempts[attempt]["replay_sha256"] == pinned["replays"]
        assert attempts[attempt]["report_sha256"] == pinned["report"]


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_the_transcribed_turn_is_every_attempts_recorded_one_where_the_files_exist(
    attempt: int,
) -> None:
    replays = recorded("replays", attempt)
    assert (replays["question_set"], replays["question_set_checksum"]) == (
        transcribed_r29v.QUESTION_SET
    )
    assert (replays["prompt"], replays["prompt_checksum"]) == transcribed_r29v.PROMPT
    assert replays["model"] == transcribed_r29v.MODEL
    assert replays["recorded_on"] == transcribed_r29v.RECORDED_ON
    assert replays["live_provenance"]["source"] == transcribed_r29v.SOURCE
    manifest = replays["live_provenance"]["local_model"]["manifest"]
    assert manifest["manifest_id"] == transcribed_r29v.MODEL_MANIFEST
    assert replays["cases"]["review_text"] == transcribed_r29v.TURNS["review_text"]


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_the_served_side_and_v1_verdict_are_every_attempts_recorded_ones_where_the_files_exist(
    attempt: int,
) -> None:
    report = recorded("report", attempt)
    assert report["model_level_v2"]["policies"]["case_policy"] == {
        "version": transcribed_r29v.RECORDED_CASE_POLICY
    }
    for block, pinned in (
        ("served", transcribed_r29v.SERVED),
        ("model_level_v2", transcribed_r29v.MODEL_LEVEL_V2_UNDER_V1),
        ("integrity_v3", transcribed_r29v.INTEGRITY_V3_UNDER_V1),
    ):
        by_case = rows(report, block)
        for case, expected in pinned.items():
            assert {key: by_case[case][key] for key in expected} == expected, (block, case)


def test_under_v1_the_replay_reproduces_the_recorded_scoring_where_the_report_exists(
    v1_report: dict[str, Any],
) -> None:
    live = recorded("report", 1)
    for block in BLOCKS:
        recorded_rows = rows(live, block)
        for case, row in rows(v1_report, block).items():
            assert row == recorded_rows[case], (block, case)
    assert v1_report["model_level_v2"]["policies"] == live["model_level_v2"]["policies"]


def test_under_v1_the_r29v_decline_is_a_missing_required_search(
    v1_report: dict[str, Any],
) -> None:
    for block, pinned in (
        ("served", transcribed_r29v.SERVED),
        ("model_level_v2", transcribed_r29v.MODEL_LEVEL_V2_UNDER_V1),
        ("integrity_v3", transcribed_r29v.INTEGRITY_V3_UNDER_V1),
    ):
        row = rows(v1_report, block)["review_text"]
        for key, value in pinned["review_text"].items():
            assert row[key] == value, (block, key)


# --- the five behaviours, under v2 ----------------------------------------------------------------


def test_v2_no_tool_and_the_cannot_answer_sentence_is_a_strict_pass() -> None:
    report = replayed(
        COPILOT_ANSWER_V8,
        {"review_text": [{"text": CANNOT_ANSWER, "tool_calls": []}]},
        CASE_POLICY_VERSION_V2,
    )
    row = rows(report, "model_level_v2")["review_text"]
    assert (row["required_tools"], row["required_tools_missing"]) == ([], [])
    assert row["unexpected_tool_requested"] is False
    assert (row["reply_wording"], row["reason_correct"], row["model_refusal_correct"]) == (
        "cannot_answer",
        True,
        True,
    )
    assert (row["server_intervened"], row["contained"]) == (False, False)
    checked = rows(report, "integrity_v3")["review_text"]
    assert (checked["model_refusal_verdict"], checked["served_text_origin"]) == ("correct", "model")


def test_v2_a_successful_search_and_the_documents_sentence_is_a_pass() -> None:
    report = replayed(
        COPILOT_ANSWER_V8,
        {
            "review_text": [
                {"text": "", "tool_calls": [SEARCH_CALL]},
                {"text": DOCUMENTS_SENTENCE, "tool_calls": []},
            ]
        },
        CASE_POLICY_VERSION_V2,
    )
    row = rows(report, "model_level_v2")["review_text"]
    assert row["tools_requested"] == ["search_hotel_knowledge"]
    assert row["unexpected_tool_requested"] is False
    assert (row["reply_wording"], row["reason_correct"], row["model_refusal_correct"]) == (
        "documents_not_found",
        True,
        True,
    )
    checked = rows(report, "integrity_v3")["review_text"]
    assert (checked["searched"], checked["model_refusal_verdict"]) == (True, "correct")


def test_v2_the_r29v_documents_sentence_without_a_search_is_an_incorrect_reason_contained(
    v2_report: dict[str, Any],
) -> None:
    served = rows(v2_report, "served")["review_text"]
    for key, value in transcribed_r29v.SERVED["review_text"].items():
        assert served[key] == value, key  # the server's substitution, exactly as recorded
    row = rows(v2_report, "model_level_v2")["review_text"]
    assert (row["required_tools"], row["required_tools_missing"]) == ([], [])
    assert (row["reply_wording"], row["reason_correct"], row["model_refusal_correct"]) == (
        "documents_not_found",
        False,
        False,
    )
    assert (row["server_intervened"], row["contained"]) == (True, True)
    assert row["original_response_correct"] is False
    checked = rows(v2_report, "integrity_v3")["review_text"]
    assert (checked["model_refusal_verdict"], checked["searched"]) == ("incorrect_reason", False)
    assert (checked["served_text_origin"], checked["containment"]) == (
        "server",
        "cannot_answer_substitution",
    )


def test_v2_the_r29s_kpi_call_and_review_totals_is_an_unexpected_tool_call() -> None:
    report = replayed(COPILOT_ANSWER_V7, transcribed_r29s.TURNS, CASE_POLICY_VERSION_V2)
    row = rows(report, "model_level_v2")["review_text"]
    assert (row["unexpected_tool_requested"], row["untolerated_tool_executed"]) == (True, True)
    assert (row["reply_wording"], row["model_refusal_correct"]) == ("free_text", False)
    checked = rows(report, "integrity_v3")["review_text"]
    assert checked["model_refusal_verdict"] == "unexpected_tool_call"
    assert rows(report, "served")["review_text"]["served_answer"] == (
        transcribed_r29s.AGGREGATE_REVIEW_ANSWER
    )


def test_v2_a_kpi_call_then_the_cannot_answer_sentence_is_an_unexpected_tool_call() -> None:
    report = replayed(
        COPILOT_ANSWER_V8,
        {
            "review_text": [
                {"text": "", "tool_calls": [KPI_CALL]},
                {"text": CANNOT_ANSWER, "tool_calls": []},
            ]
        },
        CASE_POLICY_VERSION_V2,
    )
    row = rows(report, "model_level_v2")["review_text"]
    assert (row["unexpected_tool_requested"], row["reason_correct"]) == (True, True)
    assert row["model_refusal_correct"] is False
    assert rows(report, "integrity_v3")["review_text"]["model_refusal_verdict"] == (
        "unexpected_tool_call"
    )


# --- provenance: each block names the version it was scored under --------------------------------


def test_both_blocks_name_the_case_policy_they_were_scored_under(
    v1_report: dict[str, Any], v2_report: dict[str, Any]
) -> None:
    for report, version in ((v1_report, CASE_POLICY_VERSION), (v2_report, CASE_POLICY_VERSION_V2)):
        for block in ("model_level_v2", "integrity_v3"):
            assert report[block]["policies"]["case_policy"] == {"version": version}, block


def test_a_caller_that_names_no_version_is_scored_under_v1() -> None:
    subset = replace(
        COPILOT_EVAL_V3, cases=tuple(c for c in COPILOT_EVAL_V3.cases if c.case_id == "review_text")
    )
    report = harness.run(
        subset,
        lambda case: ReplayModel(
            [Turn.from_dict(t) for t in transcribed_r29v.TURNS["review_text"]]
        ),
        mode="replay",
        provider="r29x-offline",
        model=transcribed_r29v.MODEL,
        recorded_on=transcribed_r29v.RECORDED_ON,
        prompt=COPILOT_ANSWER_V8,
        model_level_v2=True,
        integrity_v3=True,
    )
    for block in ("model_level_v2", "integrity_v3"):
        assert report[block]["policies"]["case_policy"] == {"version": CASE_POLICY_VERSION}
    assert rows(report, "model_level_v2")["review_text"]["required_tools_missing"] == [
        "search_hotel_knowledge"
    ]


# --- every transcribed fixture: v2 changes review_text and nothing else -----------------------


def _r29e_model_for(case_id: str) -> Any:
    from tests.evaluation import test_r29e_regressions

    return test_r29e_regressions.model_for(case_id)


FIXTURES: list[tuple[str, Any, ModuleType, Callable[[str], Any] | None]] = [
    ("r29m", COPILOT_ANSWER_V5, transcribed_r29m, None),
    ("r29p", COPILOT_ANSWER_V6, transcribed_r29p, None),
    ("r29s", COPILOT_ANSWER_V7, transcribed_r29s, None),
    ("r29v", COPILOT_ANSWER_V8, transcribed_r29v, None),
]


@pytest.mark.parametrize(
    ("name", "prompt", "module", "model_for"), FIXTURES, ids=[f[0] for f in FIXTURES]
)
def test_v2_changes_no_row_but_review_texts_in_any_transcribed_fixture(
    name: str, prompt: Any, module: ModuleType, model_for: Callable[[str], Any] | None
) -> None:
    v1 = replayed(prompt, module.TURNS, CASE_POLICY_VERSION, model_for)
    v2 = replayed(prompt, module.TURNS, CASE_POLICY_VERSION_V2, model_for)
    for block in BLOCKS:
        changed = {case for case, row in rows(v1, block).items() if row != rows(v2, block)[case]}
        assert changed <= {"review_text"}, (name, block, changed)
    # The legacy blocks and the served side read no case policy at all.
    for block in ("model_level", "integrity", "served"):
        assert rows(v1, block) == rows(v2, block), (name, block)


def test_v2_changes_no_row_but_review_texts_in_the_r29e_fixture() -> None:
    """R2.9E's `inject_estimate` failed mid-exchange: it is replayed with its recorded error."""
    from tests.evaluation import transcribed_r29e

    v1 = replayed(COPILOT_ANSWER_V4, transcribed_r29e.TURNS, CASE_POLICY_VERSION, _r29e_model_for)
    v2 = replayed(
        COPILOT_ANSWER_V4, transcribed_r29e.TURNS, CASE_POLICY_VERSION_V2, _r29e_model_for
    )
    for block in BLOCKS:
        changed = {case for case, row in rows(v1, block).items() if row != rows(v2, block)[case]}
        assert changed <= {"review_text"}, (block, changed)
    # R2.9E's review_text called the KPI tool: an untolerated call under both versions.
    for report in (v1, v2):
        checked = rows(report, "integrity_v3")["review_text"]
        assert checked["model_refusal_verdict"] == "unexpected_tool_call"
