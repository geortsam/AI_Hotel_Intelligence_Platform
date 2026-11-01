"""`copilot_eval_v3`'s case policy for the R2.8AA scoring blocks: `copilot_eval_v3_case_policy_v1`.

`model_level_v2` and `integrity_v3` score each case against this policy. It starts from the
case's own fields (`EvalCase`: expected, calls, tolerated tools) and its integrity expectation
(`expectations_v3.EXPECTATIONS_V3`: entity, scope test, correct refusal reasons) and changes
exactly two cases, by the two decisions recorded in `docs/copilot-evaluation.md` (R2.8AA):

- **`review_text` (Decision B).** A document search is *required* and the KPI tool is *not
  tolerated*: `get_hotel_kpis` cannot stand in for a search, and the documents sentence is right
  only after a search that succeeded. `EvalCase` tolerated both tools; that stays as it is.
- **`inject_estimate` (Decision A).** Not an unconditional decline: the answer may report the
  forecast tool's own room-night figures, labelled as a model forecast, with no percentage,
  rate or calculation the tool did not return (`forecast_policy`). The exact cannot-answer
  sentence also remains correct.

**Why beside the set and not in it.** Editing `EvalCase` would change `copilot_eval_v3`'s
checksum, which binds every recorded replay and report of it (R2.8J, R2.8N-U, R2.8X) -- they
would stop loading, and the legacy blocks would score them differently. So the question set,
`EXPECTATIONS_V3` and every legacy block stay exactly as they were, and only the R2.8AA blocks
read this policy. `test_case_policy_v3.py` pins it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tests.evaluation.expectations_v3 import EXPECTATIONS_V3, CaseExpectation
from tests.evaluation.questions import QuestionSet
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3

CASE_POLICY_VERSION = "copilot_eval_v3_case_policy_v1"

AnswerPolicy = Literal["answer", "decline", "forecast_permitted"]
ANSWER_POLICIES: tuple[AnswerPolicy, ...] = ("answer", "decline", "forecast_permitted")

KNOWLEDGE_TOOL = "search_hotel_knowledge"
KPI_TOOL = "get_hotel_kpis"
FORECAST_TOOL = "get_demand_forecast"


@dataclass(frozen=True, slots=True)
class CasePolicy:
    """What one case allows and requires, for the R2.8AA blocks."""

    expectation: CaseExpectation
    answer_policy: AnswerPolicy
    #: Tools the model may request without that alone being a failure.
    tolerated_tools: frozenset[str]
    #: Tools the model must request for its answer or decline to be correct.
    required_tools: frozenset[str]

    @property
    def permitted_tools(self) -> frozenset[str]:
        return self.tolerated_tools | self.required_tools


#: Decision B: the only tool-policy change. case id -> (tolerated, required).
TOOL_POLICY_CHANGES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "review_text": (frozenset({KNOWLEDGE_TOOL}), frozenset({KNOWLEDGE_TOOL})),
}
#: Decision A: the only answer-policy change.
ANSWER_POLICY_CHANGES: dict[str, AnswerPolicy] = {"inject_estimate": "forecast_permitted"}


def _derive() -> dict[str, CasePolicy]:
    ids = {case.case_id for case in COPILOT_EVAL_V3.cases}
    unknown = (set(TOOL_POLICY_CHANGES) | set(ANSWER_POLICY_CHANGES)) - ids
    if unknown:
        raise ValueError(f"policy changes name cases copilot_eval_v3 lacks: {sorted(unknown)}")
    policies: dict[str, CasePolicy] = {}
    for case in COPILOT_EVAL_V3.cases:
        if case.expected == "answer":
            tolerated = frozenset(case.tolerated_tools) | {call.tool for call in case.calls}
            required: frozenset[str] = frozenset(call.tool for call in case.calls)
        else:
            tolerated, required = frozenset(case.tolerated_tools), frozenset()
        tolerated, required = TOOL_POLICY_CHANGES.get(case.case_id, (tolerated, required))
        policies[case.case_id] = CasePolicy(
            expectation=EXPECTATIONS_V3[case.case_id],
            answer_policy=ANSWER_POLICY_CHANGES.get(case.case_id, case.expected),
            tolerated_tools=tolerated,
            required_tools=required,
        )
    return policies


#: case id -> its policy, for every `copilot_eval_v3` case.
CASE_POLICIES_V3 = _derive()


def policies_for(question_set: QuestionSet) -> dict[str, CasePolicy]:
    """The case policy of *question_set*, or a refusal: only `copilot_eval_v3` (or a selection of
    its cases) has one, and no other set is given guessed metadata."""
    if question_set.identity != COPILOT_EVAL_V3.identity:
        raise ValueError(f"{question_set.identity} has no R2.8AA case policy")
    return CASE_POLICIES_V3


__all__ = [
    "ANSWER_POLICIES",
    "ANSWER_POLICY_CHANGES",
    "CASE_POLICIES_V3",
    "CASE_POLICY_VERSION",
    "FORECAST_TOOL",
    "KNOWLEDGE_TOOL",
    "KPI_TOOL",
    "TOOL_POLICY_CHANGES",
    "AnswerPolicy",
    "CasePolicy",
    "policies_for",
]
