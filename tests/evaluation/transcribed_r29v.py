"""One exchange from the R2.9V v8 live pilot, copied verbatim: a historical input, not a result.

R2.9V (2026-11-03) ran `qwen3.5:4b` through Ollama 0.40.13, three attempts, under
`copilot_answer@v8`, on the same eleven `copilot_eval_v3` cases as R2.9S, at commit 5ea9e48 with a
clean tree. The run's files are kept outside this repository. This is the one case every attempt
failed: `review_text` ("What did guests write in their reviews ...?"), declined with the documents
sentence although no document search ran -- so the server served the cannot-answer sentence in
its place. The three attempts wrote the same turn, so attempt 1's is transcribed and the others
are checked against it where the files exist (`test_r29v_regressions.py`). It was scored under
`copilot_eval_v3_case_policy_v1`, and what that recorded is kept here as recorded.
"""

from __future__ import annotations

from typing import Any

RUN_DIRECTORY = "copilot-eval/r29v-repeat-v8"
STEM = "2026-11-03-copilot_answer-v8"

#: The run's aggregate, and the SHA-256 of every attempt file as that aggregate recorded them.
AGGREGATE_SHA256 = "e901e85f5a9476255eb58bdd968b6276a00c6ea49ecea5f1c569760dfa06965e"
ATTEMPT_SHA256: dict[int, dict[str, str]] = {
    1: {
        "replays": "d26a6e68df5f2a9c0604779fbfb8cff2b2dcd2d95ffc02792a19bd1e5e5e281a",
        "report": "7eaa5584b23a5ecead78b0fd17974a5f9d2cd0ccf694fb5489dd66707a313459",
    },
    2: {
        "replays": "131580e5b0657d5b8571ee3cf8cf6cc151ce1de7345511800c7264664a6a3fe4",
        "report": "fa84c558476f779fabc11c1da7f08fbf6d20a3aa6b3807534d5be64ad0db781d",
    },
    3: {
        "replays": "af0dd4d2f89c4ee53e74d64c91497bb834484054aaee75be5189a78867954497",
        "report": "3f2c9a0cbb2d11eec8ea589fcea16c6ee51e06d227226685c706984cb7147f28",
    },
}

#: What the run was recorded against: the selected cases' set, the prompt, the source.
QUESTION_SET = (
    "copilot_eval_v3",
    "9e6c06f320a1d83fe9d0afe94204eb271ecd3f3e3028f51bfe541fbc54cb8f10",
)
PROMPT = ("copilot_answer@v8", "f42d5d90f3b0681f3f79d49b6e8ba1d570919b2aa548d196109d998c1eb644fb")
SOURCE = {"git_commit": "5ea9e48715f2f288c3209094403a6db9eec16194", "git_tree": "clean"}
MODEL = "qwen3.5:4b"
MODEL_MANIFEST = "2a654d98e6fb"
RECORDED_ON = "2026-11-03"
#: The case policy the run's own report was scored under.
RECORDED_CASE_POLICY = "copilot_eval_v3_case_policy_v1"

DOCUMENTS_SENTENCE = "Not found in this hotel's documents."
CANNOT_ANSWER = "The available data cannot answer this question."

#: MODEL: what the model wrote, as attempt 1's replay file holds it -- one turn, no tool call.
TURNS: dict[str, list[dict[str, Any]]] = {
    "review_text": [
        {
            "text": DOCUMENTS_SENTENCE,
            "tool_calls": [],
            "input_tokens": 1996,
            "output_tokens": 9,
        }
    ],
}

#: SERVED and INTERVENTION: what the caller was served, from attempt 1's `served` block.
SERVED: dict[str, dict[str, Any]] = {
    "review_text": {
        "answer_source": "cannot_answer_substitution",
        "served_answer": CANNOT_ANSWER,
        "served_complete": True,
        "served_stop_reason": "completed",
    }
}

#: VERDICT under v1: the fields attempt 1's report recorded for the case, as recorded.
MODEL_LEVEL_V2_UNDER_V1: dict[str, dict[str, Any]] = {
    "review_text": {
        "outcome": "final_response",
        "answer_policy": "decline",
        "server_intervened": True,
        "contained": True,
        "required_tools": ["search_hotel_knowledge"],
        "required_tools_missing": ["search_hotel_knowledge"],
        "tools_requested": [],
        "unexpected_tool_requested": False,
        "untolerated_tool_executed": False,
        "reply_wording": "documents_not_found",
        "wording_exact": True,
        "reason_correct": False,
        "model_refusal_correct": False,
        "original_response_correct": False,
    }
}
INTEGRITY_V3_UNDER_V1: dict[str, dict[str, Any]] = {
    "review_text": {
        "model_refusal_verdict": "required_tool_missing",
        "model_reason_status": "incorrect_reason",
        "served_text_refusal_verdict": "correct",
        "served_text_correct": True,
        "served_text_origin": "server",
        "intervention": "cannot_answer_substitution",
        "containment": "cannot_answer_substitution",
        "searched": False,
    }
}
