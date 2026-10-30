"""R2.8P: R2.8N-U's `revenue_by_category` exchange, replayed offline against the new tool output.

This is an OFFLINE REPLAY, not an evaluation. The two model turns below are copied verbatim from
R2.8N-U's replay file (2026-10-29, ollama/qwen3.5:4b, `copilot_eval_v3` x `copilot_answer@v3`),
whose SHA-256 is pinned, as `transcribed_r28j.py` does for R2.8J. Replaying them asks no model
anything: it shows what the server does today with what that model wrote then. R2.8N-U itself ran
against the old tool output, without totals, and its recorded result -- the answer withheld for
`ungrounded_figures` -- is history and stays as it was. Whether a model, given the new output,
writes the same text is a question only a new live run could answer; none was run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.copilot.registry import build_default_registry
from app.copilot.tools import revenue_breakdown
from app.llm.base import Budget
from app.llm.prompts.registry import COPILOT_ANSWER_V3
from tests.evaluation import harness
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3
from tests.evaluation.replay import ReplayModel, Turn

#: R2.8N-U's replay file, kept outside the repository, and its SHA-256 when the run ended.
SOURCE_REPLAYS = Path(__file__).resolve().parents[3] / (
    "copilot-eval/r28nu-v3/replays-2026-10-29-copilot_answer-v3.json"
)
SOURCE_SHA256 = "e59974d08bb1ddf41ae484d8eb7cfef1ffca57056101795cf7264c0a5caa1e5b"

#: `cases["revenue_by_category"]` in that file, verbatim.
R28NU_REVENUE_BY_CATEGORY: list[dict[str, Any]] = [
    {
        "text": "",
        "tool_calls": [
            {
                "id": "call_oac1kpy2",
                "name": "get_revenue_breakdown",
                "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
            }
        ],
        "input_tokens": 1861,
        "output_tokens": 59,
    },
    {
        "text": (
            "Ledger revenue for 1 May 2026 to 31 May 2026 by category:\n\n"
            "- **ROOM**: €51,800.00 (212 entries, tax €4,662.00)\n"
            "- **FNB**: €2,450.00 (96 entries, tax €318.50)\n"
            "- **SPA**: €700.00 (14 entries, tax €91.00)\n\n"
            "Total revenue: €54,950.00 (tax €5,071.50)"
        ),
        "tool_calls": [],
        "input_tokens": 2100,
        "output_tokens": 138,
    },
]
FINAL_TEXT = R28NU_REVENUE_BY_CATEGORY[-1]["text"]


def replay() -> tuple[Any, Any]:
    """The recorded turns through the real copilot stack, with v3's fixtures and prompt."""
    return harness.run_case_served(
        COPILOT_EVAL_V3.case("revenue_by_category"),
        ReplayModel([Turn.from_dict(turn) for turn in R28NU_REVENUE_BY_CATEGORY]),
        registry=build_default_registry(),
        budget=Budget(timeout_seconds=120.0, max_output_tokens=1024),
        provider_name="replay",
        model_name="qwen3.5:4b",
        measure_latency=False,
        prompt=COPILOT_ANSWER_V3,
        forecasts=harness.forecasts_for(COPILOT_EVAL_V3),
    )


def test_the_transcription_is_the_recorded_replay_where_that_file_exists() -> None:
    if not SOURCE_REPLAYS.is_file():
        pytest.skip(f"the R2.8N-U replays are kept outside the repository: {SOURCE_REPLAYS.name}")
    raw = SOURCE_REPLAYS.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_SHA256
    assert json.loads(raw)["cases"]["revenue_by_category"] == R28NU_REVENUE_BY_CATEGORY


def test_the_recorded_totals_are_now_grounded_and_the_answer_is_served() -> None:
    result, served = replay()
    assert result.tools_called == ["get_revenue_breakdown"]
    assert (result.grounded, result.ungrounded_figures) == (True, [])
    assert (result.stop_reason, result.complete) == ("completed", True)
    assert result.expected_figures_present is True  # the three category figures, as before
    assert (served.answer_source, served.served_complete) == ("model", True)
    assert served.served_answer == FINAL_TEXT  # served exactly as the model wrote it


def test_without_the_totals_the_recorded_outcome_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the same turns against an output with no totals reproduce what R2.8N-U recorded
    -- the two sums ungrounded and the answer withheld -- so the totals are what changed it."""
    monkeypatch.setattr(revenue_breakdown, "currency_totals", lambda categories: [])
    result, served = replay()
    assert result.ungrounded_figures == ["54,950.00", "5,071.50"]
    assert result.stop_reason == "ungrounded_figures"
    assert (served.answer_source, served.served_answer) == ("withheld", "")
