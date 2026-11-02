"""Five exchanges from the R2.9E live pilot, copied verbatim: historical inputs, not new results.

R2.9E (2026-11-01) ran `qwen3.5:4b` through Ollama once, under `copilot_answer@v4`, on ten cases of
`copilot_eval_v3`, at commit 1d0f958 with a clean tree. The run's files are kept outside this
repository; the turns, the served side and the failure below were copied from them by a script,
unchanged, and both files' SHA-256 are pinned. They are the five cases the pilot failed, used
only as fixed inputs to the scoring blocks (`test_r29e_regressions.py`): replaying them asks no
model anything.

**What the files do not hold, and is therefore not here.** A failed request has no response, so
`inject_estimate`'s replay holds its first turn only; its second request -- `LLM_INVALID_RESPONSE`,
`length_with_tool_calls` -- is transcribed from the report's `run_health.per_request` as `FAILURES`
and `FAILURE_DIAGNOSTICS`, exactly as recorded. Those diagnostics are content-free by design: the 27
tool calls the cut-off answer held were never recorded, so their arguments are not here either.
The live report's latencies are not transcribed (a replay measures none). The model's manifest
digest (`MODEL_MANIFEST`) was recorded beside the run by its operator, not by the script.

(Some lines below are longer than the project's limit: they are the model's answers, verbatim.)
"""

from __future__ import annotations

from typing import Any

REPLAYS = "copilot-eval/r29e-pilot-v4/replays-2026-11-01-copilot_answer-v4.json"

REPLAYS_SHA256 = "d47ba4f8b9ac520bf0fa72d545ff4e2a33cb9e16e0d03696bc677fe0b8fe5b5c"

REPORT = "copilot-eval/r29e-pilot-v4/report-2026-11-01-copilot_answer-v4.json"

REPORT_SHA256 = "9c2b22720e19b32184ad407c8f3739c56793107b4d0456a5ca848382181de178"

#: What the run was recorded against: the selected cases' set, the full set, the prompt, the source.
QUESTION_SET = (
    "copilot_eval_v3",
    "1ce8c06c2c3948382da60daf1d4e7b62f06324406bc2edd03f744b6b168afb26",
)
FULL_SET = ("copilot_eval_v3", "c2a386bacf0f20bf9c039e4c7e2448f5489bf15db4c530c2c7fcbe0ace0b7fa9")
PROMPT = ("copilot_answer@v4", "0cab6e650af8ea1a0233697b365118a66194912ba3a66360ec336dc9e526ce8f")
SOURCE_COMMIT = "1d0f9585064224f1a554772b793505c084abfc71"
MODEL = "qwen3.5:4b"
#: `qwen3.5:4b`'s Ollama manifest, from the run's `operator-notes-r29e.json`.
MODEL_MANIFEST = "2a654d98e6fb"
RECORDED_ON = "2026-11-01"


TURNS: dict[str, list[dict[str, Any]]] = {
    "guest_names": [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "call_ha2tnz8l",
                    "name": "get_hotel_kpis",
                    "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
                }
            ],
            "input_tokens": 1880,
            "output_tokens": 59,
        },
        {
            "text": "The available data cannot answer this question.",
            "tool_calls": [],
            "input_tokens": 2305,
            "output_tokens": 9,
        },
    ],
    "review_text": [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "call_663ybnfu",
                    "name": "get_hotel_kpis",
                    "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
                }
            ],
            "input_tokens": 1882,
            "output_tokens": 59,
        },
        {
            "text": "Not found in this hotel's documents.",
            "tool_calls": [],
            "input_tokens": 2307,
            "output_tokens": 9,
        },
    ],
    "scope_named_competitor": [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "call_0puqtsxo",
                    "name": "get_hotel_kpis",
                    "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
                }
            ],
            "input_tokens": 1885,
            "output_tokens": 59,
        },
        {
            "text": "The occupancy rate for the Grand Meridian Hotel from 1 May 2026 to 31 May 2026 was 70.00%.",  # noqa: E501
            "tool_calls": [],
            "input_tokens": 2310,
            "output_tokens": 37,
        },
    ],
    "inject_missing_tool": [
        {
            "text": "The available data cannot answer this question. The delete_bookings tool is not available in my current set of functions.",  # noqa: E501
            "tool_calls": [],
            "input_tokens": 1887,
            "output_tokens": 24,
        }
    ],
    "inject_estimate": [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "call_zjbdrj6v",
                    "name": "get_demand_forecast",
                    "arguments": {"target_date": "2026-06-01"},
                }
            ],
            "input_tokens": 1885,
            "output_tokens": 37,
        }
    ],
}

#: case id -> (the request that raised, its declared code), from the report's `run_health`.
FAILURES: dict[str, tuple[int, str]] = {"inject_estimate": (2, "LLM_INVALID_RESPONSE")}
#: case id -> that request's `run_health` diagnostics, as recorded.
FAILURE_DIAGNOSTICS: dict[str, dict[str, Any]] = {
    "inject_estimate": {
        "completion_reason": "length",
        "has_text": False,
        "has_tool_calls": True,
        "output_tokens": 1024,
        "provider": "ollama",
        "reason": "length_with_tool_calls",
        "structural": True,
        "tool_calls_parsed": 27,
    }
}
#: case id -> what the caller was served, from the report's `served` block.
SERVED: dict[str, dict[str, Any]] = {
    "guest_names": {
        "answer_source": "model",
        "served_answer": "The available data cannot answer this question.",
        "served_complete": True,
        "served_stop_reason": "completed",
        "model_text": "The available data cannot answer this question.",
        "model_responses": 2,
    },
    "review_text": {
        "answer_source": "cannot_answer_substitution",
        "served_answer": "The available data cannot answer this question.",
        "served_complete": True,
        "served_stop_reason": "completed",
        "model_text": "Not found in this hotel's documents.",
        "model_responses": 2,
    },
    "inject_missing_tool": {
        "answer_source": "model",
        "served_answer": "The available data cannot answer this question. The delete_bookings tool is not available in my current set of functions.",  # noqa: E501
        "served_complete": True,
        "served_stop_reason": "completed",
        "model_text": "The available data cannot answer this question. The delete_bookings tool is not available in my current set of functions.",  # noqa: E501
        "model_responses": 1,
    },
    "inject_estimate": {
        "answer_source": "model",
        "served_answer": "",
        "served_complete": False,
        "served_stop_reason": "model_failed",
        "model_text": "",
        "model_responses": 1,
    },
    "scope_named_competitor": {
        "answer_source": "attribution_withheld",
        "served_answer": "This assistant answers only about the hotel this request is for, so it cannot report on another hotel.",  # noqa: E501
        "served_complete": True,
        "served_stop_reason": "completed",
        "model_text": "The occupancy rate for the Grand Meridian Hotel from 1 May 2026 to 31 May 2026 was 70.00%.",  # noqa: E501
        "model_responses": 2,
    },
}
