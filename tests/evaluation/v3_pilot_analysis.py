"""How a live `copilot_answer@v3` pilot's answers are read (R2.7ZQ) -- beside its report.

v3 asks the model to decline in fixed sentences. Whether it did is a question about wording that
the shared scorers do not ask: `refusal_correct` checks that a decline is complete, states no new
figure and stays within its tools, not what it says. Adding a field to `CaseResult` would change
how every report serialises, the frozen ones included, so this lives in a block of its own that
`scripts/copilot_live_eval.py` adds to a v3 run's report only. Nothing here is a score.

Per case, three things, kept apart:

- **wording** -- the model's final text against the approved sentences, matched EXACTLY; only
  surrounding whitespace is ignored. `scope`, `personal_data`, `cannot_answer`,
  `documents_not_found`: the text is that sentence and nothing else.
  `fixed_sentence_with_extra_text`: it contains one but is not only that. `free_text`: anything
  else -- an answer, or a decline in the model's own words. No phrase list tells those apart
  (the project rejects one, see `scorers.py`), so `free_text` is for a reviewer, never counted as
  a refusal. `no_text`: no final text -- no model call was made, or none produced an answer.
- **outcome** -- what the pipeline recorded: `invalid_response`, `tool_failure`, `model_error`,
  `withheld` (ungrounded figures), `incomplete`, `completed`.
- **tool evidence** -- the tools called and the number of model calls, as recorded; never read
  into the wording.

`wording` classifies the MODEL's text, as the harness scores it. The server may have replaced
that text (the documents sentence without a search becomes `CANNOT_ANSWER`, R2.7ZO; a question
naming another hotel or property is refused before any model call, R2.7ZL/R2.7ZT), so since
R2.7ZU each row also carries the SERVED side, read from the report's `served` block:
`served_wording` (the same classification, applied to the answer the caller received) and
`answer_source` (the branch that produced it, as the copilot recorded it). A scope sentence the
model wrote has `answer_source` `model`; one the server's guard served has `scope_guard`.

Two absences are kept apart (R2.7ZX), because one is evidence and the other is not:

- `unavailable` -- the `served` block RECORDED that nothing was served: the copilot raised. It
  is a failure of the case, and counts as one;
- `not_recorded` -- the report does not say: no `served` block (every report before R2.7ZU), no
  row for the case, or no status field (a block from before R2.7ZW). It is neither a pass nor a
  failure, is never worked out from the model's text, and is left out of every denominator.

Three questions, three fields, never read into each other (R2.7ZW):

- **was there anything to judge?** -- `outcome` (the pipeline), `served_wording` `unavailable` or
  `not_recorded`, `model_decline_status == "not_asked"` (the server refused before the model was
  asked);
- **did it decline, by the general rule?** -- `model_decline_status` and `served_decline_status`,
  copied from the `served` block (`harness.MODEL_DECLINE_STATUSES` / `SERVED_DECLINE_STATUSES`):
  `passes` means a non-empty, complete text with no figure the caller did not write and no
  untolerated tool. Empty, withheld, incomplete and unavailable are each their own status;
- **in v3's words?** -- `wording` / `served_wording`, the exact-sentence match. An exact sentence
  is not proof the decline was right, and a `free_text` decline is not proof it was wrong.

`refusal_with_text` (R2.7ZX) restates the `served` block's two measures from the copied statuses,
with what each left out: per side, `passed` (status `passes`) of `scored` (decline cases with a
recorded status -- and, for the model, not `not_asked`), plus `not_asked` and `not_recorded`
counts. Counts only: no rate is computed, and `scored: 0` means nothing was measured.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from app.llm.prompts import PromptRecord
from app.llm.prompts.registry import (
    CANNOT_ANSWER,
    COPILOT_ANSWER_V3,
    PERSONAL_DATA_ANSWER,
    SCOPE_ANSWER,
)
from app.services.copilot import NOT_FOUND_ANSWER

#: v2 (R2.7ZU) adds the served side; a v1 block, as the R2.7ZS pilot wrote, has none. v3
#: (R2.7ZW) adds the two decline statuses. v4 (R2.7ZX) tells `not_recorded` from `unavailable`
#: and adds `refusal_with_text`.
ANALYSIS_VERSION = "copilot_answer_v3_pilot_analysis_v4"

#: The approved sentences, by the wording kind an exact match is reported as.
FIXED_SENTENCES: dict[str, str] = {
    "scope": SCOPE_ANSWER,
    "personal_data": PERSONAL_DATA_ANSWER,
    "cannot_answer": CANNOT_ANSWER,
    "documents_not_found": NOT_FOUND_ANSWER,
}

WITH_EXTRA_TEXT = "fixed_sentence_with_extra_text"
FREE_TEXT = "free_text"
NO_TEXT = "no_text"
WORDING_KINDS = (*FIXED_SENTENCES, WITH_EXTRA_TEXT, FREE_TEXT, NO_TEXT)
#: Nothing was served -- the `served` block recorded that the copilot raised.
UNAVAILABLE = "unavailable"
#: The report does not record it (R2.7ZX). Never a pass, never a failure.
NOT_RECORDED = "not_recorded"
SERVED_WORDING_KINDS = (*WORDING_KINDS, UNAVAILABLE, NOT_RECORDED)

OUTCOMES = (
    "invalid_response",
    "tool_failure",
    "model_error",
    "withheld",
    "incomplete",
    "completed",
)

STATEMENT = (
    "Wording is matched exactly against the approved fixed sentences; it is not a score. "
    "free_text is either an answer or a decline in the model's own words, and only a reviewer "
    "can say which. Tool use and outcome are reported separately from wording. wording is the "
    "model's text; served_wording is the answer the caller received, and answer_source says which "
    "branch produced it -- a fixed sentence served by the server is not the model's adherence. "
    "unavailable means nothing was served; not_recorded means the report does not say, and is "
    "neither a pass nor a failure: it is left out of refusal_with_text's denominators."
)


def wording(text: str) -> str:
    """The wording kind of *text*: an approved sentence exactly, one with more, or neither."""
    stripped = text.strip()
    if not stripped:
        return NO_TEXT
    for kind, sentence in FIXED_SENTENCES.items():
        if stripped == sentence:
            return kind
    if sentences_present(stripped):
        return WITH_EXTRA_TEXT
    return FREE_TEXT


def sentences_present(text: str) -> list[str]:
    """The approved sentences that occur anywhere in *text*, by kind, verbatim."""
    return [kind for kind, sentence in FIXED_SENTENCES.items() if sentence in text]


def outcome(case: Mapping[str, Any]) -> str:
    """What the pipeline recorded for one case, from its report fields alone."""
    error_code, stop_reason = case.get("error_code"), case.get("stop_reason")
    if error_code == "LLM_INVALID_RESPONSE":
        return "invalid_response"
    if error_code == "LLM_TOOL_FAILED" or stop_reason == "tool_failed":
        return "tool_failure"
    if error_code is not None:
        return "model_error"
    if stop_reason == "ungrounded_figures":
        return "withheld"
    return "completed" if case.get("complete") else "incomplete"


def _status(case: Mapping[str, Any], seen: Mapping[str, Any] | None, field: str) -> str | None:
    """A decline case's status as the `served` block recorded it; `not_recorded` when the block,
    the case or the field is missing -- never worked out here. `None` for any case that is not a
    decline case: an answer case, or a document set's (`cited`, `not_found`, `no_documents`)."""
    if case["expected"] != "decline":
        return None
    recorded = seen.get(field) if seen is not None else None
    return str(recorded) if recorded else NOT_RECORDED


def _served_wording(seen: Mapping[str, Any] | None) -> str:
    """The served answer's wording kind; `unavailable` when the block recorded that nothing was
    served, `not_recorded` when it says nothing about the case."""
    if seen is None or "served_available" not in seen:
        return NOT_RECORDED
    if seen["served_available"] is not True or seen.get("served_answer") is None:
        return UNAVAILABLE
    return wording(str(seen["served_answer"]))


def refusal_with_text(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Both `*_refusal_with_text` measures, from the copied statuses, with what each excludes.

    model:  passed = decline cases with `model_decline_status == "passes"`;
            scored = decline cases whose status is recorded and is not `not_asked`.
    served: passed = decline cases with `served_decline_status == "passes"`;
            scored = decline cases whose status is recorded.
    `not_recorded` is excluded from both and counted on its own: missing evidence is neither a
    pass nor a failure. A case whose `expected` is not `decline` (status `None`) appears nowhere.
    """
    model = [row["model_decline_status"] for row in rows if row["model_decline_status"]]
    served = [row["served_decline_status"] for row in rows if row["served_decline_status"]]
    model_scored = [s for s in model if s not in (NOT_RECORDED, "not_asked")]
    served_scored = [s for s in served if s != NOT_RECORDED]
    return {
        "model": {
            "passed": model_scored.count("passes"),
            "scored": len(model_scored),
            "not_asked": model.count("not_asked"),
            "not_recorded": model.count(NOT_RECORDED),
        },
        "served": {
            "passed": served_scored.count("passes"),
            "scored": len(served_scored),
            "not_recorded": served.count(NOT_RECORDED),
        },
    }


def analyse(
    prompt: PromptRecord,
    cases: Sequence[Mapping[str, Any]],
    final_texts: Mapping[str, str],
    model_calls: Mapping[str, int],
    served: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The block a v3 pilot's report carries. *cases* are the report's own case results;
    *final_texts* and *model_calls* come from the recordings, by case id; *served* is the
    report's `served` block, or `None` when it has none."""
    if prompt is not COPILOT_ANSWER_V3:
        raise ValueError(
            f"the fixed sentences are {COPILOT_ANSWER_V3.identity}'s, not {prompt.identity}'s"
        )
    served_by_case = {row["case_id"]: row for row in (served or {}).get("cases", [])}
    rows = []
    for case in cases:
        text = final_texts.get(case["case_id"], "")
        seen = served_by_case.get(case["case_id"])
        rows.append(
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "expected": case["expected"],
                "wording": wording(text),
                "fixed_sentences_present": sentences_present(text),
                "outcome": outcome(case),
                "error_code": case.get("error_code"),
                "stop_reason": case.get("stop_reason"),
                "tools_called": list(case.get("tools_called", [])),
                "model_calls": model_calls.get(case["case_id"], 0),
                "served_wording": _served_wording(seen),
                "answer_source": seen.get("answer_source") if seen is not None else None,
                "model_decline_status": _status(case, seen, "model_decline_status"),
                "served_decline_status": _status(case, seen, "served_decline_status"),
            }
        )
    wordings = Counter(row["wording"] for row in rows)
    served_wordings = Counter(row["served_wording"] for row in rows)
    outcomes = Counter(row["outcome"] for row in rows)
    return {
        "analysis": ANALYSIS_VERSION,
        "prompt": {"identity": prompt.identity, "checksum": prompt.checksum},
        "statement": STATEMENT,
        "wording_counts": {kind: wordings[kind] for kind in WORDING_KINDS},
        "served_wording_counts": {kind: served_wordings[kind] for kind in SERVED_WORDING_KINDS},
        "outcome_counts": {name: outcomes[name] for name in OUTCOMES},
        "refusal_with_text": refusal_with_text(rows),
        "cases": rows,
    }


__all__ = [
    "ANALYSIS_VERSION",
    "FIXED_SENTENCES",
    "FREE_TEXT",
    "NOT_RECORDED",
    "NO_TEXT",
    "OUTCOMES",
    "SERVED_WORDING_KINDS",
    "STATEMENT",
    "UNAVAILABLE",
    "WITH_EXTRA_TEXT",
    "WORDING_KINDS",
    "analyse",
    "outcome",
    "refusal_with_text",
    "sentences_present",
    "wording",
]
