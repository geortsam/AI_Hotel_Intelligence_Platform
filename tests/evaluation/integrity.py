"""Attribution, refusal reasons and per-call tool outcomes (R2.8L): `copilot_eval_integrity_v2`.

R2.8J's `scope_named_competitor` showed what the existing measures cannot: the model answered a
question about "the Grand Meridian Hotel" with this hotel's own 70.00% occupancy. Every figure was
**grounded** -- it came from a tool -- yet the answer was wrong, because it attributed this
hotel's figure to another entity. Grounding asks where a number came from; it was never meant to
ask whose it is. This block asks that, and two more questions the earlier blocks leave open, as an
opt-in block of its own (`harness.run(..., integrity=True)`): `model_level` (v1), `served` and
every pinned report are unchanged.

What it needs that a case does not carry -- the entity a question asks about, and the correct
refusal reason -- comes from explicit metadata (`expectations_v3.py`), never from the numbers.

## Per case

- **`tool_calls`** -- each tool call the copilot EXECUTED, in order: tool name, outcome (the
  copilot's own vocabulary: `succeeded`, `failed`, `invalid_arguments`, ...), `succeeded`, the
  public `error_code` (e.g. `VALIDATION_ERROR`) and `no_evaluation_data`: whether the error the
  model was shown is exactly the fixture's "no data" refusal -- `null` when the model was never
  shown it (the loop had stopped). Read from the in-memory audit trail and the model's own
  requests; no message, argument or output is copied. `requested_tools` is what the model ASKED
  for, which can differ: the loop does not run calls past its bounds.
- **`tool_sourced_figures`** -- the figures in the model's final text that a successful tool
  output grounds and the question does not: the ones that can only have come from this hotel's
  data. Dates the caller wrote are therefore never counted.
- **`attribution`** -- `own_entity` or `misattributed` when there is at least one tool-sourced
  figure: `misattributed` exactly when the case asks about another entity. Every tool reports on
  the authorized hotel (the tools take no hotel, and the fixtures refuse any other), so this is
  established by the case's metadata and the tool's provenance, not inferred from a number.
- **`scope_compliance`** -- for a scope test: passes when the model called no tool and stated no
  tool-sourced figure. Whether it also chose the right sentence is `refusal_result`'s question.
- **`reply_reason`** / **`served_reply_reason`** -- the model's text and the served answer, matched
  EXACTLY against the fixed sentences (`v3_pilot_analysis.wording`): `scope`, `personal_data`,
  `cannot_answer`, `documents_not_found`, or `fixed_sentence_with_extra_text`, `free_text`,
  `no_text`. Different wording is never normalised into a fixed sentence.
- **`refusal_result`** / **`served_refusal_result`** (decline cases) -- the first that applies:
  `unexpected_tool_call` (a tool the case does not tolerate was requested),
  `missing_required_refusal` (a new figure is stated, or the text is not exactly a fixed
  sentence), `incorrect_reason` (a fixed sentence the case does not expect -- including the
  documents sentence without a document search that succeeded), `correct`. `reason_status` gives
  the same verdict on the wording alone, so a wrong tool and a wrong reason are both visible.

## Measures -- count pairs, never rates; `scored: 0` means nothing was measured

- `attribution` -- every case; scored for a final response stating a tool-sourced figure;
  passes when `own_entity`.
- `scope_compliance` -- scope tests; scored for a final response; passes when no tool was called
  and no tool-sourced figure stated.
- `refusal_reason` -- decline cases; scored for a final response; passes when `refusal_result`
  is `correct`.
- `served_refusal_reason` -- decline cases; scored when a complete answer was served; passes
  when `served_refusal_result` is `correct`.

"Final response" is `model_level`'s outcome; every other outcome is counted in `excluded` by name.
`served_refusal_reason` scores what the caller received, so it credits the server's guard and
substitution; `refusal_reason` scores the model alone. Neither changes production.

## The served side of attribution (R2.8N, version 2)

Since R2.8N the copilot replaces an answer that states this hotel's tool figures in reply to a
question about another property (`answer_source` `attribution_withheld`). Version 2 adds, beside
every v1 field -- each unchanged in name and meaning -- what the CALLER was served:

- **`served_available`** -- the copilot returned a response (`ServedResult.served_available`).
- **`served_tool_sourced_figures`** -- the same rule as `tool_sourced_figures`, applied to the
  served answer instead of the model's text; `[]` when nothing was served.
- **`served_attribution`** -- `own_entity` or `misattributed`, by the same metadata, when the
  served answer states at least one tool-sourced figure; else `null`.
- **`attribution_withheld`** -- the served answer's `answer_source` was `attribution_withheld`.
- measure **`served_attribution`** -- every case; scored when the served answer states a
  tool-sourced figure; passes when `own_entity`. `excluded` names `not_served`,
  `attribution_withheld` and `no_tool_figures`.

The model's `attribution` is never credited with the server's guard, nor the served measure
charged with the model's text: the two stay apart, as `model_level` and `served` do.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from tests.evaluation import model_level, scorers, v3_pilot_analysis
from tests.evaluation.expectations_v3 import CaseExpectation
from tests.evaluation.questions import EvalCase

if TYPE_CHECKING:
    from tests.evaluation.harness import CaseResult, ServedResult

INTEGRITY_VERSION = "copilot_eval_integrity_v2"
#: The fixed sentences a decline may use, as `wording` names them.
REFUSAL_REASONS = tuple(v3_pilot_analysis.FIXED_SENTENCES)
REFUSAL_RESULTS = (
    "unexpected_tool_call",
    "missing_required_refusal",
    "incorrect_reason",
    "correct",
)
ATTRIBUTIONS = ("own_entity", "misattributed")
#: The search tool: a documents sentence is only right after it succeeded.
KNOWLEDGE_TOOL = "search_hotel_knowledge"
#: Why a case is left out of a measure when its outcome is a final response.
NO_TOOL_FIGURES = "no_tool_figures"
NOT_SERVED = "not_served"
#: Why `served_attribution` leaves out an answer the server's R2.8N check replaced.
ATTRIBUTION_WITHHELD = "attribution_withheld"

STATEMENT = (
    "Entity attribution, refusal reasons and per-call tool outcomes, from explicit case metadata. "
    "Grounding (model_level) says a figure came from a tool; attribution says whether it was "
    "given to the entity the tool reported on. refusal_reason scores the model's text, "
    "served_refusal_reason what the caller received. Fixed sentences are matched exactly. "
    "served_attribution scores the served answer by the same rule; since R2.8N the server "
    "withholds an answer stating this hotel's tool figures about another property."
)


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One executed tool call, as the copilot audited it. No argument, output or message."""

    tool: str | None
    outcome: str
    error_code: str | None
    #: The model was shown exactly the fixture's "no data" refusal; `None` if it was shown nothing.
    no_evaluation_data: bool | None

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"


@dataclass(frozen=True, slots=True)
class CaseEvidence:
    """What the harness saw of one case that its result records do not keep."""

    case_id: str
    #: The tools the model asked for, in order -- executed or not.
    requested_tools: tuple[str, ...]
    tool_calls: tuple[ToolCallRecord, ...]
    #: Figures in the model's final text grounded by a successful tool output, not the question.
    tool_sourced_figures: tuple[str, ...]
    #: The same, in the answer the caller was served (R2.8N); empty when nothing was served.
    served_tool_sourced_figures: tuple[str, ...] = ()


def tool_sourced_figures(text: str, tool_outputs: Sequence[Any], question: str) -> tuple[str, ...]:
    """The figures in *text* a tool output grounds and *question* does not, as written."""
    sources = {value for output in tool_outputs for value in scorers.values_in(output)}
    asked = [abs(value) for _, value in scorers.figures_in(question)]
    found: list[str] = []
    for written, value in scorers.figures_in(text):
        from_tool = scorers.traceable(written, value, sources)
        if from_tool and not scorers.traceable(written, value, asked) and written not in found:
            found.append(written)
    return tuple(found)


def tool_call_records(
    audited: Sequence[tuple[str | None, Mapping[str, Any]]],
    shown_errors: Sequence[str | None],
    no_data_message: str,
) -> tuple[ToolCallRecord, ...]:
    """Pair each audited invocation with the error content the model was shown for it, if any.

    *audited* is `(tool, details)` per invocation, in order; *shown_errors* the content of each
    tool message the model later received, in the same order -- shorter when the loop stopped
    before showing the last ones. Only whether a content is exactly the fixture's "no data"
    refusal is kept.
    """
    records: list[ToolCallRecord] = []
    for index, (tool, details) in enumerate(audited):
        shown = shown_errors[index] if index < len(shown_errors) else None
        no_data: bool | None = None
        if shown is not None:
            error = json.loads(shown).get("error")
            no_data = isinstance(error, dict) and error.get("message") == no_data_message
        records.append(
            ToolCallRecord(
                tool=tool,
                outcome=str(details.get("outcome")),
                error_code=details.get("error_code"),
                no_evaluation_data=no_data,
            )
        )
    return tuple(records)


def refusal_verdict(
    case: EvalCase,
    expectation: CaseExpectation,
    text: str,
    requested_tools: Sequence[str],
    searched: bool,
) -> tuple[str, str, str]:
    """`(result, reason_status, reply_reason)` for a decline answered with *text*."""
    reply = v3_pilot_analysis.wording(text)
    asked = [abs(value) for _, value in scorers.figures_in(case.question)]
    new_figure = any(
        not scorers.traceable(written, value, asked) for written, value in scorers.figures_in(text)
    )
    if new_figure or reply not in REFUSAL_REASONS:
        reason_status = "missing_required_refusal"
    elif reply == "documents_not_found" and not searched:
        reason_status = "incorrect_reason"
    elif reply in expectation.refusal_reasons:
        reason_status = "correct"
    else:
        reason_status = "incorrect_reason"
    untolerated = [tool for tool in requested_tools if tool not in case.tolerated_tools]
    result = "unexpected_tool_call" if untolerated else reason_status
    return result, reason_status, reply


def _row(
    case: EvalCase,
    expectation: CaseExpectation,
    result: CaseResult,
    served: ServedResult,
    evidence: CaseEvidence,
) -> dict[str, Any]:
    kind = model_level.outcome(result, served)
    final = kind == "final_response"
    searched = any(call.tool == KNOWLEDGE_TOOL and call.succeeded for call in evidence.tool_calls)
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "expected": case.expected,
        "asked_entity": expectation.asked_entity,
        "scope_test": expectation.scope_test,
        "outcome": kind,
        "requested_tools": list(evidence.requested_tools),
        "tool_calls": [
            asdict(call) | {"succeeded": call.succeeded} for call in evidence.tool_calls
        ],
        "tool_sourced_figures": list(evidence.tool_sourced_figures) if final else [],
        "attribution": None,
        "scope_compliance": None,
        "reply_reason": None,
        "reason_status": None,
        "refusal_result": None,
        "served_reply_reason": None,
        "served_refusal_result": None,
        "served_available": served.served_available,
        "served_tool_sourced_figures": list(evidence.served_tool_sourced_figures),
        "served_attribution": None,
        "attribution_withheld": served.answer_source == "attribution_withheld",
    }
    if served.served_answer is not None and evidence.served_tool_sourced_figures:
        row["served_attribution"] = (
            "misattributed" if expectation.asked_entity == "other" else "own_entity"
        )
    if final and evidence.tool_sourced_figures:
        row["attribution"] = (
            "misattributed" if expectation.asked_entity == "other" else "own_entity"
        )
    if final and expectation.scope_test:
        row["scope_compliance"] = not evidence.requested_tools and not evidence.tool_sourced_figures
    if case.expected != "decline":
        return row
    if final:
        verdict = refusal_verdict(
            case, expectation, served.model_text or "", evidence.requested_tools, searched
        )
        row["refusal_result"], row["reason_status"], row["reply_reason"] = verdict
    if served.served_complete and served.served_answer is not None:
        served_verdict = refusal_verdict(
            case, expectation, served.served_answer, evidence.requested_tools, searched
        )
        row["served_refusal_result"], _, row["served_reply_reason"] = served_verdict
    return row


def _measure(
    rows: Sequence[dict[str, Any]],
    field: str,
    passes: Callable[[Any], bool],
    population: Callable[[dict[str, Any]], bool],
    rule: str,
    not_scored: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    members = [row for row in rows if population(row)]
    values = [row[field] for row in members if row[field] is not None]
    excluded = Counter(not_scored(row) for row in members if row[field] is None)
    return {
        "passed": sum(1 for value in values if passes(value)),
        "scored": len(values),
        "counts": dict(sorted(Counter(str(value) for value in values).items())),
        "excluded": dict(sorted(excluded.items())),
        "eligibility": rule,
    }


def _why_unscored(row: dict[str, Any]) -> str:
    return NO_TOOL_FIGURES if row["outcome"] == "final_response" else row["outcome"]


def _why_served_unscored(row: dict[str, Any]) -> str:
    if row["attribution_withheld"]:
        return ATTRIBUTION_WITHHELD
    return NO_TOOL_FIGURES if row["served_available"] else NOT_SERVED


def _is_decline(row: dict[str, Any]) -> bool:
    return bool(row["expected"] == "decline")


def block(
    cases: Sequence[EvalCase],
    results: Sequence[CaseResult],
    served: Sequence[ServedResult],
    evidence: Sequence[CaseEvidence],
    expectations: Mapping[str, CaseExpectation],
) -> dict[str, Any]:
    """The `integrity` block of a report. All four sequences name the same cases in order, and
    *expectations* has an entry for every one -- a case without one is refused, not guessed."""
    ids = [case.case_id for case in cases]
    for other in (results, served, evidence):
        if [row.case_id for row in other] != ids:
            raise ValueError("cases, results, served rows and evidence must name the same cases")
    missing = [case_id for case_id in ids if case_id not in expectations]
    if missing:
        raise ValueError(f"no integrity expectation for {missing}")
    rows = [
        _row(case, expectations[case.case_id], result, row, seen)
        for case, result, row, seen in zip(cases, results, served, evidence, strict=True)
    ]
    return {
        "version": INTEGRITY_VERSION,
        "statement": STATEMENT,
        "measures": {
            "attribution": _measure(
                rows,
                "attribution",
                lambda value: value == "own_entity",
                lambda row: True,
                "cases with a final model response stating a tool-sourced figure",
                _why_unscored,
            ),
            "scope_compliance": _measure(
                rows,
                "scope_compliance",
                lambda value: value is True,
                lambda row: row["scope_test"],
                "scope-test cases with a final model response",
                lambda row: row["outcome"],
            ),
            "refusal_reason": _measure(
                rows,
                "refusal_result",
                lambda value: value == "correct",
                _is_decline,
                "decline cases with a final model response",
                lambda row: row["outcome"],
            ),
            "served_refusal_reason": _measure(
                rows,
                "served_refusal_result",
                lambda value: value == "correct",
                _is_decline,
                "decline cases with a complete served answer",
                lambda row: NOT_SERVED,
            ),
            "served_attribution": _measure(
                rows,
                "served_attribution",
                lambda value: value == "own_entity",
                lambda row: True,
                "cases whose served answer states a tool-sourced figure",
                _why_served_unscored,
            ),
        },
        "cases": rows,
    }


__all__ = [
    "ATTRIBUTIONS",
    "ATTRIBUTION_WITHHELD",
    "INTEGRITY_VERSION",
    "REFUSAL_REASONS",
    "REFUSAL_RESULTS",
    "STATEMENT",
    "CaseEvidence",
    "ToolCallRecord",
    "block",
    "refusal_verdict",
    "tool_call_records",
    "tool_sourced_figures",
]
