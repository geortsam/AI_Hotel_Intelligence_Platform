"""`copilot_evaluation_policy_v1` (R2.8R) names only evidence the reports actually record.

The policy is prose in `docs/copilot-evaluation.md`; no code enforces it. These tests do not pin
that prose. They check its vocabulary against the code -- every answer source, verdict, outcome,
reason code and report field it relies on exists -- so a renamed field or a new contained-event
source fails here instead of leaving the policy pointing at nothing.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from pathlib import Path

from app.llm.errors import INVALID_RESPONSE_REASONS
from tests.evaluation import diagnostic_annotations, expectations_v3_v2, harness, integrity
from tests.evaluation import model_level as model_level_block
from tests.evaluation.knowledge_harness import KnowledgeCaseResult

DOC = Path(__file__).resolve().parents[2] / "docs" / "copilot-evaluation.md"
HEADING = "### Copilot evaluation policy v1 (`copilot_evaluation_policy_v1`, R2.8R, 2026-10-30)"


def policy() -> str:
    text = DOC.read_text(encoding="utf-8")
    assert text.count(HEADING) == 1
    return text.split(HEADING, 1)[1].split("\n---", 1)[0]


def subsection(letter: str) -> str:
    return re.split(rf"\n#### {letter}\. ", policy(), maxsplit=1)[1].split("\n#### ", 1)[0]


def ticked(text: str) -> set[str]:
    """Every code span in *text*, as written."""
    return set(re.findall(r"`([^`]+)`", text))


def fields_of(text: str, prefix: str) -> set[str]:
    """The field names *text* reads through *prefix* (`cases[].`, `integrity.cases[].`, ...)."""
    return set(re.findall(rf"(?<![\w.]){re.escape(prefix)}(\w+)", text))


# --- every name it relies on exists ---------------------------------------------------------------


def test_every_case_and_served_field_it_names_is_a_recorded_field() -> None:
    case_fields = {f.name for f in dataclasses.fields(harness.CaseResult)}
    served_fields = {f.name for f in dataclasses.fields(harness.ServedResult)}
    named = fields_of(policy(), "cases[].")
    assert named >= {
        "grounded",
        "scorer_agreement",
        "error_code",
        "stop_reason",
        "tools_called",
        "expected_figures_present",
        "missing_figures",
        "ungrounded_figures",
    }
    assert named <= case_fields, named - case_fields  # no case field it names is invented
    served = fields_of(policy(), "served.cases[].")
    assert served == {"answer_source"} and served <= served_fields


def test_every_integrity_and_block_field_it_names_is_written_by_the_code() -> None:
    integrity_source = inspect.getsource(integrity)
    for field in (
        "served_attribution",
        "attribution_withheld",
        "refusal_result",
        "reason_status",
        "reply_reason",
    ):
        assert f'"{field}"' in integrity_source, field
        assert field in fields_of(policy(), "integrity.cases[]."), field
    assert "fixed_sentence_with_extra_text" in inspect.getsource(integrity.v3_pilot_analysis)
    citations = {f.name for f in dataclasses.fields(KnowledgeCaseResult)}
    assert {"citation_ownership", "citation_status"} <= citations


def test_the_outcomes_verdicts_and_reasons_it_names_exist() -> None:
    named = ticked(policy())
    assert {"unavailable", "tool_stopped"} <= set(model_level_block.OUTCOMES)
    assert "refused_before_model" in expectations_v3_v2.GUARD_VERDICTS
    assert "length_with_tool_calls" in INVALID_RESPONSE_REASONS
    assert diagnostic_annotations.CALL_CAP_EXCEEDED in named
    assert {"run_health.status", "run_health.per_request[].diagnostics"} & named


# --- the contained-event table covers exactly the code's contained sources -----------------------


def test_every_contained_answer_source_in_the_code_is_in_the_policy_and_no_other() -> None:
    """A new server branch that substitutes or withholds must be classified here, not ignored:
    every `answer_source` but the model's own text, the pre-model guard and "nothing served"."""
    contained = set(harness.ANSWER_SOURCES) - {"model", "scope_guard", "unavailable"}
    table = {
        cell.strip("` ")
        for line in subsection("C").splitlines()
        if line.startswith("| `")
        for cell in [line.split("|")[1]]
        if cell.strip("` ") != "answer_source"  # the header row
    }
    assert table == contained


# --- the user's decision is stated, and the R2.8F rule is cross-referenced, not rewritten ---------


def test_policy_a_is_stated_and_adds_no_stop_condition() -> None:
    text = policy()
    assert "**answer-quality failure**" in text
    assert "`inject_estimate` is not a stop condition" in subsection("A")
    assert "`LLM_INVALID_RESPONSE`" in subsection("D")
    assert "**No code enforces it.**" in text


def test_the_r2_8f_stop_rule_it_refers_to_is_still_in_place() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert "**Stop -- no comparison is made -- if either run** exited non-zero" in doc
    assert doc.index("**Stop -- no comparison is made") < doc.index(HEADING)
