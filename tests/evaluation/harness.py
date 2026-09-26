"""Runs `copilot_eval_v1` through the real copilot stack and scores it (Stage 7.8).

    question ──► CopilotService (real) ──► ToolLoop (real) ──► ToolInvocationService (real)
                      │                                            │  registry, argument and
                      │                                            │  output validation, role
                      │                                            ▼  check: all real
                  model: ReplayModel (CI)              FixtureAnalytics / FixtureForecasts /
                         or the live provider          FixtureAccuracy -- the ONLY stand-ins
                         (opt-in, never CI)

What is replaced, and only this: the data services (fixed data, `fixture_hotel.py`, and since
Stage 7.10 `fixture_documents.py`), the membership lookup (a role per case), and the two write
sinks (the audit trail and the invocation log, which record into memory). Nothing touches a
database or a network in replay mode.

Stage 7.10 moved the copilot to `copilot_answer@v2` and a six-tool catalogue. This set's questions
and what passing it means are unchanged; its report now names v2 and lists the sixth tool among
those offered, which is exactly the change it should show. Document questions have their own set,
`copilot_knowledge_eval_v1` (`knowledge_harness.py`).

## The report, and what it may say (architecture §9.3)

One JSON document naming the harness version, the question set and its checksum, the prompt and
its checksum, the model, and the date the exchanges were recorded. Per case: what happened and
each score. Per measure: `passed` and `scored` counts, reported separately and **never combined
into an aggregate**. A `statement` says, in words, what the result does not mean.

`python -m tests.evaluation.harness --write-reference` regenerates `reference_report.json` from
the reference replays. A change to that file is a change to what CI considers correct, and is
reviewed as one.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from app.copilot.contracts import ToolServices
from app.copilot.registry import ToolRegistry, build_default_registry
from app.core.errors import ForbiddenError, NotFoundError
from app.llm.base import Budget, ChatModel, ChatRequest, ToolCall
from app.llm.errors import LlmError
from app.models.enums import SAFE_AUDIT_DETAIL_KEYS, HotelRole
from app.services.copilot import PROMPT, CopilotService
from app.services.tool_invocation import ToolInvocationService
from tests.evaluation import scorers
from tests.evaluation.fixture_documents import FixtureKnowledge
from tests.evaluation.fixture_hotel import (
    EVAL_HOTEL,
    FixtureAccuracy,
    FixtureAnalytics,
    FixtureForecasts,
    FixtureInsight,
)
from tests.evaluation.questions import COPILOT_EVAL_V1, EvalCase, QuestionSet
from tests.evaluation.replay import (
    REFERENCE_PROVIDER,
    RecordingModel,
    ReplayModel,
    Replays,
    load_replays,
)

#: v2 since Stage 7.10: the copilot under evaluation renders `copilot_answer@v2` and offers six
#: tools, so a v1 report and a v2 report are not the same measurement of the same thing.
HARNESS_VERSION = "copilot_eval_harness_v2"
HERE = Path(__file__).resolve().parent
REFERENCE_REPLAYS = HERE / "fixtures" / "reference_replays.json"
REFERENCE_REPORT = HERE / "reference_report.json"

#: The measures, in report order. Each is reported as its own passed/scored pair.
MEASURES = (
    "tool_selection",
    "grounding",
    "expected_figures",
    "refusal",
    "completed",
    "scorer_agreement",
)

_ROLE_ORDER = [HotelRole.VIEWER, HotelRole.STAFF, HotelRole.MANAGER, HotelRole.OWNER]

REFERENCE_STATEMENT = (
    "Reference exchanges written by hand to exercise the copilot pipeline and these scorers. "
    "No language model was evaluated, and nothing in this report describes any model's "
    "behaviour. A regression here means the code changed what it does with a fixed exchange."
)


def live_statement(provider: str, model: str, recorded_on: str, size: int) -> str:
    return (
        f"One run of {provider}/{model}, recorded on {recorded_on}, against {size} questions of "
        f"{COPILOT_EVAL_V1.identity} under {PROMPT.identity}, with fixed tool data. "
        "It describes this question set, this prompt and this model on that date, and nothing "
        "more: no aggregate accuracy is claimed, the set is too small for a rate to generalise, "
        "and it says nothing about the demand model's accuracy or about product usefulness."
    )


# --- the stand-ins: a role, and two sinks that record into memory ---------------------------------


class EvalHotel:
    id = 1


class EvalScope:
    """The membership answer for one case: the caller holds *role* at the evaluated hotel."""

    def __init__(self, role: HotelRole) -> None:
        self._role = role

    def require_hotel(self, hotel_public_id: uuid.UUID) -> EvalHotel:
        if hotel_public_id != EVAL_HOTEL:
            raise NotFoundError()
        return EvalHotel()

    def require_hotel_with_role(self, hotel_public_id: uuid.UUID, required: HotelRole) -> EvalHotel:
        hotel = self.require_hotel(hotel_public_id)
        if _ROLE_ORDER.index(self._role) < _ROLE_ORDER.index(required):
            raise ForbiddenError()
        return hotel


class EvalAudit:
    """Accepts `tool.invoked` events, holding them to the same approved-keys rule as the trail."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, *args: Any, details: dict[str, object] | None = None, **kwargs: Any) -> None:
        unapproved = set(details or {}) - SAFE_AUDIT_DETAIL_KEYS
        if unapproved:
            raise AssertionError(f"audit details carry unapproved keys {sorted(unapproved)}")
        self.events.append(dict(details or {}))


class EvalRow:
    public_id = uuid.UUID("7e57e7a1-0000-4000-8000-00000000f00d")


class EvalLog:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, **values: Any) -> EvalRow:
        self.rows.append(values)
        return EvalRow()


class EvalSession:
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


# --- one case ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    category: str
    role: str
    expected: str
    stop_reason: str | None
    complete: bool
    error_code: str | None
    #: The catalogue the model was shown -- so a change in what a role is offered moves the
    #: pinned report even when no recorded exchange would have called the new tool.
    tools_offered: list[str]
    tools_called: list[str]
    tool_selection: bool | None
    grounded: bool | None
    ungrounded_figures: list[str]
    expected_figures_present: bool | None
    missing_figures: list[str]
    refusal_correct: bool | None
    scorer_agreement: bool | None
    input_tokens: int
    output_tokens: int
    latency_ms: int | None


def _tool_outputs_seen(request: ChatRequest | None) -> list[Any]:
    """The successful tool results the model had in front of it when it wrote its last turn."""
    if request is None:
        return []
    outputs: list[Any] = []
    for message in request.messages:
        if message.role == "tool" and not message.is_error:
            outputs.append(json.loads(message.content))
    return outputs


def run_case(
    case: EvalCase,
    model: ChatModel,
    *,
    registry: ToolRegistry,
    budget: Budget,
    provider_name: str,
    model_name: str,
    measure_latency: bool,
) -> CaseResult:
    """Ask one question through the real stack and score what came back."""
    scope = EvalScope(HotelRole(case.role))
    session = EvalSession()
    services = ToolServices(
        analytics=cast(Any, FixtureAnalytics()),
        demand_prediction=cast(Any, FixtureForecasts()),
        forecast_performance=cast(Any, FixtureAccuracy()),
        insight=cast(Any, FixtureInsight()),
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

    started = time.perf_counter()
    error_code: str | None = None
    stop_reason: str | None = None
    complete = False
    try:
        response = copilot.ask(EVAL_HOTEL, case.question)
        stop_reason, complete = response.stop_reason, response.complete
    except LlmError as failure:
        error_code = failure.code
    latency_ms = round((time.perf_counter() - started) * 1000) if measure_latency else None

    calls: list[ToolCall] = [call for r in recorder.responses for call in r.tool_calls]
    final_text = recorder.responses[-1].text if recorder.responses else ""
    last_request = recorder.requests[-1] if recorder.requests else None
    answered = error_code is None

    grounded: bool | None = None
    offending: tuple[str, ...] = ()
    if answered:
        offending = scorers.ungrounded(
            final_text, tool_outputs=_tool_outputs_seen(last_request), question=case.question
        )
        grounded = not offending

    agreement: bool | None = None
    if grounded is not None and stop_reason in {"completed", "ungrounded_figures"}:
        agreement = grounded == (stop_reason == "completed")

    selection: bool | None = None
    missing: tuple[str, ...] = ()
    figures_present: bool | None = None
    refusal: bool | None = None
    if case.expected == "answer":
        selection = answered and scorers.tool_selection(case, calls)
        missing = scorers.missing_figures(case, final_text) if answered else case.figures
        figures_present = answered and complete and not missing if case.figures else None
    else:
        refusal = answered and scorers.refusal_correct(
            case, complete=complete, answer=final_text, calls=calls
        )

    return CaseResult(
        case_id=case.case_id,
        category=case.category,
        role=case.role,
        expected=case.expected,
        stop_reason=stop_reason,
        complete=complete,
        error_code=error_code,
        tools_offered=[spec.name for spec in recorder.requests[0].tools]
        if recorder.requests
        else [],
        tools_called=[call.name for call in calls],
        tool_selection=selection,
        grounded=grounded,
        ungrounded_figures=list(offending),
        expected_figures_present=figures_present,
        missing_figures=list(missing),
        refusal_correct=refusal,
        scorer_agreement=agreement,
        input_tokens=sum(r.usage.input_tokens for r in recorder.responses),
        output_tokens=sum(r.usage.output_tokens for r in recorder.responses),
        latency_ms=latency_ms,
    )


# --- a whole run ---------------------------------------------------------------------------------


def _measures(results: Iterable[CaseResult]) -> dict[str, dict[str, int]]:
    rows = list(results)
    fields = {
        "tool_selection": "tool_selection",
        "grounding": "grounded",
        "expected_figures": "expected_figures_present",
        "refusal": "refusal_correct",
        "scorer_agreement": "scorer_agreement",
    }
    measures: dict[str, dict[str, int]] = {}
    for measure in MEASURES:
        if measure == "completed":
            values: list[bool] = [row.complete for row in rows]
        else:
            values = [
                cast(bool, getattr(row, fields[measure]))
                for row in rows
                if getattr(row, fields[measure]) is not None
            ]
        measures[measure] = {"passed": sum(1 for v in values if v), "scored": len(values)}
    return measures


def run(
    question_set: QuestionSet,
    model_for: Callable[[EvalCase], ChatModel],
    *,
    mode: str,
    provider: str,
    model: str,
    recorded_on: str,
    budget: Budget | None = None,
    on_case: Callable[[EvalCase, RecordingModel | None], None] | None = None,
) -> dict[str, Any]:
    """Run every case and return the report. Deterministic for a replay."""
    registry = build_default_registry()
    budget = budget or Budget(timeout_seconds=30.0, max_output_tokens=1024)
    results: list[CaseResult] = []
    for case in question_set.cases:
        case_model = model_for(case)
        results.append(
            run_case(
                case,
                case_model,
                registry=registry,
                budget=budget,
                provider_name=provider,
                model_name=model,
                measure_latency=mode == "live",
            )
        )
        if on_case is not None:
            on_case(case, case_model if isinstance(case_model, RecordingModel) else None)

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


def run_replays(replays: Replays, question_set: QuestionSet = COPILOT_EVAL_V1) -> dict[str, Any]:
    """Replay a recorded run. The report names the recording's model and date, not today's."""
    return run(
        question_set,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
    )


def reference_report() -> dict[str, Any]:
    return run_replays(load_replays(REFERENCE_REPLAYS, COPILOT_EVAL_V1))


def canonical(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:  # pragma: no cover - a maintenance entry point, exercised by review
    parser = argparse.ArgumentParser(description="Regenerate the pinned reference report.")
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
    "CaseResult",
    "canonical",
    "reference_report",
    "run",
    "run_case",
    "run_replays",
]
