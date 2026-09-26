"""Stage 7.7 — the copilot, without a database and without a network.

    A. the prompt                 copilot_answer@v1: registered, checksummed, and says the rules
    B. the figure check           what `ungrounded_figures` accepts and what it refuses
    C. the loop's usage totals    additive, and nothing else about the loop changed
    D. the budget error           LLM_BUDGET_EXHAUSTED, 429, Retry-After only when it helps
    E. the HTTP contract          request and response field sets, pinned
    F. the service                the orchestration order and the result mapping, against fakes
    G. the boundaries             which modules may reach `app.llm`, and what they may read
    H. the table                  `llm_invocations` holds no content, by column set

`tests/integration/test_copilot_api.py` drives the real endpoint over real PostgreSQL.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import CheckConstraint, Table

from app.copilot.catalogue import build_catalogue
from app.copilot.contracts import ToolOutcome
from app.copilot.grounding import ungrounded_figures
from app.copilot.loop import LoopResult, ToolLoop
from app.copilot.registry import build_default_registry
from app.core.errors import (
    AppError,
    RateLimitExceededError,
    register_exception_handlers,
)
from app.llm.base import Budget, ChatRequest, ChatResponse, Message, ToolCall
from app.llm.boundary import GuardedChatModel
from app.llm.circuit import CircuitBreaker, CircuitPolicy
from app.llm.errors import (
    DECLARED_FAILURES,
    LlmBudgetExhaustedError,
    LlmCircuitOpenError,
    LlmError,
    LlmInvalidResponseError,
    LlmRateLimitedError,
    LlmUnavailableError,
)
from app.llm.prompts import REGISTRY
from app.llm.prompts.registry import COPILOT_ANSWER_V1, COPILOT_ANSWER_V2
from app.llm.testing import FailingModel, ScriptedModel, ScriptedTurn
from app.models.llm_invocation import LLM_MODEL_ERROR_CODES, LLM_STOP_REASONS, LlmInvocation
from app.schemas.copilot import (
    MAX_QUESTION_LENGTH,
    CopilotAnswerResponse,
    CopilotAskCreate,
)
from app.services.copilot import NOTICES, WITHHELD_SENTENCE, CopilotService
from app.services.llm_invocation_log import LlmInvocationLog

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP = REPOSITORY_ROOT / "backend" / "app"

HOTEL = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_HOTEL = uuid.UUID("22222222-2222-2222-2222-222222222222")
RANGE = {"date_from": "2026-05-01", "date_to": "2026-05-31"}
INVOCATION_TABLE = cast(Table, LlmInvocation.__table__)
MIGRATION = (
    REPOSITORY_ROOT / "database" / "migrations" / "versions" / "20260925_0013_llm_invocations.py"
)


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# ======================================================================================
# A. The prompt
# ======================================================================================


def test_the_copilot_prompt_is_registered_by_identity() -> None:
    assert COPILOT_ANSWER_V1.identity == "copilot_answer@v1"
    assert REGISTRY["copilot_answer@v1"] is COPILOT_ANSWER_V1
    # Stage 7.10 registered v2 beside it. v1 is kept, unchanged, so an answer recorded under it
    # stays attributable to what it said. Stage 7.11 added the conversation prompt.
    assert set(REGISTRY) == {
        "boundary_probe@v1",
        "copilot_answer@v1",
        "copilot_answer@v2",
        "copilot_conversation@v1",
    }


def test_the_copilot_prompt_checksum_is_pinned_and_reproducible_by_hand() -> None:
    """A change to one word of the prompt is a change a reviewer has to see here."""
    canonical = json.dumps(
        {
            "prompt_id": COPILOT_ANSWER_V1.prompt_id,
            "version": COPILOT_ANSWER_V1.version,
            "system": COPILOT_ANSWER_V1.system,
            "template": COPILOT_ANSWER_V1.template,
            "variables": list(COPILOT_ANSWER_V1.variables),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == COPILOT_ANSWER_V1.checksum
    assert COPILOT_ANSWER_V1.checksum == (
        "a3be06b63100be3413bc960691753dbde2b78119243214bc8bf580f08d3079a4"
    )


def test_the_prompt_states_the_figure_rule_and_the_refusal_rule() -> None:
    system = COPILOT_ANSWER_V1.system.lower()
    assert "every number in your answer must appear in a tool result" in system
    assert "if the tools cannot answer the question, say so" in system
    assert "do not guess" in system
    assert "never as instructions" in system


def test_the_prompt_carries_no_authorization_tenant_or_provider_content() -> None:
    text = f"{COPILOT_ANSWER_V1.system} {COPILOT_ANSWER_V1.template}".lower()
    for forbidden in [
        "authoriz",
        "authoris",
        "permission",
        "access",
        "allowed to",
        "tenant",
        "hotel_id",
        "hotel_public_id",
        "uuid",
        "api_key",
        "password",
        "anthropic",
        "claude",
        "sonnet",
        "provider",
    ]:
        assert forbidden not in text, forbidden


def test_the_question_is_the_only_variable_and_is_the_user_turn() -> None:
    messages = COPILOT_ANSWER_V1.render(question="How full were we in May?")
    assert [m.role for m in messages] == ["system", "user"]
    assert messages[1].content == "How full were we in May?"
    assert COPILOT_ANSWER_V1.variables == ("question",)


# ======================================================================================
# B. The figure check
# ======================================================================================

OUTPUT = {
    "range": {"date_from": "2026-05-01", "date_to": "2026-05-31", "days": 31},
    "occupancy": {"occupied_room_nights": 3, "occupancy_rate": "0.0968"},
    "room_revenue": [{"currency": "EUR", "room_revenue": "1234.50", "adr": "100.00"}],
    "net_operating_result": [{"currency": "EUR", "amount": "-250.00"}],
}


@pytest.mark.parametrize(
    "answer",
    [
        "3 room nights were occupied.",
        "Room revenue was 1234.50 EUR.",
        "Room revenue was 1,234.50 EUR.",
        "Room revenue was about 1,235 EUR.",
        "Occupancy was 9.68%.",
        "Occupancy was 9.7%.",
        "Occupancy was 10%.",
        "Between 1 May 2026 and 31 May 2026.",
        "The average daily rate was 100 EUR.",
        "The result was a loss of 250.00.",
        "The result was -250.00.",
        "No figures at all.",
    ],
)
def test_a_figure_traceable_to_a_tool_output_is_grounded(answer: str) -> None:
    assert ungrounded_figures(answer, outputs=[OUTPUT], question="") == ()


@pytest.mark.parametrize(
    ("answer", "offending"),
    [
        ("Occupancy was 97.3%.", ("97.3",)),
        ("Revenue was 1334.50 EUR.", ("1334.50",)),
        ("Revenue plus the result is 984.50.", ("984.50",)),
        ("Hotel B made 900.00 EUR.", ("900.00",)),
        ("7 and 7 again and 8.", ("7", "8")),
    ],
)
def test_a_figure_no_tool_returned_is_refused(answer: str, offending: tuple[str, ...]) -> None:
    """Invented, computed (sums), or another property's figure: none is traceable."""
    assert ungrounded_figures(answer, outputs=[OUTPUT], question="") == offending


def test_a_figure_the_caller_wrote_is_grounded_by_the_question() -> None:
    assert ungrounded_figures("You asked about 2025.", outputs=[], question="And in 2025?") == ()


def test_with_no_tool_output_every_figure_is_refused() -> None:
    assert ungrounded_figures("Occupancy was 71%.", outputs=[], question="How full?") == ("71",)


# ======================================================================================
# C. The loop's usage totals
# ======================================================================================

SPECS = build_catalogue(build_default_registry(), build_default_registry().names())


def run_loop(model: Any, execute: Callable[[ToolCall], ToolOutcome]) -> LoopResult:
    return ToolLoop(model).run(
        prompt_id="copilot_answer",
        prompt_version="v1",
        messages=COPILOT_ANSWER_V1.render(question="q"),
        budget=Budget(timeout_seconds=5, max_output_tokens=100),
        tools=SPECS,
        execute=execute,
    )


def ok_outcome(call: ToolCall) -> ToolOutcome:
    return ToolOutcome(tool=call.name, outcome="succeeded", output={"value": 1})


def test_usage_is_summed_over_every_model_call() -> None:
    model = ScriptedModel(
        [ScriptedTurn(tool_calls=(ToolCall(id="c1", name="get_hotel_kpis", arguments={}),)), "a"],
        output_tokens=7,
    )
    result = run_loop(model, ok_outcome)

    assert result.model_calls == 2
    assert (result.input_tokens, result.output_tokens) == (20, 14)


def test_a_failed_model_call_adds_no_usage() -> None:
    result = run_loop(FailingModel(LlmUnavailableError), ok_outcome)
    assert (result.input_tokens, result.output_tokens) == (0, 0)


def test_the_loop_result_gained_only_the_two_usage_fields() -> None:
    from dataclasses import fields

    assert [f.name for f in fields(LoopResult)] == [
        "stop_reason",
        "text",
        "rounds",
        "model_calls",
        "outcomes",
        "prompt_id",
        "prompt_version",
        "model_error",
        "input_tokens",
        "output_tokens",
    ]


# ======================================================================================
# D. The budget error
# ======================================================================================


def test_the_budget_error_keeps_its_declared_code_and_status() -> None:
    error = LlmBudgetExhaustedError("spent", retry_after=42)
    assert (error.code, error.status_code, error.retry_after) == ("LLM_BUDGET_EXHAUSTED", 429, 42)
    assert LlmBudgetExhaustedError().retry_after is None
    assert DECLARED_FAILURES["LLM_BUDGET_EXHAUSTED"] is LlmBudgetExhaustedError
    assert len(DECLARED_FAILURES) == 6


def test_budget_exhaustion_is_distinct_from_the_endpoint_rate_limit() -> None:
    assert not issubclass(LlmBudgetExhaustedError, RateLimitExceededError)
    assert LlmBudgetExhaustedError.code != RateLimitExceededError.code


def raising_app(error: Exception) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/x")
    def boom() -> None:
        raise error

    return TestClient(app, raise_server_exceptions=False)


def test_a_spent_allowance_carries_retry_after() -> None:
    response = raising_app(LlmBudgetExhaustedError("spent", retry_after=17)).get("/x")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert response.json()["error"]["code"] == "LLM_BUDGET_EXHAUSTED"


def test_a_per_request_ceiling_carries_no_retry_after() -> None:
    """Retrying the same over-budget request later would be refused again."""
    response = raising_app(LlmBudgetExhaustedError()).get("/x")
    assert response.status_code == 429
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize("error", [LlmRateLimitedError(), LlmUnavailableError(), AppError()])
def test_no_other_app_error_gained_a_retry_after(error: AppError) -> None:
    assert "Retry-After" not in raising_app(error).get("/x").headers


# ======================================================================================
# E. The HTTP contract
# ======================================================================================


def test_the_request_is_exactly_a_question() -> None:
    assert set(CopilotAskCreate.model_fields) == {"question"}
    assert CopilotAskCreate.model_config.get("extra") == "forbid"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": "x" * (MAX_QUESTION_LENGTH + 1)},
        {"question": "ok", "hotel_id": 2},
        {"question": "ok", "hotel_public_id": str(OTHER_HOTEL)},
        {"question": "ok", "model": "other"},
        {"question": 42},
    ],
)
def test_a_malformed_request_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(PydanticValidationError):
        CopilotAskCreate.model_validate(body)


def test_a_question_is_trimmed_and_may_be_exactly_the_maximum() -> None:
    assert CopilotAskCreate.model_validate({"question": "  hi  "}).question == "hi"
    assert CopilotAskCreate.model_validate({"question": "x" * MAX_QUESTION_LENGTH})


def test_the_response_is_exactly_these_fields() -> None:
    """Stage 7.10 added two fields, additively: nothing was removed or renamed."""
    assert set(CopilotAnswerResponse.model_fields) == {
        "answer",
        "complete",
        "stop_reason",
        "notice",
        "tools_used",
        "prompt_id",
        "prompt_version",
        "invocation_public_id",
        "citations",
        "document_evidence",
    }


@pytest.mark.parametrize(
    "forbidden",
    ["provider", "model", "token", "key", "hotel", "tenant", "cost", "latency", "question"],
)
def test_the_response_names_no_provider_cost_or_tenant(forbidden: str) -> None:
    schema = json.dumps(CopilotAnswerResponse.model_json_schema()["properties"])
    fields = set(CopilotAnswerResponse.model_fields) | {
        name
        for model in CopilotAnswerResponse.model_json_schema().get("$defs", {}).values()
        for name in model.get("properties", {})
    }
    assert not any(forbidden in name for name in fields), forbidden
    del schema


def test_the_stop_reasons_agree_everywhere() -> None:
    from typing import get_args

    from app.copilot.loop import StopReason as LoopStopReason
    from app.schemas.copilot import StopReason

    assert set(get_args(StopReason)) == set(LLM_STOP_REASONS)
    assert set(get_args(LoopStopReason)) | {"ungrounded_figures"} == set(LLM_STOP_REASONS)
    assert set(NOTICES) == set(LLM_STOP_REASONS) - {"completed"}


# ======================================================================================
# F. The service, against fakes
# ======================================================================================


class FakeHotel:
    id = 41


class FakeScope:
    def __init__(self) -> None:
        self.resolved: list[uuid.UUID] = []

    def require_hotel(self, hotel_public_id: uuid.UUID) -> FakeHotel:
        self.resolved.append(hotel_public_id)
        return FakeHotel()


class FakeInvocations:
    """Stands in for ToolInvocationService and records what the loop asked of it."""

    def __init__(self, outcomes: list[ToolOutcome] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[uuid.UUID, str, Mapping[str, Any], frozenset[str]]] = []
        self.order: list[str] = []

    def permitted_tools(self, hotel_public_id: uuid.UUID) -> tuple[str, ...]:
        self.order.append(f"permitted:{hotel_public_id}")
        return ("get_daily_series", "get_hotel_kpis")

    def invoke(
        self,
        hotel_public_id: uuid.UUID,
        name: str,
        arguments: Mapping[str, Any],
        *,
        offered: Any,
        evidence: Any = None,
    ) -> ToolOutcome:
        # Stage 7.10: the service hands every call its request's evidence ledger. These fakes
        # return structured outputs only, so nothing is ever staged in it.
        self.calls.append((hotel_public_id, name, dict(arguments), frozenset(offered)))
        if self.outcomes:
            return self.outcomes.pop(0)
        return ToolOutcome(
            tool=name,
            outcome="succeeded",
            output={"range": {"date_from": "2026-05-01"}, "occupied_room_nights": 3},
        )


class FakeRow:
    public_id = uuid.UUID("33333333-3333-3333-3333-333333333333")


class FakeLog:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, **values: Any) -> FakeRow:
        self.rows.append(values)
        return FakeRow()


class FakeSession:
    def __init__(self) -> None:
        self.log: list[str] = []

    def commit(self) -> None:
        self.log.append("commit")

    def rollback(self) -> None:
        self.log.append("rollback")


class Harness:
    def __init__(self, model: Any, outcomes: list[ToolOutcome] | None = None) -> None:
        self.scope = FakeScope()
        self.invocations = FakeInvocations(outcomes)
        self.log = FakeLog()
        self.session = FakeSession()
        self.model = model
        ticks = iter([10.0, 10.25])
        self.service = CopilotService(
            cast(Any, self.session),
            model,
            build_default_registry(),
            cast(Any, self.invocations),
            cast(Any, self.log),
            cast(Any, self.scope),
            budget=Budget(timeout_seconds=5, max_output_tokens=100),
            provider_name="upstream-provider",
            model_name="upstream-model-name",
            monotonic=lambda: next(ticks),
        )

    def ask(self, question: str = "How many room nights in May?") -> CopilotAnswerResponse:
        return self.service.ask(HOTEL, question)


def kpis(call_id: str = "c1", arguments: Mapping[str, Any] | None = None) -> ScriptedTurn:
    call = ToolCall(id=call_id, name="get_hotel_kpis", arguments=dict(arguments or RANGE))
    return ScriptedTurn(tool_calls=(call,))


def test_a_complete_answer_is_labelled_complete_and_recorded() -> None:
    harness = Harness(ScriptedModel([kpis(), "3 room nights were occupied."]))

    response = harness.ask()

    assert response.complete is True
    assert response.stop_reason == "completed"
    assert response.notice is None
    assert response.answer == "3 room nights were occupied."
    assert [u.model_dump() for u in response.tools_used] == [
        {"tool": "get_hotel_kpis", "outcome": "succeeded"}
    ]
    # Stage 7.10: the copilot renders v2.
    assert (response.prompt_id, response.prompt_version) == ("copilot_answer", "v2")
    assert response.invocation_public_id == FakeRow.public_id
    [row] = harness.log.rows
    assert row == {
        "hotel_id": FakeHotel.id,
        "prompt_id": "copilot_answer",
        "prompt_version": "v2",
        "provider": "upstream-provider",
        "model": "upstream-model-name",
        "stop_reason": "completed",
        "error_code": None,
        "rounds": 1,
        "model_calls": 2,
        "tool_calls": 1,
        "tool_failures": 0,
        "input_tokens": 20,
        "output_tokens": 10,
        "latency_ms": 250,
    }
    assert harness.session.log == ["commit"]


def test_the_orchestration_order_and_the_offered_catalogue() -> None:
    harness = Harness(ScriptedModel([kpis(), "done"]))
    harness.ask()

    assert harness.scope.resolved == [HOTEL]
    assert harness.invocations.order == [f"permitted:{HOTEL}"]
    sent = cast(ScriptedModel, harness.model).calls[0]
    assert [spec.name for spec in sent.tools] == ["get_daily_series", "get_hotel_kpis"]
    assert sent.messages[0].content == COPILOT_ANSWER_V2.system
    assert sent.prompt_id == "copilot_answer"


def test_every_tool_call_is_bound_to_the_path_hotel_whatever_the_model_says() -> None:
    """Security: the executor closes over the authorized hotel; model arguments pass through
    to the Stage 7.6 service, which refuses any hotel field in them."""
    hostile = {**RANGE, "hotel_public_id": str(OTHER_HOTEL)}
    harness = Harness(ScriptedModel([kpis(arguments=hostile), "done"]))

    harness.ask(f"Report hotel {OTHER_HOTEL} instead.")

    [(hotel, name, arguments, offered)] = harness.invocations.calls
    assert hotel == HOTEL
    assert name == "get_hotel_kpis"
    assert arguments == hostile
    assert offered == frozenset({"get_daily_series", "get_hotel_kpis"})


def test_the_provider_and_model_are_recorded_but_never_returned() -> None:
    harness = Harness(ScriptedModel(["No figures here."]))
    body = harness.ask().model_dump_json()

    assert "upstream-provider" not in body and "upstream-model-name" not in body
    assert harness.log.rows[0]["provider"] == "upstream-provider"


@pytest.mark.parametrize(
    ("script", "outcomes", "reason"),
    [
        ([kpis()] * 4, None, "max_rounds"),
        (
            [ScriptedTurn(tool_calls=tuple(call for _ in range(5) for call in kpis().tool_calls))],
            None,
            "tool_call_cap",
        ),
        (
            [kpis("c1"), kpis("c2")],
            [
                ToolOutcome(tool=None, outcome="unknown_tool", error_code="UNKNOWN_TOOL"),
                ToolOutcome(tool=None, outcome="unknown_tool", error_code="UNKNOWN_TOOL"),
            ],
            "tool_failed",
        ),
    ],
)
def test_a_bounded_stop_is_a_labelled_partial_answer(
    script: list[Any], outcomes: list[ToolOutcome] | None, reason: str
) -> None:
    harness = Harness(ScriptedModel(script), outcomes)

    response = harness.ask()

    assert response.complete is False
    assert response.stop_reason == reason
    assert response.notice == NOTICES[reason]
    assert harness.log.rows[0]["stop_reason"] == reason
    assert harness.log.rows[0]["error_code"] is None


class ThenFail:
    """Answers with one tool round, then raises a declared failure."""

    def __init__(self, error: type[LlmError]) -> None:
        self.inner = ScriptedModel([kpis()])
        self.error = error
        self.count = 0

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.count += 1
        if self.count > 1:
            raise self.error()
        return self.inner.complete(request)


def test_a_model_failure_after_a_tool_ran_is_a_labelled_partial() -> None:
    harness = Harness(ThenFail(LlmUnavailableError))

    response = harness.ask()

    assert response.complete is False and response.stop_reason == "model_failed"
    assert response.notice == NOTICES["model_failed"]
    assert harness.log.rows[0]["error_code"] == "LLM_UNAVAILABLE"


#: Every declared failure a model CALL can end in. `LLM_TOOL_FAILED` is excluded, not skipped:
#: the loop reports a tool failure as a stop reason, never as a model error.
MODEL_FAILURES = [e for e in DECLARED_FAILURES.values() if e.code != "LLM_TOOL_FAILED"]


@pytest.mark.parametrize("error", MODEL_FAILURES)
def test_a_model_failure_before_any_tool_is_recorded_and_re_raised(error: type[LlmError]) -> None:
    """§5.7's status and code reach the client unchanged, and the request is still accounted."""
    harness = Harness(FailingModel(error))

    with pytest.raises(error):
        harness.ask()

    [row] = harness.log.rows
    assert row["stop_reason"] == "model_failed"
    assert row["error_code"] == error.code
    assert row["error_code"] in LLM_MODEL_ERROR_CODES
    assert harness.session.log == ["commit"]


def test_an_open_circuit_is_recorded_as_unavailable_and_re_raised() -> None:
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1))
    breaker.record_failure(breaker.acquire())
    inner = ScriptedModel("never")
    harness = Harness(
        GuardedChatModel(inner, sleep=lambda _s: None, jitter=lambda _a, _b: 0.0, breaker=breaker)
    )

    with pytest.raises(LlmCircuitOpenError):
        harness.ask()

    assert inner.calls == []
    assert harness.log.rows[0]["error_code"] == "LLM_UNAVAILABLE"


def test_an_ungrounded_complete_answer_is_withheld() -> None:
    """The prompt's rule, checked: no tool returned 97.3, so the answer is not served."""
    harness = Harness(ScriptedModel([kpis(), "Occupancy was 97.3%."]))

    response = harness.ask()

    assert response.answer == ""
    assert response.complete is False
    assert response.stop_reason == "ungrounded_figures"
    assert response.notice == NOTICES["ungrounded_figures"]
    assert harness.log.rows[0]["stop_reason"] == "ungrounded_figures"


def test_an_answer_with_no_tool_and_an_invented_figure_is_withheld() -> None:
    harness = Harness(ScriptedModel(["Occupancy was 71% last month."]))
    response = harness.ask("How full were we?")
    assert (response.answer, response.stop_reason) == ("", "ungrounded_figures")


def test_an_ungrounded_partial_keeps_its_reason_and_withholds_the_text() -> None:
    harness = Harness(ThenFail(LlmUnavailableError))
    cast(ThenFail, harness.model).inner = ScriptedModel(
        [ScriptedTurn(text="Revenue was 999.", tool_calls=kpis().tool_calls)]
    )

    response = harness.ask()

    assert response.stop_reason == "model_failed"
    assert response.answer == ""
    assert response.notice == f"{NOTICES['model_failed']} {WITHHELD_SENTENCE}"


def test_a_grounded_answer_is_served_verbatim() -> None:
    harness = Harness(ScriptedModel([kpis(), "In May 2026, 3 room nights were occupied."]))
    assert harness.ask().answer == "In May 2026, 3 room nights were occupied."


def test_no_content_reaches_the_accounting_row() -> None:
    harness = Harness(ScriptedModel([kpis(), "ANSWER-SENTINEL"]))
    harness.ask("QUESTION-SENTINEL")

    dumped = json.dumps(harness.log.rows, default=str)
    for sentinel in [
        "QUESTION-SENTINEL",
        "ANSWER-SENTINEL",
        COPILOT_ANSWER_V1.system,
        COPILOT_ANSWER_V2.system,
        "2026-05",
    ]:
        assert sentinel not in dumped


# ======================================================================================
# G. The boundaries
# ======================================================================================


def test_only_the_copilot_service_among_services_reaches_the_llm_package() -> None:
    reaching = {
        path.name: sorted(i for i in imports_of(path) if i.startswith("app.llm"))
        for path in python_files(APP / "services")
        if any(i.startswith("app.llm") for i in imports_of(path))
    }
    assert reaching == {
        "copilot.py": ["app.llm.base", "app.llm.prompts.registry"],
    }


def test_the_api_reaches_the_llm_package_only_through_the_composition_root() -> None:
    reaching = {
        path.relative_to(APP).as_posix(): sorted(
            i for i in imports_of(path) if i.startswith("app.llm")
        )
        for path in python_files(APP / "api")
        if any(i.startswith("app.llm") for i in imports_of(path))
    }
    assert reaching == {
        "api/deps.py": ["app.llm.base", "app.llm.errors", "app.llm.factory"],
    }


def test_the_copilot_services_import_closure_reaches_no_vendor() -> None:
    """§5.1, asserted as it is written: the closure reaches the seam and nothing vendor-specific."""
    probe = (
        "import sys; import app.services.copilot; "
        "bad = sorted(m for m in sys.modules if m.startswith(('app.llm.providers', "
        "'app.llm.factory', 'anthropic'))); print(bad)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT / "backend",
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_nothing_outside_the_seam_reads_who_answered() -> None:
    """The claim in `ChatResponse`'s docstring, made true (Stage 7.7).

    Every module outside `app/llm` that imports from `app.llm` is a consumer of the seam; none
    may read `.provider`, `.model` or `.diagnostics` off anything. Attribution is resolved by the
    composition root from settings and handed to the service as opaque strings.
    """
    offenders: list[str] = []
    for path in python_files(APP):
        if "llm" in path.relative_to(APP).parts[:1]:
            continue
        if not any(i.startswith("app.llm") for i in imports_of(path)):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                "provider",
                "model",
                "diagnostics",
            }:
                offenders.append(f"{path.relative_to(APP).as_posix()}:{node.lineno} .{node.attr}")

    assert offenders == []


def test_the_guard_above_would_catch_a_real_reader() -> None:
    """A positive control, so the scan above cannot pass vacuously."""
    tree = ast.parse("def f(response):\n    return response.model\n")
    assert any(isinstance(n, ast.Attribute) and n.attr == "model" for n in ast.walk(tree))


def test_the_budget_cannot_run_before_membership_is_established() -> None:
    """Authorization before the hotel is charged, as a property of the dependency GRAPH.

    `copilot_budget` depends on the route's own role dependency, so FastAPI cannot resolve it
    before membership is proved -- whatever order a decorator lists its dependencies in. The
    route lists them in that order as well; this pins the stronger, structural half.
    """
    import typing

    from app.api import deps

    hints = typing.get_type_hints(deps.copilot_budget, include_extras=True)
    _, marker = typing.get_args(hints["_member"])
    assert marker.dependency is deps.require_copilot_member
    assert deps.require_copilot_member.__qualname__.startswith("require_role.")

    route_dependencies = [
        dependency.dependency
        for route in __import__("app.api.v1.endpoints.copilot", fromlist=["router"]).router.routes
        for dependency in getattr(route, "dependencies", [])
    ]
    assert route_dependencies == [deps.require_copilot_member, deps.copilot_budget]


def test_the_route_imports_no_llm_module_and_no_repository() -> None:
    imported = imports_of(APP / "api" / "v1" / "endpoints" / "copilot.py")
    assert not any(i.startswith(("app.llm", "app.repositories", "app.copilot")) for i in imported)


def test_the_copilot_adds_no_rag_memory_or_agent_machinery() -> None:
    sources = [
        APP / "services" / "copilot.py",
        APP / "services" / "llm_invocation_log.py",
        APP / "copilot" / "grounding.py",
        APP / "api" / "v1" / "endpoints" / "copilot.py",
        APP / "schemas" / "copilot.py",
    ]
    for path in sources:
        code = ast.unparse(ast.parse(path.read_text(encoding="utf-8")))
        code = "\n".join(
            line for line in code.splitlines() if not line.lstrip().startswith(("'", '"'))
        ).lower()
        for word in ["embedding", "pgvector", "retriev", "conversation", "recommend"]:
            assert word not in code, (path.name, word)
        # Word-bounded: "upstream" names the provider this deployment routes to.
        assert not re.search(r"\bstream(ing)?\b", code), path.name


# ======================================================================================
# H. The table
# ======================================================================================


def test_the_invocation_table_has_exactly_these_columns() -> None:
    """No column a question, an answer, a prompt, a tool output or a credential could use."""
    assert set(INVOCATION_TABLE.columns.keys()) == {
        "id",
        "public_id",
        "hotel_id",
        "actor_user_id",
        "prompt_id",
        "prompt_version",
        "provider",
        "model",
        "stop_reason",
        "complete",
        "error_code",
        "rounds",
        "model_calls",
        "tool_calls",
        "tool_failures",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "request_id",
        "created_at",
    }


def test_the_recorder_accepts_no_content() -> None:
    parameters = set(inspect.signature(LlmInvocationLog.record).parameters) - {"self"}
    # Identities (`prompt_id`, `prompt_version`) are allowed; anything that could carry TEXT is not.
    for forbidden in [
        "question",
        "answer",
        "text",
        "prompt",
        "prompt_text",
        "system",
        "content",
        "output",
        "tool_output",
        "arguments",
        "key",
        "api_key",
        "credentials",
    ]:
        assert forbidden not in parameters, forbidden
    assert "actor" not in parameters and "user" not in parameters


def test_the_table_checks_describe_data_not_the_loops_policy() -> None:
    checks = " ".join(
        str(c.sqltext) for c in INVOCATION_TABLE.constraints if isinstance(c, CheckConstraint)
    )
    for policy in ["<= 3", "<= 4", "<= 2", "BETWEEN 0 AND 3", "BETWEEN 1 AND 4"]:
        assert policy not in checks, policy


def test_the_migration_and_the_model_declare_the_same_constraints() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    for constraint in INVOCATION_TABLE.constraints:
        name = str(constraint.name)
        assert name in migration, name
    for index in INVOCATION_TABLE.indexes:
        assert str(index.name) in migration
    assert "CREATE TRIGGER trg_llm_invocations_append_only" in migration
    assert "BEFORE UPDATE OR DELETE ON llm_invocations" in migration
    assert "ON DELETE RESTRICT" in migration


def test_the_migration_touches_no_existing_table() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    upgrade = migration[migration.index("def upgrade") : migration.index("def downgrade")]
    assert "ALTER TABLE" not in upgrade
    assert "DROP" not in upgrade
    assert upgrade.count("CREATE TABLE") == 1


def test_messages_used_by_the_loop_are_still_the_stage_7_6_shapes() -> None:
    assert Message(role="user", content="x").tool_calls == ()


def test_an_invalid_response_is_still_a_declared_failure() -> None:
    assert issubclass(LlmInvalidResponseError, LlmError)
