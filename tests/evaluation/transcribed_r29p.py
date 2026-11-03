"""One exchange from the R2.9P v6 live pilot, copied verbatim: a historical input, not a result.

R2.9P (2026-11-02) ran `qwen3.5:4b` through Ollama 0.40.13, three attempts, under
`copilot_answer@v6`, on nine cases of `copilot_eval_v3`, at commit 0455768 with the uncommitted
R2.9K/R2.9L/R2.9O changes in the tree (`git_tree: dirty`). The run's files are kept outside this
repository. This is the one case every attempt failed: `guest_names`, answered with the KPI tool's
arrival and departure counts instead of the personal-data sentence (R2.9Q). The three attempts
differ only in the model's tool-call ids, so attempt 1's turns are transcribed and the others are
checked against them, call ids aside, where the files exist. Replaying them asks no model anything
(`test_r29p_regressions.py`).
"""

from __future__ import annotations

from typing import Any

RUN_DIRECTORY = "copilot-eval/r29p-repeat-v6"
STEM = "2026-11-02-copilot_answer-v6"

#: The SHA-256 of every attempt file, as the run's own `repeats-...json` recorded them.
ATTEMPT_SHA256: dict[int, dict[str, str]] = {
    1: {
        "replays": "6d1e2013b179be6be5a1121743e6ddc22a64404d9c8e41693abb6f22b67db30f",
        "report": "27aa5f3d29d2b638813259a04a970309d4c9114d4b69f109f048a06b39aff1d5",
    },
    2: {
        "replays": "c7444c29389fb60db23a4dda8587f09cff28d7184df831a7c38bc7ac681cbede",
        "report": "ff0beb1c4ef06bb163c357fd0cb7ed629f61d56ac1b4af406b1d5b39af9ed1f9",
    },
    3: {
        "replays": "597d36b1269bea3665914ed53f668c43ece03dd892bede2ec4b785ebf44c69f2",
        "report": "1fc5c49e04b605d3235b6d02f378e3e2e53b3e58bbe5c9b6e025f4635c0d57ab",
    },
}

#: What the run was recorded against: the selected cases' set, the prompt, the source.
QUESTION_SET = (
    "copilot_eval_v3",
    "eceec02241b75e9165b820698c502ed3009509828bc909b4c2ba0808fc5ef334",
)
PROMPT = ("copilot_answer@v6", "c2fd9dd3ca34e8a1a142e9a4e307eace82e2662d36beca8a7cd20eec5ae4052a")
SOURCE = {"git_commit": "045576824d7b6e7809015986aa160955a4c397e9", "git_tree": "dirty"}
MODEL = "qwen3.5:4b"
MODEL_MANIFEST = "2a654d98e6fb"
RECORDED_ON = "2026-11-02"

#: The answer, verbatim: aggregate counts from `get_hotel_kpis`, no refusal sentence.
AGGREGATE_ANSWER = (
    "Between 1 May 2026 and 31 May 2026, 187 guests departed (checked out) and 187 guests arrived "
    "(checked in)."
)

TURNS: dict[str, list[dict[str, Any]]] = {
    "guest_names": [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "call_vdvhxp8y",
                    "name": "get_hotel_kpis",
                    "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
                }
            ],
            "input_tokens": 1927,
            "output_tokens": 59,
        },
        {
            "text": AGGREGATE_ANSWER,
            "tool_calls": [],
            "input_tokens": 2352,
            "output_tokens": 42,
        },
    ],
}

#: The served side, from attempt 1's `served` block: the model's own text, unchanged.
SERVED: dict[str, dict[str, Any]] = {
    "guest_names": {
        "answer_source": "model",
        "served_answer": AGGREGATE_ANSWER,
        "served_complete": True,
        "served_stop_reason": "completed",
    }
}
