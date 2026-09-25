"""Runs `copilot_knowledge_eval_v1` through the real copilot stack and scores it (Stage 7.10).

The same arrangement as `harness.py`: everything real except the data services (now four, with
`FixtureKnowledge` beside the three Stage 7.8 fixtures), the membership lookup and the two write
sinks. The model is a replay of hand-written reference exchanges in CI, or a live provider in an
opt-in capture.

## The report

Named by harness version, question set and checksum, prompt and checksum, model and date. Each
measure is reported as its own `passed` / `scored` pair and **never combined** -- there is no
aggregate, and the `statement` says what the result does not mean. `citation_ownership` and
`citation_status` are guarantees of the pipeline, so they are expected to pass whatever the model
does; the other measures describe what the model did.

`python -m tests.evaluation.knowledge_harness --write-reference` regenerates
`knowledge_reference_report.json` from the reference replays.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from app.copilot.contracts import ToolServices
from app.copilot.registry import ToolRegistry, build_default_registry
from app.llm.base import Budget, ChatModel, ToolCall
from app.llm.errors import LlmError
from app.models.enums import HotelRole
from app.services.copilot import PROMPT, CopilotService
from app.services.tool_invocation import ToolInvocationService
from tests.evaluation import knowledge_scorers as scorers
from tests.evaluation.fixture_documents import FixtureKnowledge
from tests.evaluation.fixture_hotel import (
    EVAL_HOTEL,
    FixtureAccuracy,
    FixtureAnalytics,
    FixtureForecasts,
)
from tests.evaluation.harness import EvalAudit, EvalLog, EvalScope, EvalSession
from tests.evaluation.knowledge_questions import (
    COPILOT_KNOWLEDGE_EVAL_V1,
    KnowledgeCase,
    KnowledgeQuestionSet,
)
from tests.evaluation.replay import (
    REFERENCE_PROVIDER,
    RecordingModel,
    ReplayModel,
    Replays,
    load_replays,
)

HARNESS_VERSION = "copilot_knowledge_eval_harness_v1"
HERE = Path(__file__).resolve().parent
REFERENCE_REPLAYS = HERE / "fixtures" / "knowledge_reference_replays.json"
REFERENCE_REPORT = HERE / "knowledge_reference_report.json"

#: The measures, in report order. Each is its own passed/scored pair.
MEASURES = (
    "tool_selection",
    "citation_validity",
    "citation_ownership",
    "citation_status",
    "cited_figure_grounding",
    "not_found",
    "expected_sources",
    "expected_figures",
    "completed",
    "scorer_agreement",
)

REFERENCE_STATEMENT = (
    "Reference exchanges written by hand to exercise the copilot's document path and these "
    "scorers, over a fixed stand-in for document search. No language model was evaluated and "
    "retrieval quality was not measured here (see knowledge_retrieval_v1). A regression means "
    "the code changed what it does with a fixed exchange."
)


def live_statement(provider: str, model: str, recorded_on: str, size: int) -> str:
    return (
        f"One run of {provider}/{model}, recorded on {recorded_on}, against {size} questions of "
        f"{COPILOT_KNOWLEDGE_EVAL_V1.identity} under {PROMPT.identity}, with fixed tool data and a "
        "fixed stand-in for document search. It describes this set, this prompt and this model on "
        "that date, and nothing more: no aggregate accuracy is claimed, the set is too small for a "
        "rate to generalise, and retrieval quality is measured elsewhere."
    )


@dataclass(frozen=True, slots=True)
class KnowledgeCaseResult:
    case_id: str
    category: str
    expected: str
    stop_reason: str | None
    complete: bool
    error_code: str | None
    tools_offered: list[str]
    tools_called: list[str]
    document_evidence: str | None
    served_answer: str
    served_citations: list[dict[str, Any]]
    tool_selection: bool | None
    citation_validity: bool | None
    citation_ownership: bool | None
    citation_status: bool | None
    cited_figure_grounding: bool | None
    not_found: bool | None
    expected_sources: bool | None
    expected_figures: bool | None
    scorer_agreement: bool | None


def run_case(
    case: KnowledgeCase,
    model: ChatModel,
    *,
    registry: ToolRegistry,
    budget: Budget,
    provider_name: str,
    model_name: str,
) -> KnowledgeCaseResult:
    scope = EvalScope(HotelRole(case.role))
    session = EvalSession()
    services = ToolServices(
        analytics=cast(Any, FixtureAnalytics()),
        demand_prediction=cast(Any, FixtureForecasts()),
        forecast_performance=cast(Any, FixtureAccuracy()),
        knowledge=cast(Any, FixtureKnowledge()),
    )
    invocations = ToolInvocationService(
        cast(Any, session), registry, cast(Any, scope), cast(Any, EvalAudit()), services
    )
    recorder = RecordingModel(model)
    copilot = CopilotService(
        cast(Any, session),
        recorder,
        registry,
        invocations,
        cast(Any, EvalLog()),
        cast(Any, scope),
        budget=budget,
        provider_name=provider_name,
        model_name=model_name,
    )

    stop_reason: str | None = None
    complete = False
    error_code: str | None = None
    evidence: str | None = None
    served_answer = ""
    served: list[dict[str, Any]] = []
    try:
        response = copilot.ask(EVAL_HOTEL, case.question)
        stop_reason, complete = response.stop_reason, response.complete
        evidence, served_answer = response.document_evidence, response.answer
        served = [item.model_dump(mode="json") for item in response.citations]
    except LlmError as failure:
        error_code = failure.code
    answered = error_code is None

    calls: list[ToolCall] = [call for r in recorder.responses for call in r.tool_calls]
    final_text = recorder.responses[-1].text if recorder.responses else ""
    seen = scorers.labels_seen(recorder.requests)
    structured = [
        json.loads(m.content)
        for m in (recorder.requests[-1].messages if recorder.requests else ())
        if m.role == "tool" and not m.is_error and "untrusted_retrieved_content" not in m.content
    ]
    attempted = scorers.attempted_citations(final_text)

    # Scored when the model cited anything, or should have: an expected-cited answer that cites
    # nothing fails. Grounding of cited figures is scored only on citations that resolve.
    valid = answered and bool(attempted) and scorers.citation_validity(final_text, seen)
    validity: bool | None = valid if answered and (attempted or case.expected == "cited") else None
    agreement: bool | None = (
        valid == (evidence != "citation_rejected") if answered and attempted else None
    )
    grounding: bool | None = (
        scorers.cited_figure_grounding(
            final_text, seen, structured_outputs=structured, question=case.question
        )
        if valid
        else None
    )

    return KnowledgeCaseResult(
        case_id=case.case_id,
        category=case.category,
        expected=case.expected,
        stop_reason=stop_reason,
        complete=complete,
        error_code=error_code,
        tools_offered=[spec.name for spec in recorder.requests[0].tools]
        if recorder.requests
        else [],
        tools_called=[call.name for call in calls],
        document_evidence=evidence,
        served_answer=served_answer,
        served_citations=served,
        tool_selection=answered and scorers.tool_selection(case, calls),
        citation_validity=validity,
        citation_ownership=scorers.citation_ownership(served) if served else None,
        citation_status=scorers.citation_status(served) if served else None,
        cited_figure_grounding=grounding,
        not_found=answered and scorers.not_found_served(served_answer, served)
        if case.expected == "not_found"
        else None,
        expected_sources=answered and complete and scorers.expected_sources(case, served)
        if case.expected == "cited"
        else None,
        expected_figures=answered and complete and scorers.expected_figures(case, served_answer)
        if case.figures
        else None,
        scorer_agreement=agreement,
    )


def _measures(results: Iterable[KnowledgeCaseResult]) -> dict[str, dict[str, int]]:
    rows = list(results)
    measures: dict[str, dict[str, int]] = {}
    for measure in MEASURES:
        if measure == "completed":
            values = [row.complete for row in rows]
        else:
            values = [
                cast(bool, getattr(row, measure))
                for row in rows
                if getattr(row, measure) is not None
            ]
        measures[measure] = {"passed": sum(1 for v in values if v), "scored": len(values)}
    return measures


def run(
    question_set: KnowledgeQuestionSet,
    model_for: Callable[[KnowledgeCase], ChatModel],
    *,
    mode: str,
    provider: str,
    model: str,
    recorded_on: str,
    budget: Budget | None = None,
) -> dict[str, Any]:
    registry = build_default_registry()
    budget = budget or Budget(timeout_seconds=30.0, max_output_tokens=1024)
    results = [
        run_case(
            case,
            model_for(case),
            registry=registry,
            budget=budget,
            provider_name=provider,
            model_name=model,
        )
        for case in question_set.cases
    ]
    statement = (
        REFERENCE_STATEMENT
        if provider == REFERENCE_PROVIDER
        else live_statement(provider, model, recorded_on, len(question_set.cases))
    )
    return {
        "harness": HARNESS_VERSION,
        "mode": mode,
        "question_set": {
            "identity": question_set.identity,
            "checksum": question_set.checksum,
            "size": len(question_set.cases),
        },
        "prompt": {"identity": PROMPT.identity, "checksum": PROMPT.checksum},
        "model": {"provider": provider, "model": model},
        "recorded_on": recorded_on,
        "statement": statement,
        "measures": _measures(results),
        "cases": [asdict(result) for result in results],
    }


def run_replays(
    replays: Replays, question_set: KnowledgeQuestionSet = COPILOT_KNOWLEDGE_EVAL_V1
) -> dict[str, Any]:
    return run(
        question_set,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
    )


def reference_report() -> dict[str, Any]:
    return run_replays(load_replays(REFERENCE_REPLAYS, COPILOT_KNOWLEDGE_EVAL_V1))


def canonical(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:  # pragma: no cover - a maintenance entry point, exercised by review
    parser = argparse.ArgumentParser(description="Regenerate the pinned knowledge report.")
    parser.add_argument("--write-reference", action="store_true")
    options = parser.parse_args()
    report = canonical(reference_report())
    if options.write_reference:
        REFERENCE_REPORT.write_text(report, encoding="utf-8", newline="\n")
        print(f"wrote tests/evaluation/{REFERENCE_REPORT.name}")
    else:
        print(report)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "HARNESS_VERSION",
    "MEASURES",
    "REFERENCE_REPLAYS",
    "REFERENCE_REPORT",
    "KnowledgeCaseResult",
    "canonical",
    "reference_report",
    "run",
    "run_case",
    "run_replays",
]
