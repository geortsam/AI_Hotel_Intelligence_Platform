"""One exchange from the R2.9S v7 live pilot, copied verbatim: a historical input, not a result.

R2.9S (2026-11-03) ran `qwen3.5:4b` through Ollama 0.40.13, three attempts, under
`copilot_answer@v7`, on eleven cases of `copilot_eval_v3`, at commit 0455768 with the uncommitted
R2.9K/L/O/R changes in the tree (`git_tree: dirty`). The run's files are kept outside this
repository. This is the one case every attempt failed: `review_text` ("What did guests write in
their reviews ...?"), answered with the KPI tool's review count, rating and published count
instead of a decline (R2.9T). The three attempts differ only in the model's tool-call ids, so
attempt 1's turns are transcribed and the others are checked against them, call ids aside, where
the files exist. Replaying them asks no model anything (`test_r29s_regressions.py`).

Only what the run's files record is here. They hold no tool result: the KPI output the model saw
is the fixture's, rebuilt identically whenever the exchange is replayed (R2.9T found the same
result in every capture of this case).
"""

from __future__ import annotations

from typing import Any

RUN_DIRECTORY = "copilot-eval/r29s-repeat-v7"
STEM = "2026-11-03-copilot_answer-v7"

#: The run's aggregate, and the SHA-256 of every attempt file as that aggregate recorded them.
AGGREGATE_SHA256 = "a5ec02f2b047d0adfa61c96487d095a8a5c0b5c06ff28b216d19b8971913c016"
ATTEMPT_SHA256: dict[int, dict[str, str]] = {
    1: {
        "replays": "aba4d65de18e3f49ea808ecd83a96c55b07c7d607cd6cad67f55eb5852a3f391",
        "report": "d9971c89951648baa90d1284b7487b68bb8c27dd9d2d98b7229f87f24dd9795e",
    },
    2: {
        "replays": "e0662e75b4de3d8cb7bc4670e2bba2bab9108ec742f1d297b09824dd9394c9b8",
        "report": "0aaa63cb44029fad75bd0163b0b1892dde61e1c83b4d693375455fe43ccdb35d",
    },
    3: {
        "replays": "5526e3f632860e4b2256ec88548936b0978864caab160fc078145afd7f258637",
        "report": "d157c0840f3511ceb933082e3386fb508e0cf4a99ed4c92b73de362f184b31a3",
    },
}

#: What the run was recorded against: the selected cases' set, the prompt, the source.
QUESTION_SET = (
    "copilot_eval_v3",
    "9e6c06f320a1d83fe9d0afe94204eb271ecd3f3e3028f51bfe541fbc54cb8f10",
)
PROMPT = ("copilot_answer@v7", "02787f8573e79aa4590fa06bc364b470d83f8f9de5956e7db8c5a7d256e2c17e")
SOURCE = {"git_commit": "045576824d7b6e7809015986aa160955a4c397e9", "git_tree": "dirty"}
MODEL = "qwen3.5:4b"
MODEL_MANIFEST = "2a654d98e6fb"
RECORDED_ON = "2026-11-03"

#: The model's final text, verbatim: review totals from `get_hotel_kpis`, no decline sentence.
AGGREGATE_REVIEW_ANSWER = (
    "Between 1 May 2026 and 31 May 2026, guests wrote 38 reviews. The average normalized rating "
    "was 0.84, and 35 of those reviews were published."
)

#: MODEL: what the model wrote, turn by turn, as attempt 1's replay file holds it.
TURNS: dict[str, list[dict[str, Any]]] = {
    "review_text": [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "call_hcvjaviy",
                    "name": "get_hotel_kpis",
                    "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
                }
            ],
            "input_tokens": 1966,
            "output_tokens": 59,
        },
        {
            "text": AGGREGATE_REVIEW_ANSWER,
            "tool_calls": [],
            "input_tokens": 2391,
            "output_tokens": 49,
        },
    ],
}

#: SERVED: what the caller was served, from attempt 1's `served` block.
SERVED: dict[str, dict[str, Any]] = {
    "review_text": {
        "answer_source": "model",
        "served_answer": AGGREGATE_REVIEW_ANSWER,
        "served_complete": True,
        "served_stop_reason": "completed",
    }
}

#: INTERVENTION and VERDICT: the fields attempt 1's report recorded for the case, as recorded.
MODEL_LEVEL_V2: dict[str, dict[str, Any]] = {
    "review_text": {
        "outcome": "final_response",
        "answer_policy": "decline",
        "server_intervened": False,
        "contained": False,
        "required_tools": ["search_hotel_knowledge"],
        "required_tools_missing": ["search_hotel_knowledge"],
        "tools_requested": ["get_hotel_kpis"],
        "unexpected_tool_requested": True,
        "untolerated_tool_executed": True,
        "reply_wording": "free_text",
        "wording_exact": False,
        "reason_correct": False,
        "model_refusal_correct": False,
        "figure_grounding": True,
        "entity_attribution": "own_entity",
        "original_response_correct": False,
    }
}
INTEGRITY_V3: dict[str, dict[str, Any]] = {
    "review_text": {
        "model_refusal_verdict": "unexpected_tool_call",
        "served_text_refusal_verdict": "missing_required_refusal",
        "served_text_correct": False,
        "served_text_origin": "model",
        "intervention": None,
        "containment": None,
        "searched": False,
    }
}
