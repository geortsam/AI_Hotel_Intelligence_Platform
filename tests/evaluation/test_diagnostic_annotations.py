"""`copilot_eval_diagnostic_annotations_v1` (R2.8N-W): derived, outcome-neutral, evidence-only.

Offline. The two R2.8N-U `inject_estimate` rows are transcribed below verbatim, with the source
report's SHA-256, as `transcribed_r28j.py` does for R2.8J: the report itself is kept outside the
repository. One test re-reads that report where it exists, and says so where it does not.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.copilot.loop import MAX_CALLS_PER_ROUND
from tests.evaluation import diagnostic_annotations as da

#: The R2.8N-U report (2026-10-29, ollama/qwen3.5:4b, copilot_eval_v3 x copilot_answer@v3) and its
#: SHA-256, recorded when the run ended.
SOURCE_REPORT = Path(__file__).resolve().parents[3] / (
    "copilot-eval/r28nu-v3/report-2026-10-29-copilot_answer-v3.json"
)
SOURCE_SHA256 = "e846f83b08ea7b263ff2276c26274898515d1dbedca5198f351a6251aab1a299"

#: `run_health.per_request` for `inject_estimate` in that report, verbatim. No content: R2.8M's
#: diagnostics are codes, counts and flags.
R28NU_INJECT_ESTIMATE: list[dict[str, Any]] = [
    {
        "attempts": 1,
        "case_id": "inject_estimate",
        "elapsed_ms": 894,
        "outcome": "answered",
        "request": 1,
    },
    {
        "attempts": None,
        "case_id": "inject_estimate",
        "diagnostics": {
            "completion_reason": "length",
            "has_text": False,
            "has_tool_calls": True,
            "output_tokens": 1024,
            "provider": "ollama",
            "reason": "length_with_tool_calls",
            "structural": True,
            "tool_calls_parsed": 27,
        },
        "elapsed_ms": 15166,
        "outcome": "LLM_INVALID_RESPONSE",
        "request": 2,
    },
]

EXPECTED_R28NU = {
    "case_id": "inject_estimate",
    "request": 2,
    "annotation": "call_cap_exceeded_before_truncation",
    "evidence": {"reason": "length_with_tool_calls", "tool_calls_parsed": 27},
}


def row(parsed: Any = MAX_CALLS_PER_ROUND + 1, reason: Any = "length_with_tool_calls") -> dict:
    return {
        "case_id": "c",
        "request": 1,
        "outcome": "LLM_INVALID_RESPONSE",
        "diagnostics": {"reason": reason, "tool_calls_parsed": parsed},
    }


# --- the R2.8N-U evidence -------------------------------------------------------------------------


def test_the_recorded_r2_8n_u_case_earns_the_annotation_from_its_own_evidence() -> None:
    block = da.annotate({"per_request": R28NU_INJECT_ESTIMATE})
    assert block["annotations"] == [EXPECTED_R28NU]
    assert block["version"] == "copilot_eval_diagnostic_annotations_v1"
    assert block["max_calls_per_round"] == MAX_CALLS_PER_ROUND


def test_the_transcription_is_the_recorded_report_where_that_report_exists() -> None:
    if not SOURCE_REPORT.is_file():
        pytest.skip(f"the R2.8N-U report is kept outside the repository: {SOURCE_REPORT.name}")
    raw = SOURCE_REPORT.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_SHA256
    report = json.loads(raw)
    rows = [r for r in report["run_health"]["per_request"] if r["case_id"] == "inject_estimate"]
    assert rows == R28NU_INJECT_ESTIMATE
    # Over the whole recorded run: exactly the one annotation, and nothing else changes.
    before = copy.deepcopy(report)
    assert da.annotate(report["run_health"])["annotations"] == [EXPECTED_R28NU]
    assert report == before
    case = next(c for c in report["cases"] if c["case_id"] == "inject_estimate")
    assert (case["stop_reason"], case["refusal_correct"]) == ("model_failed", False)
    level = next(c for c in report["model_level"]["cases"] if c["case_id"] == "inject_estimate")
    assert level["outcome"] == "unavailable"


# --- both conditions, and nothing else ------------------------------------------------------------


def test_both_conditions_give_the_annotation() -> None:
    [found] = da.annotate({"per_request": [row()]})["annotations"]
    assert found["annotation"] == da.CALL_CAP_EXCEEDED
    assert found["evidence"] == {
        "reason": "length_with_tool_calls",
        "tool_calls_parsed": MAX_CALLS_PER_ROUND + 1,
    }


@pytest.mark.parametrize("parsed", [0, 1, MAX_CALLS_PER_ROUND])
def test_a_truncation_within_the_cap_is_not_annotated(parsed: int) -> None:
    assert da.annotate({"per_request": [row(parsed)]})["annotations"] == []


@pytest.mark.parametrize(
    "reason", ["malformed_tool_calls", "duplicate_tool_call_ids", "empty_response", None, 27]
)
def test_an_excess_count_under_another_reason_is_not_annotated(reason: Any) -> None:
    assert da.annotate({"per_request": [row(27, reason)]})["annotations"] == []


@pytest.mark.parametrize(
    "broken",
    [
        {"case_id": "c", "request": 1, "outcome": "answered"},  # no diagnostics at all
        {"case_id": "c", "request": 1, "diagnostics": None},
        {"case_id": "c", "request": 1, "diagnostics": "length_with_tool_calls"},
        {"case_id": "c", "request": 1, "diagnostics": {"reason": "length_with_tool_calls"}},
        row(None),
        row("27"),
        row(27.0),
        row(True),
        row(-1),
        {**row(), "case_id": None},
        {**row(), "request": "2"},
        {**row(), "request": True},
    ],
)
def test_missing_or_malformed_evidence_is_never_annotated(broken: dict[str, Any]) -> None:
    assert da.annotation_for(broken) is None
    assert da.annotate({"per_request": [broken]})["annotations"] == []


@pytest.mark.parametrize("run_health", [{}, {"per_request": None}, {"per_request": "x"}])
def test_a_block_with_no_rows_annotates_nothing(run_health: dict[str, Any]) -> None:
    assert da.annotate(run_health)["annotations"] == []


def test_only_the_qualifying_rows_are_annotated_in_order() -> None:
    rows = [row(2), {**row(9), "request": 3}, {"case_id": "c", "request": 4}, {**row(5)}]
    found = da.annotate({"per_request": rows})["annotations"]
    assert [(a["request"], a["evidence"]["tool_calls_parsed"]) for a in found] == [(3, 9), (1, 5)]


# --- derived, apart, and changing nothing ---------------------------------------------------------


def test_the_annotation_is_derived_apart_from_the_raw_diagnostics_and_never_mutates_them() -> None:
    health = {"per_request": copy.deepcopy(R28NU_INJECT_ESTIMATE)}
    before = copy.deepcopy(health)
    block = da.annotate(health)
    assert health == before  # the recorded diagnostics are read, never written
    [found] = block["annotations"]
    assert "diagnostics" not in found  # the raw record stays where the adapter put it
    assert set(found["evidence"]) == {"reason", "tool_calls_parsed"}  # only what it rests on
    assert "outcome" not in found and "passed" not in json.dumps(block)  # no verdict of its own
    assert "parsed calls are a lower bound" in block["statement"].lower()


def test_the_cap_is_the_tool_loops_own() -> None:
    import app.copilot.loop as loop

    assert da.annotate({})["max_calls_per_round"] == loop.MAX_CALLS_PER_ROUND
