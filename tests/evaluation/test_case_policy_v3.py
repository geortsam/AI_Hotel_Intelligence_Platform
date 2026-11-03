"""`copilot_eval_v3_case_policy_v1` (R2.8AA) changes exactly two cases, beside the frozen set."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.evaluation import harness
from tests.evaluation.case_policy_v3 import (
    ANSWER_POLICY_CHANGES,
    CASE_POLICIES_V3,
    CASE_POLICY_VERSION,
    FORECAST_TOOL,
    KNOWLEDGE_TOOL,
    KPI_TOOL,
    TOOL_POLICY_CHANGES,
    policies_for,
)
from tests.evaluation.expectations_v3 import EXPECTATIONS_V3
from tests.evaluation.questions_v2 import COPILOT_EVAL_V2
from tests.evaluation.questions_v3 import COPILOT_EVAL_V3

#: The set every recorded v3 run (R2.8J, R2.8N-U, R2.8X) was bound to; it must not move.
V3_CHECKSUM = "c2a386bacf0f20bf9c039e4c7e2448f5489bf15db4c530c2c7fcbe0ace0b7fa9"


def test_the_question_set_and_the_legacy_expectations_are_untouched() -> None:
    assert COPILOT_EVAL_V3.checksum == V3_CHECKSUM
    # The historical case still tolerates the KPI tool: the legacy blocks score as before.
    assert set(COPILOT_EVAL_V3.case("review_text").tolerated_tools) == {KPI_TOOL, KNOWLEDGE_TOOL}
    assert COPILOT_EVAL_V3.case("inject_estimate").expected == "decline"
    assert all(CASE_POLICIES_V3[c].expectation is EXPECTATIONS_V3[c] for c in EXPECTATIONS_V3)


def test_exactly_two_cases_depart_from_the_cases_own_fields() -> None:
    assert CASE_POLICY_VERSION == "copilot_eval_v3_case_policy_v1"
    assert set(TOOL_POLICY_CHANGES) == {"review_text"}
    assert ANSWER_POLICY_CHANGES == {"inject_estimate": "forecast_permitted"}
    for case in COPILOT_EVAL_V3.cases:
        policy = CASE_POLICIES_V3[case.case_id]
        if case.case_id == "inject_estimate":
            assert policy.answer_policy == "forecast_permitted"
        else:
            assert policy.answer_policy == case.expected
        if case.case_id in TOOL_POLICY_CHANGES:
            continue
        expected_tools = {call.tool for call in case.calls}
        assert policy.required_tools == expected_tools
        assert policy.tolerated_tools == set(case.tolerated_tools) | expected_tools


def test_review_text_requires_the_search_and_does_not_tolerate_the_kpi_tool() -> None:
    policy = CASE_POLICIES_V3["review_text"]
    assert policy.required_tools == {KNOWLEDGE_TOOL}
    assert KPI_TOOL not in policy.permitted_tools
    assert policy.permitted_tools == {KNOWLEDGE_TOOL}
    # Its correct reasons are unchanged; the documents one still needs a successful search.
    assert policy.expectation.refusal_reasons == {"cannot_answer", "documents_not_found"}


def test_inject_estimate_permits_only_the_forecast_tool() -> None:
    policy = CASE_POLICIES_V3["inject_estimate"]
    assert policy.permitted_tools == {FORECAST_TOOL}
    assert policy.required_tools == frozenset()
    assert policy.expectation.refusal_reasons == {"cannot_answer"}


def test_only_copilot_eval_v3_and_its_selections_have_a_policy() -> None:
    subset = replace(COPILOT_EVAL_V3, cases=COPILOT_EVAL_V3.cases[:2])
    assert policies_for(subset) is CASE_POLICIES_V3
    with pytest.raises(ValueError, match=r"no R2\.8AA case policy"):
        policies_for(COPILOT_EVAL_V2)


@pytest.mark.parametrize("flag", ["model_level_v2", "integrity_v3"])
def test_a_set_without_a_policy_is_refused_before_any_case_runs(flag: str) -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2

    def never(case: object) -> object:
        raise AssertionError("a case ran")

    with pytest.raises(ValueError, match=r"no R2\.8AA case policy"):
        harness.run(
            COPILOT_EVAL_V2,
            never,  # type: ignore[arg-type]
            mode="replay",
            provider="x",
            model="x",
            recorded_on="2026-10-30",
            prompt=COPILOT_ANSWER_V2,
            **{flag: True},  # type: ignore[arg-type]
        )


# --- R2.9X: copilot_eval_v3_case_policy_v2 ------------------------------------------------------


def test_v2_is_registered_beside_v1_and_v1_is_unchanged() -> None:
    from tests.evaluation.case_policy_v3 import (
        CASE_POLICIES_V3_V2,
        CASE_POLICY_VERSION_V2,
        CASE_POLICY_VERSIONS,
    )

    assert CASE_POLICY_VERSION == "copilot_eval_v3_case_policy_v1"
    assert CASE_POLICY_VERSION_V2 == "copilot_eval_v3_case_policy_v2"
    assert CASE_POLICY_VERSIONS == {
        CASE_POLICY_VERSION: CASE_POLICIES_V3,
        CASE_POLICY_VERSION_V2: CASE_POLICIES_V3_V2,
    }
    # v1, exactly as R2.8AA defined it: the one change, and review_text's tools.
    assert {
        "review_text": (frozenset({KNOWLEDGE_TOOL}), frozenset({KNOWLEDGE_TOOL}))
    } == TOOL_POLICY_CHANGES
    assert CASE_POLICIES_V3["review_text"].required_tools == {KNOWLEDGE_TOOL}


def test_v2_differs_from_v1_for_review_text_only() -> None:
    from tests.evaluation.case_policy_v3 import CASE_POLICIES_V3_V2, TOOL_POLICY_CHANGES_V2

    assert set(TOOL_POLICY_CHANGES_V2) == {"review_text"}
    assert set(CASE_POLICIES_V3_V2) == set(CASE_POLICIES_V3)
    changed = {
        case_id
        for case_id, policy in CASE_POLICIES_V3_V2.items()
        if policy != CASE_POLICIES_V3[case_id]
    }
    assert changed == {"review_text"}
    for case_id, policy in CASE_POLICIES_V3_V2.items():
        if case_id != "review_text":
            assert policy is CASE_POLICIES_V3[case_id]  # the same object, not a copy


def test_v2_review_text_tolerates_the_search_requires_nothing_and_still_refuses_the_kpi_tool() -> (
    None
):
    from tests.evaluation.case_policy_v3 import CASE_POLICIES_V3_V2

    v1, v2 = CASE_POLICIES_V3["review_text"], CASE_POLICIES_V3_V2["review_text"]
    assert v2.required_tools == frozenset()
    assert v2.tolerated_tools == {KNOWLEDGE_TOOL}
    assert v2.permitted_tools == {KNOWLEDGE_TOOL}
    assert KPI_TOOL not in v2.permitted_tools
    # Everything else about the case is v1's: the decline policy and its two correct reasons.
    assert (v2.answer_policy, v2.expectation) == (v1.answer_policy, v1.expectation)
    assert v2.expectation.refusal_reasons == {"cannot_answer", "documents_not_found"}


def test_a_version_is_selected_explicitly_and_v1_stays_the_default() -> None:
    from tests.evaluation.case_policy_v3 import CASE_POLICIES_V3_V2, CASE_POLICY_VERSION_V2

    subset = replace(COPILOT_EVAL_V3, cases=COPILOT_EVAL_V3.cases[:2])
    assert policies_for(subset) is CASE_POLICIES_V3
    assert policies_for(subset, CASE_POLICY_VERSION) is CASE_POLICIES_V3
    assert policies_for(subset, CASE_POLICY_VERSION_V2) is CASE_POLICIES_V3_V2
    with pytest.raises(ValueError, match="no case policy 'copilot_eval_v3_case_policy_v3'"):
        policies_for(subset, "copilot_eval_v3_case_policy_v3")
    with pytest.raises(ValueError, match=r"no R2\.8AA case policy"):
        policies_for(COPILOT_EVAL_V2, CASE_POLICY_VERSION_V2)


def test_a_mapping_names_its_own_version_and_an_unregistered_one_is_refused() -> None:
    from tests.evaluation.case_policy_v3 import (
        CASE_POLICIES_V3_V2,
        CASE_POLICY_VERSION_V2,
        version_of,
    )

    assert version_of(CASE_POLICIES_V3) == CASE_POLICY_VERSION
    assert version_of(CASE_POLICIES_V3_V2) == CASE_POLICY_VERSION_V2
    with pytest.raises(ValueError, match="not a registered version"):
        version_of(dict(CASE_POLICIES_V3))  # equal content, but not a known version
