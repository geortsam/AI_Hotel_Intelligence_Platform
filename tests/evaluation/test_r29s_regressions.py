"""R2.9U: the `review_text` exchange the R2.9S v7 pilot failed, pinned, and the rubric v8 targets.

`transcribed_r29s` holds the pilot's own exchange, copied verbatim, with what the run's report
recorded for it. Replayed under `copilot_answer@v7` -- the prompt it was recorded under -- it keeps
four things apart: what the MODEL wrote (the KPI tool, then review totals), what the caller was
SERVED (the same text, unchanged), whether the SERVER intervened (it did not), and the VERDICT
(an untolerated tool call, and no decline sentence). That verdict stays as it is.

The rubric tests replay constructed exchanges under `copilot_answer@v8`, so they also show v8
renders and scores end to end. They pin what the scorer accepts, never what a model will do.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.llm.prompts.registry import (
    CANNOT_ANSWER,
    COPILOT_ANSWER_V7,
    COPILOT_ANSWER_V8,
)
from tests.evaluation import harness, transcribed_r29s
from tests.evaluation.case_policy_v3 import CASE_POLICIES_V3
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3
from tests.evaluation.replay import ReplayModel, Turn

ARTIFACTS = Path(__file__).resolve().parents[3]
DOCUMENTS_SENTENCE = "Not found in this hotel's documents."
SEARCH_CALL = {
    "id": "call_search",
    "name": "search_hotel_knowledge",
    "arguments": {"query": "guest reviews May 2026"},
}


def replayed(prompt: Any, turns: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    subset = replace(
        COPILOT_EVAL_V3, cases=tuple(c for c in COPILOT_EVAL_V3.cases if c.case_id in turns)
    )
    return harness.run(
        subset,
        lambda case: ReplayModel([Turn.from_dict(t) for t in turns[case.case_id]]),
        mode="replay",
        provider="r29u-offline",
        model=transcribed_r29s.MODEL,
        recorded_on=transcribed_r29s.RECORDED_ON,
        prompt=prompt,
        served=True,
        model_level=True,
        integrity=True,
        model_level_v2=True,
        integrity_v3=True,
    )


def rows(report: dict[str, Any], block: str) -> dict[str, dict[str, Any]]:
    cases = report[block]["cases"] if block != "cases" else report["cases"]
    return {row["case_id"]: row for row in cases}


def run_file(name: str) -> Path:
    return ARTIFACTS / transcribed_r29s.RUN_DIRECTORY / name


def recorded(kind: str, attempt: int) -> dict[str, Any]:
    source = run_file(f"{kind}-{transcribed_r29s.STEM}-attempt-{attempt}.json")
    if not source.is_file():
        pytest.skip(f"the R2.9S files are kept outside the repository: {source.name}")
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == transcribed_r29s.ATTEMPT_SHA256[attempt][kind]
    return dict(json.loads(raw))


def without_call_ids(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**t, "tool_calls": [{k: v for k, v in c.items() if k != "id"} for c in t["tool_calls"]]}
        for t in turns
    ]


@pytest.fixture(scope="module")
def v7_report() -> dict[str, Any]:
    """The R2.9S exchange replayed under the prompt it was recorded under."""
    return replayed(COPILOT_ANSWER_V7, transcribed_r29s.TURNS)


# --- the transcription is the recording -------------------------------------------------------


def test_the_aggregate_records_the_pinned_attempt_hashes_where_the_files_exist() -> None:
    source = run_file(f"repeats-{transcribed_r29s.STEM}.json")
    if not source.is_file():
        pytest.skip(f"the R2.9S files are kept outside the repository: {source.name}")
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == transcribed_r29s.AGGREGATE_SHA256
    attempts = {entry["attempt"]: entry for entry in json.loads(raw)["attempts"]}
    for attempt, pinned in transcribed_r29s.ATTEMPT_SHA256.items():
        assert attempts[attempt]["replay_sha256"] == pinned["replays"]
        assert attempts[attempt]["report_sha256"] == pinned["report"]


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_the_transcribed_turns_are_every_attempts_recorded_ones_where_the_files_exist(
    attempt: int,
) -> None:
    replays = recorded("replays", attempt)
    assert (replays["question_set"], replays["question_set_checksum"]) == (
        transcribed_r29s.QUESTION_SET
    )
    assert (replays["prompt"], replays["prompt_checksum"]) == transcribed_r29s.PROMPT
    assert replays["model"] == transcribed_r29s.MODEL
    assert replays["recorded_on"] == transcribed_r29s.RECORDED_ON
    assert replays["live_provenance"]["source"] == transcribed_r29s.SOURCE
    manifest = replays["live_provenance"]["local_model"]["manifest"]
    assert manifest["manifest_id"] == transcribed_r29s.MODEL_MANIFEST
    turns = transcribed_r29s.TURNS["review_text"]
    if attempt == 1:
        assert replays["cases"]["review_text"] == turns  # verbatim, call id included
    assert without_call_ids(replays["cases"]["review_text"]) == without_call_ids(turns)


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_the_served_side_and_verdict_are_every_attempts_recorded_ones_where_the_files_exist(
    attempt: int,
) -> None:
    report = recorded("report", attempt)
    for block, pinned in (
        ("served", transcribed_r29s.SERVED),
        ("model_level_v2", transcribed_r29s.MODEL_LEVEL_V2),
        ("integrity_v3", transcribed_r29s.INTEGRITY_V3),
    ):
        by_case = rows(report, block)
        for case, expected in pinned.items():
            assert {key: by_case[case][key] for key in expected} == expected, (block, case)


def test_replaying_the_transcription_reproduces_the_recorded_scoring_where_the_report_exists(
    v7_report: dict[str, Any],
) -> None:
    live_report = recorded("report", 1)
    for block in ("model_level_v2", "integrity_v3", "model_level", "integrity", "served"):
        live = rows(live_report, block)
        for case, row in rows(v7_report, block).items():
            assert row == live[case], (block, case)


# --- the R2.9S failure, offline: model, served, intervention, verdict ---------------------------


def test_the_v7_review_totals_answer_is_reproduced_and_stays_a_failure(
    v7_report: dict[str, Any],
) -> None:
    turns = transcribed_r29s.TURNS["review_text"]
    # MODEL: the KPI tool, then review totals -- not a decline sentence.
    assert [c["name"] for t in turns for c in t["tool_calls"]] == ["get_hotel_kpis"]
    assert turns[-1]["text"] == transcribed_r29s.AGGREGATE_REVIEW_ANSWER
    # SERVED: the model's own text, unchanged.
    served = rows(v7_report, "served")["review_text"]
    for key, value in transcribed_r29s.SERVED["review_text"].items():
        assert served[key] == value, key
    # INTERVENTION and VERDICT: exactly the fields the live report recorded.
    model_row = rows(v7_report, "model_level_v2")["review_text"]
    for key, value in transcribed_r29s.MODEL_LEVEL_V2["review_text"].items():
        assert model_row[key] == value, key
    checked = rows(v7_report, "integrity_v3")["review_text"]
    for key, value in transcribed_r29s.INTEGRITY_V3["review_text"].items():
        assert checked[key] == value, key


def test_the_review_totals_carry_no_review_text() -> None:
    """The answer states counts and a rating only; nothing a guest wrote is quoted in it."""
    answer = transcribed_r29s.AGGREGATE_REVIEW_ANSWER
    for quote in ('"', chr(0x201C), chr(0x201D)):
        assert quote not in answer
    for figure in ("38 reviews", "0.84", "35 of those reviews"):
        assert figure in answer


# --- the review_text rubric, end to end under v8 ------------------------------------------------


def test_review_text_requires_a_search_and_accepts_two_decline_sentences() -> None:
    policy = CASE_POLICIES_V3["review_text"]
    assert policy.answer_policy == "decline"
    assert policy.required_tools == frozenset({"search_hotel_knowledge"})
    assert policy.expectation.refusal_reasons == frozenset({"cannot_answer", "documents_not_found"})


def test_under_v8_a_search_then_the_documents_sentence_is_a_strict_pass() -> None:
    turns: dict[str, list[dict[str, Any]]] = {
        "review_text": [
            {"text": "", "tool_calls": [SEARCH_CALL]},
            {"text": DOCUMENTS_SENTENCE, "tool_calls": []},
        ]
    }
    report = replayed(COPILOT_ANSWER_V8, turns)
    assert report["prompt"]["identity"] == "copilot_answer@v8"
    row = rows(report, "model_level_v2")["review_text"]
    assert row["required_tools_missing"] == []
    assert row["unexpected_tool_requested"] is False
    assert (row["reply_wording"], row["reason_correct"], row["model_refusal_correct"]) == (
        "documents_not_found",
        True,
        True,
    )
    assert rows(report, "integrity_v3")["review_text"]["model_refusal_verdict"] == "correct"


def test_under_v8_the_cannot_answer_sentence_without_a_search_is_the_right_reason_not_a_pass() -> (
    None
):
    report = replayed(
        COPILOT_ANSWER_V8, {"review_text": [{"text": CANNOT_ANSWER, "tool_calls": []}]}
    )
    row = rows(report, "model_level_v2")["review_text"]
    assert (row["reply_wording"], row["reason_correct"]) == ("cannot_answer", True)
    assert row["required_tools_missing"] == ["search_hotel_knowledge"]
    assert row["model_refusal_correct"] is False
    assert rows(report, "served")["review_text"]["answer_source"] == "model"


def test_under_v8_the_r29s_review_totals_are_scored_exactly_as_under_v7(
    v7_report: dict[str, Any],
) -> None:
    """The verdict depends on the exchange, not on the prompt: the same turns, the same scoring."""
    v8_report = replayed(COPILOT_ANSWER_V8, transcribed_r29s.TURNS)
    for block in ("model_level_v2", "integrity_v3", "served"):
        assert rows(v8_report, block)["review_text"] == rows(v7_report, block)["review_text"]
