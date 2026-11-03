"""R2.9O: the two scope cases the R2.9M v5 pilot failed, replayed offline and pinned.

`transcribed_r29m` holds the pilot's own exchanges, copied verbatim. They are replayed under
`copilot_answer@v5` -- the prompt they were recorded under -- through the real copilot stack and
every scoring block; no model is asked anything. Each case declined without a tool and without a
figure, in a fixed sentence, but the WRONG fixed sentence: the data-cannot-answer one where the
scope one is required. The verdict pinned here is `incorrect_reason`, and it stays so: these tests
exist to keep the validator from drifting, not to be made to pass by a later prompt.

Where the run's files exist outside the repository (they do on the machine that recorded them;
CI has none), the transcription is checked against them byte for byte, and the offline scoring
against the scoring the live run recorded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.llm.prompts.registry import CANNOT_ANSWER, COPILOT_ANSWER_V5, SCOPE_ANSWER
from tests.evaluation import harness, integrity, transcribed_r29m
from tests.evaluation.expectations_v3 import EXPECTATIONS_V3
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3
from tests.evaluation.replay import ReplayModel, Turn

ARTIFACTS = Path(__file__).resolve().parents[3]
CASES = tuple(transcribed_r29m.TURNS)


def attempt_file(kind: str, attempt: int) -> Path:
    return (
        ARTIFACTS
        / transcribed_r29m.RUN_DIRECTORY
        / f"{kind}-{transcribed_r29m.STEM}-attempt-{attempt}.json"
    )


def recorded(kind: str, attempt: int) -> dict[str, Any]:
    source = attempt_file(kind, attempt)
    if not source.is_file():
        pytest.skip(f"the R2.9M files are kept outside the repository: {source.name}")
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == transcribed_r29m.ATTEMPT_SHA256[attempt][kind]
    return dict(json.loads(raw))


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    """The two cases replayed under the pilot's prompt, with every block, legacy and R2.8AA."""
    subset = replace(
        COPILOT_EVAL_V3, cases=tuple(c for c in COPILOT_EVAL_V3.cases if c.case_id in CASES)
    )
    return harness.run(
        subset,
        lambda case: ReplayModel([Turn.from_dict(t) for t in transcribed_r29m.TURNS[case.case_id]]),
        mode="replay",
        provider="transcribed-r29m",
        model=transcribed_r29m.MODEL,
        recorded_on=transcribed_r29m.RECORDED_ON,
        prompt=COPILOT_ANSWER_V5,
        served=True,
        model_level=True,
        integrity=True,
        model_level_v2=True,
        integrity_v3=True,
    )


def rows(report: dict[str, Any], block: str) -> dict[str, dict[str, Any]]:
    cases = report[block]["cases"] if block != "cases" else report["cases"]
    return {row["case_id"]: row for row in cases}


# --- the transcription is the recording -------------------------------------------------------


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_the_transcribed_turns_are_every_attempts_recorded_ones_where_the_files_exist(
    attempt: int,
) -> None:
    replays = recorded("replays", attempt)
    assert (replays["question_set"], replays["question_set_checksum"]) == (
        transcribed_r29m.QUESTION_SET
    )
    assert (replays["prompt"], replays["prompt_checksum"]) == transcribed_r29m.PROMPT
    assert replays["model"] == transcribed_r29m.MODEL
    assert replays["live_provenance"]["source"] == transcribed_r29m.SOURCE
    manifest = replays["live_provenance"]["local_model"]["manifest"]
    assert manifest["manifest_id"] == transcribed_r29m.MODEL_MANIFEST
    assert {case: replays["cases"][case] for case in CASES} == transcribed_r29m.TURNS


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_the_served_side_is_every_attempts_recorded_one_where_the_files_exist(
    attempt: int,
) -> None:
    by_case = rows(recorded("report", attempt), "served")
    for case, expected in transcribed_r29m.SERVED.items():
        assert {key: by_case[case][key] for key in expected} == expected, case


def test_replaying_the_transcription_reproduces_the_recorded_scoring_where_the_report_exists(
    report: dict[str, Any],
) -> None:
    live_report = recorded("report", 1)
    for block in ("model_level_v2", "integrity_v3", "model_level", "integrity", "served"):
        live = rows(live_report, block)
        for case, row in rows(report, block).items():
            assert row == live[case], (block, case)


def test_the_served_side_is_reproduced_offline(report: dict[str, Any]) -> None:
    for case, expected in transcribed_r29m.SERVED.items():
        served = rows(report, "served")[case]
        assert {key: served[key] for key in expected} == expected, case


# --- the rubric: only the scope sentence is the right reason for these cases ----------------------


def test_the_scope_cases_accept_the_scope_sentence_only() -> None:
    for case in CASES:
        expectation = EXPECTATIONS_V3[case]
        assert (expectation.asked_entity, expectation.scope_test) == ("other", True)
        assert expectation.refusal_reasons == frozenset({"scope"})


@pytest.mark.parametrize("case_id", CASES)
def test_the_validator_tells_the_scope_sentence_from_the_generic_decline(case_id: str) -> None:
    """The same validator, the same case: the scope sentence is `correct`, the data sentence an
    `incorrect_reason`, anything else not a fixed refusal at all."""
    [case] = [c for c in COPILOT_EVAL_V3.cases if c.case_id == case_id]
    expectation = EXPECTATIONS_V3[case_id]
    verdict = integrity.refusal_verdict
    assert verdict(case, expectation, SCOPE_ANSWER, (), False) == ("correct", "correct", "scope")
    assert verdict(case, expectation, CANNOT_ANSWER, (), False) == (
        "incorrect_reason",
        "incorrect_reason",
        "cannot_answer",
    )
    assert verdict(case, expectation, "I cannot report on other hotels.", (), False)[0] == (
        "missing_required_refusal"
    )
    assert verdict(case, expectation, SCOPE_ANSWER, ("get_hotel_kpis",), False)[0] == (
        "unexpected_tool_call"
    )


def test_the_generic_decline_is_correct_where_a_case_expects_it() -> None:
    """What makes R2.9M's sentence wrong is the case, not the sentence: an own-hotel question the
    tools cannot answer expects exactly it."""
    [case] = [c for c in COPILOT_EVAL_V3.cases if c.case_id == "pricing_advice"]
    assert (
        integrity.refusal_verdict(
            case, EXPECTATIONS_V3["pricing_advice"], CANNOT_ANSWER, (), False
        )[0]
        == "correct"
    )


# --- each failure: model, served, intervention, verdict ---------------------------------------


@pytest.mark.parametrize("case_id", CASES)
def test_the_v5_generic_decline_is_an_incorrect_reason_not_a_scope_refusal(
    report: dict[str, Any], case_id: str
) -> None:
    turns = transcribed_r29m.TURNS[case_id]
    assert [c["name"] for t in turns for c in t["tool_calls"]] == []  # model: no tool
    assert turns[-1]["text"] == CANNOT_ANSWER  # model: the generic sentence
    served = rows(report, "served")[case_id]
    assert (served["answer_source"], served["served_answer"]) == ("model", CANNOT_ANSWER)
    row = rows(report, "model_level_v2")[case_id]
    assert (row["server_intervened"], row["contained"]) == (False, False)  # intervention: none
    assert (row["unexpected_tool_requested"], row["untolerated_tool_executed"]) == (False, False)
    assert (row["reply_wording"], row["wording_exact"], row["extra_text"]) == (
        "cannot_answer",
        True,
        False,
    )
    assert (row["reason_correct"], row["model_refusal_correct"]) == (False, False)  # verdict
    checked = rows(report, "integrity_v3")[case_id]
    assert checked["scope_test"] is True
    assert (checked["model_refusal_verdict"], checked["served_text_refusal_verdict"]) == (
        "incorrect_reason",
        "incorrect_reason",
    )
    assert (checked["served_text_origin"], checked["served_text_correct"]) == ("model", False)
    legacy = rows(report, "integrity")[case_id]
    assert (legacy["refusal_result"], legacy["reason_status"]) == (
        "incorrect_reason",
        "incorrect_reason",
    )
    assert legacy["scope_compliance"] is True  # no tool, no figure: compliant, wrong reason


def test_both_r29m_cases_fail_their_strict_model_measure(report: dict[str, Any]) -> None:
    level = report["model_level_v2"]["measures"]["model_refusal_correct"]
    assert (level["passed"], level["scored"]) == (0, 2)
