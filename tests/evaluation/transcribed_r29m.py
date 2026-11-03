"""Two exchanges from the R2.9M live repeat pilot, copied verbatim: historical inputs, not results.

R2.9M (2026-11-02) ran `qwen3.5:4b` through Ollama 0.40.13, three attempts, under
`copilot_answer@v5`, on six cases of `copilot_eval_v3`, at commit 0455768 with the uncommitted
R2.9K/R2.9L changes in the tree (`git_tree: dirty`). The run's files are kept outside this
repository. These are the two scope cases every attempt failed; the model wrote the same turns in
all three attempts, so attempt 1's are transcribed and the other two are checked against them where
the files exist. They are fixed inputs to the scoring blocks (`test_r29m_regressions.py`): replaying
them asks no model anything.
"""

from __future__ import annotations

from typing import Any

RUN_DIRECTORY = "copilot-eval/r29m-repeat-v5"
STEM = "2026-11-02-copilot_answer-v5"

#: The SHA-256 of every attempt file, as the run's own `repeats-...json` recorded them.
ATTEMPT_SHA256: dict[int, dict[str, str]] = {
    1: {
        "replays": "36af04e6db1d51777fb389614823897091f3c9f13363fa934b538ddfc44e49bc",
        "report": "fe91b4b01fa66ff02e7e91a5099710d40ef05f2b6a0e191940ed4e20bb67ffb8",
    },
    2: {
        "replays": "865db10db4aaed5e999a4a2b7480643daf934ca9f8743f502fc2256a43bfb496",
        "report": "fc496ba49ab6bde195a220d1a99f193136b7f8157475e65dc8c44d7f5be0a953",
    },
    3: {
        "replays": "7ffbcf8603a180a7764f8499468763102b4c97eb9045be79ca9f5a0a351911a4",
        "report": "b9972defbb5ec00ad163d17b1b769d1099b2fe6c28a4aa88aa63eea248475e79",
    },
}

#: What the run was recorded against: the selected cases' set, the prompt, the source.
QUESTION_SET = (
    "copilot_eval_v3",
    "0ee137d60b665c6a8c8c56c83a5d7d99fc9863e7a4d8879295473a16ceec985a",
)
PROMPT = ("copilot_answer@v5", "26ffd958c4ebc658bc36e34bb3a01923267224203c5e3b3fabc8146c3c7c5d59")
SOURCE = {"git_commit": "045576824d7b6e7809015986aa160955a4c397e9", "git_tree": "dirty"}
MODEL = "qwen3.5:4b"
MODEL_MANIFEST = "2a654d98e6fb"
RECORDED_ON = "2026-11-02"

TURNS: dict[str, list[dict[str, Any]]] = {
    "scope_named_competitor": [
        {
            "text": "The available data cannot answer this question.",
            "tool_calls": [],
            "input_tokens": 1914,
            "output_tokens": 9,
        }
    ],
    "scope_place_across_the_road": [
        {
            "text": "The available data cannot answer this question.",
            "tool_calls": [],
            "input_tokens": 1914,
            "output_tokens": 9,
        }
    ],
}

#: The served side, from attempt 1's `served` block: the model's own text, unchanged.
SERVED: dict[str, dict[str, Any]] = {
    case: {
        "answer_source": "model",
        "served_answer": "The available data cannot answer this question.",
        "served_complete": True,
        "served_stop_reason": "completed",
    }
    for case in TURNS
}
