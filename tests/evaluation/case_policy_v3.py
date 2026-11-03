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

**`copilot_eval_v3_case_policy_v2` (R2.9X).** v1 with one change, to `review_text` only: the
document search is *tolerated*, no longer *required*, and the KPI tool stays not tolerated. No
copilot tool returns review text -- `get_hotel_kpis` gives review counts and an average rating,
and `search_hotel_knowledge` searches uploaded documents, none of which hold reviews (R2.9W) --
so declining with the cannot-answer sentence and no tool is an honest answer; v1 failed it. The
documents sentence is still right only after a search that succeeded (`integrity.refusal_verdict`
checks that, under either version). v1 is kept exactly as it was and stays the default
(`policies_for`), so every report it scored keeps meaning what it said; a caller selects v2 by its
version, and the blocks then name v2 in their `policies` (`version_of`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
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


#: R2.9X. The second version of this policy, and its one change. case id -> (tolerated, required).
CASE_POLICY_VERSION_V2 = "copilot_eval_v3_case_policy_v2"
TOOL_POLICY_CHANGES_V2: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "review_text": (frozenset({KNOWLEDGE_TOOL}), frozenset()),
}


def _derive_v2() -> dict[str, CasePolicy]:
    """v1's policies, with `TOOL_POLICY_CHANGES_V2` applied and nothing else."""
    unknown = set(TOOL_POLICY_CHANGES_V2) - set(CASE_POLICIES_V3)
    if unknown:
        raise ValueError(f"v2 changes name cases copilot_eval_v3 lacks: {sorted(unknown)}")
    return {
        case_id: (
            replace(
                policy,
                tolerated_tools=TOOL_POLICY_CHANGES_V2[case_id][0],
                required_tools=TOOL_POLICY_CHANGES_V2[case_id][1],
            )
            if case_id in TOOL_POLICY_CHANGES_V2
            else policy
        )
        for case_id, policy in CASE_POLICIES_V3.items()
    }


#: case id -> its v2 policy, for every `copilot_eval_v3` case.
CASE_POLICIES_V3_V2 = _derive_v2()

#: Every version of this policy, by its identifier.
CASE_POLICY_VERSIONS: dict[str, dict[str, CasePolicy]] = {
    CASE_POLICY_VERSION: CASE_POLICIES_V3,
    CASE_POLICY_VERSION_V2: CASE_POLICIES_V3_V2,
}


def policies_for(
    question_set: QuestionSet, version: str = CASE_POLICY_VERSION
) -> dict[str, CasePolicy]:
    """The case policy of *question_set*, or a refusal: only `copilot_eval_v3` (or a selection of
    its cases) has one, and no other set is given guessed metadata. *version* selects which
    (R2.9X); v1 by default, so a caller that names none scores exactly as before."""
    if question_set.identity != COPILOT_EVAL_V3.identity:
        raise ValueError(f"{question_set.identity} has no R2.8AA case policy")
    if version not in CASE_POLICY_VERSIONS:
        raise ValueError(f"no case policy {version!r}; known: {sorted(CASE_POLICY_VERSIONS)}")
    return CASE_POLICY_VERSIONS[version]


def version_of(policies: Mapping[str, CasePolicy]) -> str:
    """R2.9X. The version *policies* is -- read from which registered mapping it is, so a block can
    never name a version other than the one it scored under. A mapping that is not one of them
    is refused: its version is not known, and is not guessed."""
    for version, registered in CASE_POLICY_VERSIONS.items():
        if policies is registered:
            return version
    raise ValueError("these case policies are not a registered version")


__all__ = [
    "ANSWER_POLICIES",
    "ANSWER_POLICY_CHANGES",
    "CASE_POLICIES_V3",
    "CASE_POLICIES_V3_V2",
    "CASE_POLICY_VERSION",
    "CASE_POLICY_VERSIONS",
    "CASE_POLICY_VERSION_V2",
    "FORECAST_TOOL",
    "KNOWLEDGE_TOOL",
    "KPI_TOOL",
    "TOOL_POLICY_CHANGES",
    "TOOL_POLICY_CHANGES_V2",
    "AnswerPolicy",
    "CasePolicy",
    "policies_for",
    "version_of",
]
