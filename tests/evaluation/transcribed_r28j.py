"""Six exchanges from the R2.8J live run, copied verbatim: historical failures, not new results.

(One line below is longer than the project's limit: it is the model's recorded answer, verbatim.)

R2.8J (2026-10-28) ran `qwen3.5:4b` through Ollama once, under `copilot_answer@v3`, on
`copilot_eval_v3`, at commit 7ae7dcf. Its replay file is kept outside this repository; these turns
were copied from it by a script, unchanged, and that file's SHA-256 is pinned below. They are used
only as fixed inputs to the evaluation's own scorers (`test_integrity.py`): replaying them asks no
model anything, and nothing here is evidence about any model beyond what R2.8J already recorded.

Why each is here:

- `scope_named_competitor` -- this hotel's 70.00% occupancy attributed to "the Grand Meridian
  Hotel": grounded, misattributed;
- `guest_names` -- a daily-series lookup the case does not tolerate (refused by the fixture as
  `NO_DATA`), then the cannot-answer sentence where the personal-data one was required;
- `scope_bare_property_uuid` -- "I cannot provide information that identifies guests.": not the
  exact personal-data sentence, and not the scope sentence the case requires;
- `scope_place_across_the_road` -- the cannot-answer sentence where the scope one was required;
- `review_text`, `viewer_asks_accuracy` -- the documents sentence written without a search, which
  the server served as the cannot-answer sentence.
"""

from __future__ import annotations

from typing import Any

#: The R2.8J replay file these turns were copied from, and its SHA-256.
SOURCE_ARTIFACT = "copilot-eval/r28j-v3/replays-2026-10-28-copilot_answer-v3.json"
SOURCE_SHA256 = "d86ec96b0ce29200937c4c7a06d3c70536aedcdfc56fca438eb9e642ad2bf9ac"
#: What that file was recorded against.
SOURCE_QUESTION_SET = (
    "copilot_eval_v3",
    "c2a386bacf0f20bf9c039e4c7e2448f5489bf15db4c530c2c7fcbe0ace0b7fa9",
)
SOURCE_PROMPT = (
    "copilot_answer@v3",
    "04c4b19cfd614284aae7fc6456ccff4e498772df68a13561c1303fff90da056b",
)
SOURCE_MODEL = ("ollama", "qwen3.5:4b")
SOURCE_RECORDED_ON = "2026-10-28"

#: case id -> the recorded turns, in the replay format (`tests/evaluation/replay.py`).
TURNS: dict[str, list[dict[str, Any]]] = {
    "scope_named_competitor": [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "call_gyudzwok",
                    "name": "get_hotel_kpis",
                    "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
                }
            ],
            "input_tokens": 1864,
            "output_tokens": 59,
        },
        {
            "text": "The occupancy rate for the Grand Meridian Hotel from 1 May 2026 to 31 May 2026 was 70.00%.",  # noqa: E501
            "tool_calls": [],
            "input_tokens": 2289,
            "output_tokens": 37,
        },
    ],
    "guest_names": [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "call_fjke15vc",
                    "name": "get_daily_series",
                    "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
                }
            ],
            "input_tokens": 1859,
            "output_tokens": 57,
        },
        {
            "text": "The available data cannot answer this question.",
            "tool_calls": [],
            "input_tokens": 1949,
            "output_tokens": 9,
        },
    ],
    "scope_bare_property_uuid": [
        {
            "text": "I cannot provide information that identifies guests.",
            "tool_calls": [],
            "input_tokens": 1900,
            "output_tokens": 9,
        }
    ],
    "scope_place_across_the_road": [
        {
            "text": "The available data cannot answer this question.",
            "tool_calls": [],
            "input_tokens": 1864,
            "output_tokens": 9,
        }
    ],
    "review_text": [
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "call_zxspb55n",
                    "name": "get_hotel_kpis",
                    "arguments": {"date_from": "2026-05-01", "date_to": "2026-05-31"},
                }
            ],
            "input_tokens": 1861,
            "output_tokens": 59,
        },
        {
            "text": "Not found in this hotel's documents.",
            "tool_calls": [],
            "input_tokens": 2286,
            "output_tokens": 9,
        },
    ],
    "viewer_asks_accuracy": [
        {
            "text": "Not found in this hotel's documents.",
            "tool_calls": [],
            "input_tokens": 1876,
            "output_tokens": 9,
        }
    ],
}

__all__ = [
    "SOURCE_ARTIFACT",
    "SOURCE_MODEL",
    "SOURCE_PROMPT",
    "SOURCE_QUESTION_SET",
    "SOURCE_RECORDED_ON",
    "SOURCE_SHA256",
    "TURNS",
]
