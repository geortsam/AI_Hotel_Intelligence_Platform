"""What the MODEL did, apart from the server and the infrastructure (R2.8B).

The report's top-level measures keep their meaning, and that meaning mixes three things: a case
the provider never answered scores `tool_selection` and `expected_figures` false, a case the
server refused before asking the model scores `refusal` and `grounding` true, and `completed`
counts every one of them. The `served` block (R2.7ZU) separates the served answer from the
model's text for declines. This block does the same for every measure the model's final response
can be held to, and is added only when a run asks for it (`harness.run(..., model_level=True)`),
so every pinned report stays exactly what it was.

## Each case has exactly one outcome, read from what the run recorded

Tested in this order; the first that applies is the outcome:

- `not_asked` -- the server's scope guard answered and no model response exists (R2.7ZL/ZT);
- `unavailable` -- the provider did not deliver the final response: the copilot raised a
  declared LLM failure (`error_code` is set, nothing was served), or the loop stopped at
  `model_failed` after a tool round;
- `tool_stopped` -- the tool loop stopped the exchange before the model wrote a final response:
  `tool_failed`, `max_rounds` or `tool_call_cap`;
- `final_response` -- the model finished (`completed`, or `ungrounded_figures`: the server
  withholding a text the model did finish) and its final text was recorded.

Anything else is refused with `ValueError` rather than guessed at.

## The measures, and who is eligible for each

Only `final_response` cases are ever scored, and only on the model's own final text and calls --
never on the served answer. Every measure reports `passed` of `scored`, and `excluded`: the cases
of its population that were not scored, by reason, so `scored + sum(excluded) == population`.
Counts only: no rate is computed, and `scored: 0` means nothing was measured.

- `tool_selection` -- population: answer cases. Scored when `final_response`. Passes when
  `scorers.tool_selection` holds: every expected call made, and nothing else called.
- `grounding` -- population: every case. Scored when `final_response` AND the final text states
  at least one figure. Passes when no figure is untraceable to the tool results the model saw in
  its final request, or to the question (`CaseResult.grounded`).
- `expected_figures` -- population: answer cases with expected figures. Scored when
  `final_response`. Passes when the final text states every one (`scorers.missing_figures`).
- `refusal_with_text` -- population: decline cases. Scored when `final_response`. Passes when
  `model_decline_status == "passes"` (R2.7ZW): non-empty, no new figure, no untolerated tool.

A final text that states no figure is excluded from `grounding` as `no_figures`: it is
vacuously grounded, and counting it would score an absence of evidence as a success.
`expected_figures` here does not require the served response to be complete, unlike the top-level
measure: a text the server withheld is still the model's, and still states what it states.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from tests.evaluation import scorers
from tests.evaluation.questions import EvalCase

if TYPE_CHECKING:
    from tests.evaluation.harness import CaseResult, ServedResult

MODEL_LEVEL_VERSION = "copilot_model_level_v1"

#: Every outcome a case can have, in the order they are tested.
OUTCOMES = ("not_asked", "unavailable", "tool_stopped", "final_response")
#: Loop stops at which the tool loop, not the model, ended the exchange.
TOOL_STOPS = frozenset({"tool_failed", "max_rounds", "tool_call_cap"})
#: Stops at which the model had finished writing (`harness._MODEL_FINISHED`).
MODEL_FINISHED = frozenset({"completed", "ungrounded_figures"})
#: Why a final response is left out of `grounding`.
NO_FIGURES = "no_figures"

STATEMENT = (
    "The model's final response alone: what it called, what it wrote and the figures in it. A "
    "case the server refused before asking the model (not_asked), one the provider did not "
    "answer (unavailable) and one the tool loop stopped (tool_stopped) is in no measure's "
    "denominator, and is counted under 'outcomes' instead. Served answers are scored in the "
    "'served' block, never here. Counts only; scored 0 means nothing was measured."
)

#: The eligibility rule of each measure, as the report states it.
RULES: dict[str, str] = {
    "tool_selection": "answer cases with a final model response",
    "grounding": "cases with a final model response that states at least one figure",
    "expected_figures": "answer cases with expected figures and a final model response",
    "refusal_with_text": "decline cases with a final model response",
}


def outcome(result: CaseResult, served: ServedResult) -> str:
    """The case's one outcome. See the module docstring for the order and the meaning."""
    if served.answer_source == "scope_guard" and served.model_responses == 0:
        return "not_asked"
    if result.error_code is not None or result.stop_reason == "model_failed":
        return "unavailable"
    if result.stop_reason in TOOL_STOPS:
        return "tool_stopped"
    if result.stop_reason in MODEL_FINISHED and served.model_text is not None:
        return "final_response"
    raise ValueError(
        f"{result.case_id}: stop {result.stop_reason!r} with "
        f"{served.model_responses} model response(s) has no model-level outcome"
    )


def _row(case: EvalCase, result: CaseResult, served: ServedResult) -> dict[str, Any]:
    """One case: its outcome, and per measure True / False, or None when it is not scored."""
    kind = outcome(result, served)
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "expected": case.expected,
        "outcome": kind,
        "tool_selection": None,
        "grounding": None,
        "expected_figures": None,
        "refusal_with_text": None,
    }
    if kind != "final_response":
        return row
    if scorers.figures_in(served.model_text or ""):
        row["grounding"] = result.grounded is True
    if case.expected == "answer":
        row["tool_selection"] = result.tool_selection is True
        if case.figures:
            row["expected_figures"] = not result.missing_figures
    else:
        row["refusal_with_text"] = served.model_decline_status == "passes"
    return row


def _measure(
    rows: Sequence[dict[str, Any]], name: str, population: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    members = [row for row in rows if population(row)]
    scored = [row[name] for row in members if row[name] is not None]
    excluded = Counter(
        NO_FIGURES if row["outcome"] == "final_response" else row["outcome"]
        for row in members
        if row[name] is None
    )
    return {
        "passed": sum(1 for value in scored if value),
        "scored": len(scored),
        "excluded": dict(sorted(excluded.items())),
        "eligibility": RULES[name],
    }


def block(
    cases: Sequence[EvalCase],
    results: Sequence[CaseResult],
    served: Sequence[ServedResult],
) -> dict[str, Any]:
    """The `model_level` block of a report, from the run's own case and served results, which
    must name the same cases in the same order."""
    if [c.case_id for c in cases] != [r.case_id for r in results] or [
        r.case_id for r in results
    ] != [s.case_id for s in served]:
        raise ValueError("cases, results and served rows must name the same cases in order")
    by_id = {case.case_id: case for case in cases}
    rows = [_row(by_id[r.case_id], r, s) for r, s in zip(results, served, strict=True)]
    outcomes = Counter(row["outcome"] for row in rows)
    figures = {case.case_id for case in cases if case.figures}
    return {
        "version": MODEL_LEVEL_VERSION,
        "statement": STATEMENT,
        "outcomes": {kind: outcomes[kind] for kind in OUTCOMES},
        "measures": {
            "tool_selection": _measure(rows, "tool_selection", lambda r: r["expected"] == "answer"),
            "grounding": _measure(rows, "grounding", lambda r: True),
            "expected_figures": _measure(
                rows,
                "expected_figures",
                lambda r: r["expected"] == "answer" and r["case_id"] in figures,
            ),
            "refusal_with_text": _measure(
                rows, "refusal_with_text", lambda r: r["expected"] == "decline"
            ),
        },
        "cases": rows,
    }


__all__ = [
    "MODEL_FINISHED",
    "MODEL_LEVEL_VERSION",
    "NO_FIGURES",
    "OUTCOMES",
    "RULES",
    "STATEMENT",
    "TOOL_STOPS",
    "block",
    "outcome",
]
