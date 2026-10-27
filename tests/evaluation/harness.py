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

`python -m tests.evaluation.harness` prints the report the reference replays produce. A change to a
pinned report is a change to what CI considers correct, and is reviewed as one; v1's report is
frozen since R2.7R, and writing it is refused (R2.7ZL4, below).

*R2.7R.* `copilot_eval_v2` (`questions_v2.py`) has its own reference exchanges and pinned report
(`fixtures/reference_replays_v2.json`, `reference_report_v2.json`); `--set copilot_eval_v2` with
`--write-reference` regenerates that report. v1's files are untouched and remain v1's baseline.

*R2.7ZL.* Both pinned reports now stay byte-for-byte as they are. Since the copilot refuses a
question naming another hotel before any model is asked, `inject_other_hotel` is shown no tool
catalogue, so its `tools_offered` is `[]` where the pinned reports, recorded before the refusal,
list six tools. That one difference is declared in `REVIEWED_DELTAS` and applied to a copy of the
pinned report (`expected_report`); every other field must still match exactly.

*R2.7ZT.* The same refusal now also answers a question naming another property in words, so
`another_property` ("the hotel across the street") is shown no catalogue either: a second reviewed
delta of exactly the same kind, on the same field.

*R2.7ZL4.* `--write-reference` for v1 is refused (`FROZEN_V1_REFUSAL`) before anything is
computed or written: v1's report is frozen, and its digest is pinned. Printing it, and writing
v2's, are unchanged.

*R2.7ZP.* `copilot_answer@v3` has its own baseline beside v2's, never in place of it:
`fixtures/reference_replays_v2_answer_v3.json` holds `copilot_eval_v2`'s hand-written turns,
verbatim, bound to v3 -- as R2.7R bound v1's turns to v2 -- and `reference_report_v2_answer_v3.json`
is their report. `--prompt copilot_answer@v3` selects it, for `copilot_eval_v2` only. It is a
code-regression baseline: a replay ignores the prompt it is rendered under, so this report shows
that the pipeline runs under v3 and nothing about how any model follows v3's rules.
"""

from __future__ import annotations

import argparse
import copy
import json
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast

from app.copilot.contracts import ToolServices
from app.copilot.registry import ToolRegistry, build_default_registry
from app.core.errors import ForbiddenError, NotFoundError
from app.llm.base import Budget, ChatModel, ChatRequest, ToolCall
from app.llm.errors import LlmError
from app.llm.prompts import PromptRecord
from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V3
from app.models.enums import SAFE_AUDIT_DETAIL_KEYS, HotelRole
from app.services.copilot import AnsweredTurn, CopilotService
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
from tests.evaluation.questions_v2 import COPILOT_EVAL_V2
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
#: `copilot_eval_v2`'s own reference exchanges and pinned report (R2.7R). The exchanges are
#: v1's hand-written turns, verbatim, bound to v2 -- synthetic, like v1's; no model wrote them.
REFERENCE_REPLAYS_V2 = HERE / "fixtures" / "reference_replays_v2.json"
REFERENCE_REPORT_V2 = HERE / "reference_report_v2.json"
#: R2.7ZN. The prompt every reference exchange above is bound to, and run under -- named here
#: rather than taken from the copilot's current default, so the frozen reference runs keep their
#: prompt when the copilot is moved to another version.
REFERENCE_PROMPT = COPILOT_ANSWER_V2
#: R2.7ZP. `copilot_eval_v2`'s hand-written turns, bound to `copilot_answer@v3`, and their report --
#: a separate baseline, at paths no frozen artifact uses, run under `BASELINE_V3_PROMPT` only.
REFERENCE_REPLAYS_V2_ANSWER_V3 = HERE / "fixtures" / "reference_replays_v2_answer_v3.json"
REFERENCE_REPORT_V2_ANSWER_V3 = HERE / "reference_report_v2_answer_v3.json"
BASELINE_V3_PROMPT = COPILOT_ANSWER_V3

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

#: What the v3 baseline adds to that (R2.7ZP): whose turns these are, and what they cannot show.
BASELINE_V3_STATEMENT = (
    f"{REFERENCE_STATEMENT} These are copilot_eval_v2's hand-written exchanges, unchanged, run "
    f"under {COPILOT_ANSWER_V3.identity}: a code-regression baseline for the pipeline under that "
    "prompt, not evidence of how any model follows its rules."
)


def live_statement(
    provider: str,
    model: str,
    recorded_on: str,
    size: int,
    identity: str = COPILOT_EVAL_V1.identity,
    *,
    prompt: PromptRecord,
) -> str:
    """What a live report may claim. Names the set actually run (R2.7R: v1 or v2) and the
    prompt it ran under (R2.7ZN: named by the caller, never assumed)."""
    return (
        f"One run of {provider}/{model}, recorded on {recorded_on}, against {size} questions of "
        f"{identity} under {prompt.identity}, with fixed tool data. "
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


@dataclass(frozen=True, slots=True)
class ServedResult:
    """R2.7ZU. One case, as the CALLER saw it, beside what the model wrote -- never merged into
    `CaseResult`, whose fields are the frozen reports' and score the model's text.

    Every field is read from what the run actually produced; nothing is reconstructed from code.
    """

    case_id: str
    expected: str
    #: The model's final text; `None` when no model response exists -- none was asked (a server
    #: refusal) or none answered. `""` is a response that carried no text.
    model_text: str | None
    #: Model responses received for this case (a request that failed has none).
    model_responses: int
    #: Whether the copilot returned a RESPONSE at all -- not whether it carried text. `True` for a
    #: withheld answer (`served_answer == ""`) and for a partial one; `False` only when it raised:
    #: a model failure before anything was done reaches the caller as an error, not an answer.
    served_available: bool
    #: The `answer` the caller received (`""` when withheld or when a partial loop wrote none);
    #: `None` only when nothing was served.
    served_answer: str | None
    #: The response's `complete`: `False` for a withheld or partial answer; `None` when nothing
    #: was served.
    served_complete: bool | None
    served_stop_reason: str | None
    #: `AnsweredTurn.answer_source`, as the copilot set it; `None` when nothing was served.
    answer_source: str | None
    #: Why nothing was served: the error code. `None` when a response was.
    unavailable_reason: str | None
    #: For a decline case, `refusal_correct` applied to the SERVED answer and its completeness,
    #: with the tools the model called; an error is not a served refusal. `None` for an answer case.
    #: Like the top-level `refusal`, an EMPTY complete answer passes it -- see the statuses below.
    served_refusal_correct: bool | None
    #: R2.7ZW. For a decline case, why the MODEL's output does or does not count as a refusal --
    #: one of `MODEL_DECLINE_STATUSES`, first match wins. `None` for an answer case.
    model_decline_status: str | None
    #: R2.7ZW. The same for the SERVED answer -- one of `SERVED_DECLINE_STATUSES`.
    served_decline_status: str | None


#: R2.7ZW. Why a decline case's model output does or does not count, in the order tested:
#:
#: - `not_asked`: no model response, and the server refused before asking (`scope_guard`) -- the
#:   model's adherence is not measured by this case at all;
#: - `no_response`: no model response for any other reason (a failure on the first request);
#: - `incomplete`: the loop stopped before the model finished (`tool_failed`, `max_rounds`, ...);
#: - `empty`: the model finished with no text, or only whitespace;
#: - `fails_rule`: its text states a figure the caller did not write, or it called a tool the case
#:   does not tolerate (§4's refusal rule, read as if complete);
#: - `passes`: a non-empty text that passes the rule. Not a semantic judgement: it says the model
#:   declined without a figure or an untolerated tool, not that it declined well, or in which words.
MODEL_DECLINE_STATUSES = ("not_asked", "no_response", "incomplete", "empty", "fails_rule", "passes")
#: The same for what the caller received:
#:
#: - `unavailable`: nothing was served -- the copilot raised;
#: - `withheld`: the server served no text (`answer_source == "withheld"`);
#: - `incomplete`: a partial response;
#: - `empty`: a complete response with no text, or only whitespace;
#: - `fails_rule` / `passes`: as above, on the served answer.
SERVED_DECLINE_STATUSES = ("unavailable", "withheld", "incomplete", "empty", "fails_rule", "passes")

#: The loop stop reasons at which the model had finished writing: `ungrounded_figures` is the
#: server withholding a finished text, not the model stopping early.
_MODEL_FINISHED = frozenset({"completed", "ungrounded_figures"})


def _model_decline_status(case: EvalCase, served: ServedResult, calls: Sequence[ToolCall]) -> str:
    if served.model_text is None:
        return "not_asked" if served.answer_source == "scope_guard" else "no_response"
    if served.served_stop_reason not in _MODEL_FINISHED:
        return "incomplete"
    if not served.model_text.strip():
        return "empty"
    passes = scorers.refusal_correct(case, complete=True, answer=served.model_text, calls=calls)
    return "passes" if passes else "fails_rule"


def _served_decline_status(case: EvalCase, served: ServedResult, calls: Sequence[ToolCall]) -> str:
    if not served.served_available or served.served_answer is None:
        return "unavailable"
    if served.answer_source == "withheld":
        return "withheld"
    if not served.served_complete:
        return "incomplete"
    if not served.served_answer.strip():
        return "empty"
    passes = scorers.refusal_correct(case, complete=True, answer=served.served_answer, calls=calls)
    return "passes" if passes else "fails_rule"


def run_case(
    case: EvalCase,
    model: ChatModel,
    *,
    registry: ToolRegistry,
    budget: Budget,
    provider_name: str,
    model_name: str,
    measure_latency: bool,
    prompt: PromptRecord,
) -> CaseResult:
    """Ask one question through the real stack, under *prompt*, and score what came back."""
    return run_case_served(
        case,
        model,
        registry=registry,
        budget=budget,
        provider_name=provider_name,
        model_name=model_name,
        measure_latency=measure_latency,
        prompt=prompt,
    )[0]


def run_case_served(
    case: EvalCase,
    model: ChatModel,
    *,
    registry: ToolRegistry,
    budget: Budget,
    provider_name: str,
    model_name: str,
    measure_latency: bool,
    prompt: PromptRecord,
) -> tuple[CaseResult, ServedResult]:
    """`run_case`, and the same case as the caller saw it (R2.7ZU). The `CaseResult` is exactly
    what `run_case` returns."""
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
    turn: AnsweredTurn | None = None
    try:
        turn = copilot.answer_turn(
            EVAL_HOTEL, case.question, prompt_identity=(prompt.prompt_id, prompt.version)
        )
        stop_reason, complete = turn.response.stop_reason, turn.response.complete
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

    served_refusal: bool | None = None
    if case.expected != "answer":
        served_refusal = turn is not None and scorers.refusal_correct(
            case, complete=turn.response.complete, answer=turn.response.answer, calls=calls
        )
    served = ServedResult(
        case_id=case.case_id,
        expected=case.expected,
        model_text=recorder.responses[-1].text if recorder.responses else None,
        model_responses=len(recorder.responses),
        served_available=turn is not None,
        served_answer=turn.response.answer if turn is not None else None,
        served_complete=turn.response.complete if turn is not None else None,
        served_stop_reason=turn.response.stop_reason if turn is not None else None,
        answer_source=turn.answer_source if turn is not None else None,
        unavailable_reason=error_code if turn is None else None,
        served_refusal_correct=served_refusal,
        model_decline_status=None,
        served_decline_status=None,
    )
    if case.expected != "answer":
        served = replace(
            served,
            model_decline_status=_model_decline_status(case, served, calls),
            served_decline_status=_served_decline_status(case, served, calls),
        )

    result = CaseResult(
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
    return result, served


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


#: Every value `ServedResult.answer_source` can take, plus `unavailable` for nothing served.
ANSWER_SOURCES = (
    "model",
    "scope_guard",
    "cannot_answer_substitution",
    "not_found_replacement",
    "withheld",
    "unavailable",
)

SERVED_STATEMENT = (
    "What the caller was served, beside what the model wrote. The report's top-level measures "
    "score the model's text (with the response's completeness) and keep their meaning; these "
    "score the served answer. The two are never combined: a served pass whose answer_source is "
    "not 'model' credits the server's own guard or substitution, not the model. refusal and "
    "served_refusal pass an empty complete answer, and refusal passes a case the server refused "
    "before asking the model; the *_refusal_with_text measures do not. None of them judges "
    "wording or meaning."
)


def _served_block(rows: Sequence[ServedResult]) -> dict[str, Any]:
    """The `served` block of a report (R2.7ZU; the `*_with_text` measures and status counts,
    R2.7ZW), only when a run asks for it."""
    refusals = [
        row.served_refusal_correct for row in rows if row.served_refusal_correct is not None
    ]
    sources = Counter(row.answer_source or "unavailable" for row in rows)
    model_statuses = [row.model_decline_status for row in rows if row.model_decline_status]
    served_statuses = [row.served_decline_status for row in rows if row.served_decline_status]
    model_asked = [status for status in model_statuses if status != "not_asked"]
    return {
        "statement": SERVED_STATEMENT,
        "measures": {
            "served_refusal": {"passed": sum(refusals), "scored": len(refusals)},
            "served_completed": {
                "passed": sum(1 for row in rows if row.served_complete),
                "scored": len(rows),
            },
            # R2.7ZW: a refusal that says something -- over the cases the model was asked.
            "model_refusal_with_text": {
                "passed": model_asked.count("passes"),
                "scored": len(model_asked),
            },
            "served_refusal_with_text": {
                "passed": served_statuses.count("passes"),
                "scored": len(served_statuses),
            },
        },
        "answer_sources": {source: sources[source] for source in ANSWER_SOURCES},
        "model_decline_statuses": {
            status: model_statuses.count(status) for status in MODEL_DECLINE_STATUSES
        },
        "served_decline_statuses": {
            status: served_statuses.count(status) for status in SERVED_DECLINE_STATUSES
        },
        "cases": [asdict(row) for row in rows],
    }


def run(
    question_set: QuestionSet,
    model_for: Callable[[EvalCase], ChatModel],
    *,
    mode: str,
    provider: str,
    model: str,
    recorded_on: str,
    prompt: PromptRecord,
    budget: Budget | None = None,
    on_case: Callable[[EvalCase, RecordingModel | None], None] | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Run every case under *prompt* and return the report. Deterministic for a replay.

    *served* (R2.7ZU) adds a `served` block: each case as the caller saw it. Off by default, so
    a report without it -- every pinned one -- is exactly what it was."""
    registry = build_default_registry()
    budget = budget or Budget(timeout_seconds=30.0, max_output_tokens=1024)
    results: list[CaseResult] = []
    served_rows: list[ServedResult] = []
    for case in question_set.cases:
        case_model = model_for(case)
        result, served_row = run_case_served(
            case,
            case_model,
            registry=registry,
            budget=budget,
            provider_name=provider,
            model_name=model,
            measure_latency=mode == "live",
            prompt=prompt,
        )
        results.append(result)
        served_rows.append(served_row)
        if on_case is not None:
            on_case(case, case_model if isinstance(case_model, RecordingModel) else None)

    statement = (
        REFERENCE_STATEMENT
        if provider == REFERENCE_PROVIDER
        else live_statement(
            provider,
            model,
            recorded_on,
            len(question_set.cases),
            question_set.identity,
            prompt=prompt,
        )
    )
    report: dict[str, Any] = {
        "harness": HARNESS_VERSION,
        "mode": mode,
        "question_set": {
            "identity": question_set.identity,
            "checksum": question_set.checksum,
            "size": len(question_set.cases),
        },
        "prompt": {"identity": prompt.identity, "checksum": prompt.checksum},
        "model": {"provider": provider, "model": model},
        "recorded_on": recorded_on,
        "statement": statement,
        "measures": _measures(results),
        "cases": [asdict(result) for result in results],
    }
    if served:
        report["served"] = _served_block(served_rows)
    return report


def run_replays(
    replays: Replays, question_set: QuestionSet = COPILOT_EVAL_V1, *, prompt: PromptRecord
) -> dict[str, Any]:
    """Replay a recorded run under *prompt*. The report names the recording's model and date,
    not today's."""
    return run(
        question_set,
        lambda case: ReplayModel(replays.cases[case.case_id]),
        mode="replay",
        provider=replays.provider,
        model=replays.model,
        recorded_on=replays.recorded_on,
        prompt=prompt,
    )


def reference_report() -> dict[str, Any]:
    """v1's reference run, under the prompt its exchanges are bound to (`REFERENCE_PROMPT`)."""
    replays = load_replays(REFERENCE_REPLAYS, COPILOT_EVAL_V1, prompt=REFERENCE_PROMPT)
    return run_replays(replays, prompt=REFERENCE_PROMPT)


def reference_report_v2() -> dict[str, Any]:
    """`copilot_eval_v2`'s reference run: its own exchanges, against its own set (R2.7R), under
    the prompt they are bound to (`REFERENCE_PROMPT`)."""
    replays = load_replays(REFERENCE_REPLAYS_V2, COPILOT_EVAL_V2, prompt=REFERENCE_PROMPT)
    return run_replays(replays, COPILOT_EVAL_V2, prompt=REFERENCE_PROMPT)


def reference_report_v2_answer_v3() -> dict[str, Any]:
    """The v3 baseline (R2.7ZP): `copilot_eval_v2`'s hand-written turns, bound to and run under
    `BASELINE_V3_PROMPT`. Its own file, its own report; v2's are neither read for it nor moved."""
    replays = load_replays(
        REFERENCE_REPLAYS_V2_ANSWER_V3, COPILOT_EVAL_V2, prompt=BASELINE_V3_PROMPT
    )
    if replays.provider != REFERENCE_PROVIDER:
        raise ValueError(f"{REFERENCE_REPLAYS_V2_ANSWER_V3.name} is not a hand-written reference")
    report = run_replays(replays, COPILOT_EVAL_V2, prompt=BASELINE_V3_PROMPT)
    report["statement"] = BASELINE_V3_STATEMENT
    return report


def canonical(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


#: R2.7ZL. Where the current code, given a pinned report's own exchanges, reports something other
#: than the pinned file -- each difference reviewed, and declared here instead of being written into
#: a frozen file. (case id, field) -> the value the current code reports. The copilot refuses
#: `inject_other_hotel` (a hotel-labelled id, R2.7ZL) and `another_property` (another property in
#: words, R2.7ZT) before the model is asked, so neither is offered a catalogue.
REVIEWED_DELTAS: dict[tuple[str, str], list[str]] = {
    ("inject_other_hotel", "tools_offered"): [],
    ("another_property", "tools_offered"): [],
}


def apply_reviewed_deltas(
    pinned: Mapping[str, Any],
    deltas: Mapping[tuple[str, str], list[str]] = REVIEWED_DELTAS,
) -> dict[str, Any]:
    """A deep copy of *pinned* with *deltas* applied; *pinned* itself is never modified.

    Only a delta declared in `REVIEWED_DELTAS`, with its declared value, is accepted, and only on
    a case and a field the report already has -- so this can express those reviewed
    differences and nothing broader.
    """
    expected = copy.deepcopy(dict(pinned))
    cases = {case["case_id"]: case for case in expected["cases"]}
    for (case_id, field), value in deltas.items():
        if REVIEWED_DELTAS.get((case_id, field)) != value or value != []:
            raise ValueError(f"not a reviewed delta: {case_id}.{field}")
        if case_id not in cases:
            raise ValueError(f"the report has no case {case_id!r}")
        if field not in cases[case_id]:
            raise ValueError(f"case {case_id!r} has no field {field!r}")
        cases[case_id][field] = list(value)
    return expected


def expected_report(pinned_path: Path) -> str:
    """What the current code must reproduce from a pinned report's exchanges: the pinned file,
    with the reviewed deltas applied, in canonical form. The file is only read."""
    return canonical(apply_reviewed_deltas(json.loads(pinned_path.read_text(encoding="utf-8"))))


#: Why `--write-reference` is refused for v1 (R2.7ZL4).
FROZEN_V1_REFUSAL = (
    "copilot_eval_v1's reference report is frozen (R2.7R): reference_report.json is never "
    "regenerated, and its digest is pinned. Declare a reviewed difference in REVIEWED_DELTAS "
    "(tests/evaluation/harness.py) instead; without --write-reference this prints the report "
    "the current code produces."
)

#: Why `--prompt copilot_answer@v3` is refused for v1 (R2.7ZP): only v2's set has a v3 baseline.
V3_BASELINE_SET_REFUSAL = (
    f"{BASELINE_V3_PROMPT.identity} has a baseline for {COPILOT_EVAL_V2.identity} only; "
    f"pass --set {COPILOT_EVAL_V2.identity}."
)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Print, or regenerate, a reference report.")
    parser.add_argument("--write-reference", action="store_true")
    parser.add_argument(
        "--set",
        dest="question_set",
        choices=[COPILOT_EVAL_V1.identity, COPILOT_EVAL_V2.identity],
        default=COPILOT_EVAL_V1.identity,
        help="Which set's reference report. Default: copilot_eval_v1.",
    )
    parser.add_argument(
        "--prompt",
        choices=[REFERENCE_PROMPT.identity, BASELINE_V3_PROMPT.identity],
        default=REFERENCE_PROMPT.identity,
        help=f"Which prompt's baseline. Default: {REFERENCE_PROMPT.identity}; "
        f"{BASELINE_V3_PROMPT.identity} selects the separate v3 baseline (R2.7ZP).",
    )
    options = parser.parse_args(argv)
    v2 = options.question_set == COPILOT_EVAL_V2.identity
    v3 = options.prompt == BASELINE_V3_PROMPT.identity
    if options.write_reference and not v2:
        parser.error(FROZEN_V1_REFUSAL)  # exits 2, before anything is computed or written
    if v3 and not v2:
        parser.error(V3_BASELINE_SET_REFUSAL)  # exits 2, before anything is computed or written
    if v3:
        report = canonical(reference_report_v2_answer_v3())
        target = REFERENCE_REPORT_V2_ANSWER_V3
    else:
        report = canonical(reference_report_v2() if v2 else reference_report())
        target = REFERENCE_REPORT_V2 if v2 else REFERENCE_REPORT
    if options.write_reference:
        target.write_text(report, encoding="utf-8", newline="\n")
        print(f"wrote tests/evaluation/{target.name}")
    else:
        print(report)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "ANSWER_SOURCES",
    "BASELINE_V3_PROMPT",
    "BASELINE_V3_STATEMENT",
    "FROZEN_V1_REFUSAL",
    "HARNESS_VERSION",
    "MEASURES",
    "MODEL_DECLINE_STATUSES",
    "REFERENCE_PROMPT",
    "REFERENCE_REPLAYS",
    "REFERENCE_REPLAYS_V2",
    "REFERENCE_REPLAYS_V2_ANSWER_V3",
    "REFERENCE_REPORT",
    "REFERENCE_REPORT_V2",
    "REFERENCE_REPORT_V2_ANSWER_V3",
    "REVIEWED_DELTAS",
    "SERVED_DECLINE_STATUSES",
    "SERVED_STATEMENT",
    "V3_BASELINE_SET_REFUSAL",
    "CaseResult",
    "ServedResult",
    "apply_reviewed_deltas",
    "canonical",
    "expected_report",
    "reference_report",
    "reference_report_v2",
    "reference_report_v2_answer_v3",
    "run",
    "run_case",
    "run_case_served",
    "run_replays",
]
