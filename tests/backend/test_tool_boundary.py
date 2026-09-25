"""Stage 7.6 — the tool boundary, the circuit breaker and failure mode 4. No database, no network.

What is proved here, and where the real-PostgreSQL half lives:

    A. the circuit breaker               semantics of §5.8, on an injected clock
    B. the seam's tool types             ChatRequest.tools / ChatResponse.tool_calls / Message
    C. the adapter's tool translation    against a stand-in with the SDK's shape
    D. the registry                      allow-list, fail-closed lookup, structural tenant rule
    E. the five tools                    contracts, delegation, schemas, side effects
    F. the catalogue                     deterministic, three fields, no authorization data
    G. the loop                          §5.4 bounds and §5.7 row 4, against ScriptedModel
    H. the invocation service            ordering and outcomes, against fakes
    S. the brief's security tests A-M    each named, each pointing at what proves it

`tests/integration/test_tool_invocation.py` runs the same service against a real database with
two hotels, real memberships, the real audit trail and the served demand model.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from app.copilot import catalogue as catalogue_module
from app.copilot import loop as loop_module
from app.copilot import registry as registry_module
from app.copilot.catalogue import build_catalogue
from app.copilot.contracts import (
    HOTEL_IDENTIFIER_FIELD,
    DateRangeArguments,
    ToolArguments,
    ToolContext,
    ToolContract,
    ToolOutcome,
    ToolOutput,
    ToolServices,
)
from app.copilot.loop import (
    MAX_CALLS_PER_ROUND,
    MAX_TOOL_FAILURES,
    MAX_TOOL_ROUNDS,
    LoopResult,
    ToolLoop,
)
from app.copilot.registry import (
    ToolRegistrationError,
    ToolRegistry,
    UnknownToolError,
    build_default_registry,
    property_names,
)
from app.copilot.tools import (
    daily_series,
    demand_forecast,
    forecast_accuracy,
    hotel_kpis,
    revenue_breakdown,
)
from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    InternalFaultError,
    NotFoundError,
    ValidationError,
)
from app.llm.base import (
    Budget,
    ChatRequest,
    ChatResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from app.llm.boundary import GuardedChatModel
from app.llm.circuit import (
    FAILURE_THRESHOLD,
    OPEN_SECONDS,
    WINDOW_SECONDS,
    CircuitBreaker,
    CircuitPolicy,
)
from app.llm.errors import (
    DECLARED_FAILURES,
    LlmBudgetExhaustedError,
    LlmCircuitOpenError,
    LlmDisabledError,
    LlmError,
    LlmInvalidResponseError,
    LlmRateLimitedError,
    LlmToolFailedError,
    LlmUnavailableError,
)
from app.llm.factory import breaker_for, build_chat_model
from app.llm.providers.anthropic_provider import AnthropicChatModel
from app.llm.testing import FailingModel, ScriptedModel, ScriptedTurn, SlowModel
from app.models.enums import (
    SAFE_AUDIT_DETAIL_KEYS,
    AuditAction,
    AuditResourceType,
    HotelRole,
)
from app.schemas.analytics import DailySeriesResponse, OverviewResponse, RevenueBreakdownResponse
from app.schemas.ml_performance import ForecastAccuracyResponse
from app.schemas.ml_serving import DemandPredictionResponse
from app.services import tool_invocation as invocation_module
from app.services.tool_invocation import ToolInvocationService, arguments_sha256

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP = REPOSITORY_ROOT / "backend" / "app"
COPILOT = APP / "copilot"
ARCHITECTURE = REPOSITORY_ROOT / "docs" / "v2-architecture.md"

HOTEL = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_HOTEL = uuid.UUID("22222222-2222-2222-2222-222222222222")

#: The five, and the order everything is reported in.
EXPECTED_TOOLS = (
    "get_daily_series",
    "get_demand_forecast",
    "get_forecast_accuracy",
    "get_hotel_kpis",
    "get_revenue_breakdown",
)

#: Every spelling of "which hotel" the brief names, and a few it implies.
TENANT_FIELDS = (
    "hotel_id",
    "hotel_public_id",
    "hotel_uuid",
    "hotel",
    "hotel_name",
    "tenant_id",
    "tenant",
    "tenant_name",
    "property_id",
    "property",
    "organisation_id",
    "organization_id",
    "user_id",
    "id",
)


class Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def budget() -> Budget:
    return Budget(timeout_seconds=5.0, max_output_tokens=100)


def request(*, tools: tuple[ToolSpec, ...] = ()) -> ChatRequest:
    return ChatRequest(
        prompt_id="probe",
        prompt_version="v1",
        messages=(Message(role="user", content="question"),),
        budget=budget(),
        tools=tools,
    )


def guard(inner: Any, **kwargs: Any) -> GuardedChatModel:
    defaults: dict[str, Any] = {"sleep": lambda _s: None, "jitter": lambda _a, _b: 0.0}
    return GuardedChatModel(inner, **{**defaults, **kwargs})


def call(name: str, arguments: Mapping[str, Any] | None = None, *, id: str = "c1") -> ToolCall:
    return ToolCall(id=id, name=name, arguments=dict(arguments or {}))


def ok(name: str = "get_hotel_kpis") -> ToolOutcome:
    return ToolOutcome(tool=name, outcome="succeeded", output={"value": 1})


def failed(code: str = "VALIDATION_ERROR") -> ToolOutcome:
    return ToolOutcome(
        tool="get_hotel_kpis", outcome="failed", error_code=code, error_message="bad range"
    )


def source_of(path: Path) -> str:
    """Source with docstrings removed, so prose explaining a rule cannot trip it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


# ======================================================================================
# A. The circuit breaker (§5.8, Amendment A1)
# ======================================================================================


def test_the_documented_numbers_are_the_implemented_numbers() -> None:
    """The amendment and the code say the same thing, and a test holds them together."""
    assert (FAILURE_THRESHOLD, WINDOW_SECONDS, OPEN_SECONDS) == (5, 60.0, 30.0)
    assert CircuitPolicy() == CircuitPolicy(5, 60.0, 30.0)

    document = ARCHITECTURE.read_text(encoding="utf-8")
    section = document[document.index("### 5.8") :]
    assert "Amendment A1" in section
    for phrase in ["5 availability failures", "60 seconds", "30 seconds", "LLM_UNAVAILABLE"]:
        assert phrase in section, phrase


def test_a_new_breaker_is_closed() -> None:
    assert CircuitBreaker(monotonic=Clock()).state == "closed"


def test_it_opens_at_the_threshold_within_the_window() -> None:
    clock = Clock()
    breaker = CircuitBreaker(monotonic=clock)
    for _ in range(FAILURE_THRESHOLD - 1):
        breaker.record_failure(breaker.acquire())
        clock.advance(1)
    assert breaker.state == "closed"

    breaker.record_failure(breaker.acquire())
    assert breaker.state == "open"


def test_failures_older_than_the_window_do_not_count() -> None:
    clock = Clock()
    breaker = CircuitBreaker(monotonic=clock)
    for _ in range(FAILURE_THRESHOLD - 1):
        breaker.record_failure(breaker.acquire())
    clock.advance(WINDOW_SECONDS + 1)

    breaker.record_failure(breaker.acquire())

    assert breaker.state == "closed"


def test_an_open_breaker_refuses_with_the_public_unavailable_code() -> None:
    clock = Clock()
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1), monotonic=clock)
    breaker.record_failure(breaker.acquire())

    with pytest.raises(LlmCircuitOpenError) as refused:
        breaker.acquire()

    assert isinstance(refused.value, LlmUnavailableError)
    assert refused.value.code == "LLM_UNAVAILABLE"
    assert refused.value.status_code == 503
    # Not a seventh declared failure: the taxonomy is unchanged.
    assert len(DECLARED_FAILURES) == 6
    assert LlmCircuitOpenError not in DECLARED_FAILURES.values()


def test_it_half_opens_after_the_open_duration_and_admits_one_probe() -> None:
    clock = Clock()
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1), monotonic=clock)
    breaker.record_failure(breaker.acquire())
    clock.advance(OPEN_SECONDS - 0.001)
    assert breaker.state == "open"

    clock.advance(0.001)
    assert breaker.state == "half_open"
    assert breaker.acquire() is True
    with pytest.raises(LlmCircuitOpenError):
        breaker.acquire()


def test_a_successful_probe_closes_it() -> None:
    clock = Clock()
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1), monotonic=clock)
    breaker.record_failure(breaker.acquire())
    clock.advance(OPEN_SECONDS)

    breaker.record_success(breaker.acquire())

    assert breaker.state == "closed"
    assert breaker.acquire() is False


def test_a_failed_probe_reopens_it_for_a_full_period() -> None:
    clock = Clock()
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1), monotonic=clock)
    breaker.record_failure(breaker.acquire())
    clock.advance(OPEN_SECONDS)

    breaker.record_failure(breaker.acquire())
    clock.advance(OPEN_SECONDS - 1)

    assert breaker.state == "open"


def test_a_probe_that_ends_neutrally_frees_the_slot_and_stays_half_open() -> None:
    clock = Clock()
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1), monotonic=clock)
    breaker.record_failure(breaker.acquire())
    clock.advance(OPEN_SECONDS)

    breaker.release(breaker.acquire())

    assert breaker.state == "half_open"
    assert breaker.acquire() is True


def test_a_success_in_closed_does_not_erase_the_window() -> None:
    clock = Clock()
    breaker = CircuitBreaker(monotonic=clock)
    for _ in range(FAILURE_THRESHOLD - 1):
        breaker.record_failure(breaker.acquire())
    breaker.record_success(breaker.acquire())

    breaker.record_failure(breaker.acquire())

    assert breaker.state == "open"


def test_a_late_non_probe_outcome_cannot_close_an_open_breaker() -> None:
    """A call that began while CLOSED says nothing about recovery since."""
    clock = Clock()
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1), monotonic=clock)
    straggler = breaker.acquire()
    breaker.record_failure(breaker.acquire())

    breaker.record_success(straggler)

    assert breaker.state == "open"


@pytest.mark.parametrize(
    ("threshold", "window", "opening"), [(0, 60.0, 30.0), (1, 0.0, 30.0), (1, 60.0, -1.0)]
)
def test_a_policy_must_be_positive(threshold: int, window: float, opening: float) -> None:
    with pytest.raises(ValueError):
        CircuitPolicy(threshold, window, opening)


def test_the_guard_counts_only_availability_failures() -> None:
    clock = Clock()
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1), monotonic=clock)
    for error in (LlmRateLimitedError, LlmBudgetExhaustedError):
        with pytest.raises(error):
            guard(FailingModel(error), breaker=breaker).complete(request())
    with pytest.raises(LlmInvalidResponseError):
        guard(FailingModel(LlmInvalidResponseError), breaker=breaker).complete(request())

    assert breaker.state == "closed"


def test_the_guard_opens_on_unavailability_and_then_never_reaches_the_provider() -> None:
    """Security test M: an OPEN breaker prevents new model calls."""
    breaker = CircuitBreaker(monotonic=Clock())
    failing = FailingModel(LlmUnavailableError)
    for _ in range(FAILURE_THRESHOLD):
        with pytest.raises(LlmUnavailableError):
            guard(failing, breaker=breaker).complete(request())
    assert breaker.state == "open"
    reached = len(failing.calls)

    healthy = ScriptedModel("fine")
    with pytest.raises(LlmCircuitOpenError):
        guard(healthy, breaker=breaker).complete(request())
    with pytest.raises(LlmCircuitOpenError):
        guard(failing, breaker=breaker).complete(request())

    assert healthy.calls == []
    assert len(failing.calls) == reached


def test_one_timed_out_call_is_one_failure_despite_its_retry() -> None:
    """The boundary makes two attempts; the breaker sees one call that ended unavailable."""
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=2), monotonic=Clock())
    slow = SlowModel(block_seconds=5.0)
    try:
        with pytest.raises(LlmUnavailableError):
            guard(slow, breaker=breaker).complete(
                ChatRequest(
                    prompt_id="p",
                    prompt_version="v1",
                    messages=(Message(role="user", content="q"),),
                    budget=Budget(timeout_seconds=0.05, max_output_tokens=10),
                )
            )
    finally:
        slow.release()

    assert len(slow.calls) == 2
    assert breaker.state == "closed"


def test_a_disabled_or_over_budget_request_is_refused_for_its_own_reason_first() -> None:
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1), monotonic=Clock())
    breaker.record_failure(breaker.acquire())

    with pytest.raises(LlmDisabledError):
        guard(ScriptedModel(), enabled=False, breaker=breaker).complete(request())
    with pytest.raises(LlmBudgetExhaustedError):
        guard(ScriptedModel(), max_output_tokens=10, breaker=breaker).complete(request())


def test_the_factory_shares_one_breaker_per_upstream() -> None:
    assert breaker_for("anthropic", "model-a") is breaker_for("anthropic", "model-a")
    assert breaker_for("anthropic", "model-a") is not breaker_for("anthropic", "model-b")

    settings = Settings(environment="test", llm_enabled=True, llm_api_key="not-a-real-key")
    first = cast(GuardedChatModel, build_chat_model(settings))
    second = cast(GuardedChatModel, build_chat_model(settings))
    assert first._breaker is second._breaker is breaker_for("anthropic", settings.llm_model)


def test_a_disabled_deployment_gets_no_breaker() -> None:
    disabled = cast(GuardedChatModel, build_chat_model(Settings(environment="test")))
    assert disabled._breaker is None


def test_the_breaker_holds_no_request_and_no_tenant() -> None:
    """Counts and times only, so it cannot carry anything between callers."""
    source = source_of(APP / "llm" / "circuit.py")
    for forbidden in ["ChatRequest", "Message", "hotel", "user", "prompt"]:
        assert forbidden not in source, forbidden


# ======================================================================================
# B. The seam's tool types
# ======================================================================================


def test_a_tool_spec_is_exactly_name_description_and_schema() -> None:
    assert [f.name for f in fields(ToolSpec)] == ["name", "description", "input_schema"]
    assert [f.name for f in fields(ToolCall)] == ["id", "name", "arguments"]


def test_a_tool_message_must_name_its_call() -> None:
    with pytest.raises(ValueError, match="must name the call"):
        Message(role="tool", content="x")
    with pytest.raises(ValueError, match="Only a tool message"):
        Message(role="user", content="x", is_error=True)
    with pytest.raises(ValueError, match="Only an assistant"):
        Message(role="user", content="x", tool_calls=(call("t"),))


def test_a_request_may_not_offer_a_tool_twice() -> None:
    spec = ToolSpec(name="t", description="d", input_schema={})
    with pytest.raises(ValueError, match="same tool twice"):
        request(tools=(spec, spec))


def test_the_boundary_preserves_tool_calls_and_does_not_validate_a_tool_turn() -> None:
    class Answer(BaseModel):
        value: int

    asked = (call("get_hotel_kpis"),)
    model = ScriptedModel([ScriptedTurn(tool_calls=asked)])
    structured = ChatRequest(
        prompt_id="p",
        prompt_version="v1",
        messages=(Message(role="user", content="q"),),
        budget=budget(),
        response_schema=Answer,
    )

    response = guard(model).complete(structured)

    assert response.tool_calls == asked
    assert response.finish_reason == "tool_use"
    assert response.parsed is None


# ======================================================================================
# C. The adapter's tool translation
# ======================================================================================


class Block:
    def __init__(self, **attributes: Any) -> None:
        self.__dict__.update(attributes)


class Usage:
    input_tokens = 3
    output_tokens = 2


class FakeMessage:
    def __init__(self, blocks: list[Block], stop_reason: str = "end_turn") -> None:
        self.content = blocks
        self.stop_reason = stop_reason
        self.usage = Usage()
        self.model = "recorded"


class FakeClient:
    def __init__(self, reply: FakeMessage) -> None:
        self.sent: list[dict[str, Any]] = []
        self.messages = self
        self._reply = reply

    def create(self, **kwargs: Any) -> FakeMessage:
        self.sent.append(kwargs)
        return self._reply


def adapter(reply: FakeMessage) -> tuple[AnthropicChatModel, FakeClient]:
    client = FakeClient(reply)
    return AnthropicChatModel(api_key="k", model="m", client=client), client


def test_no_tools_parameter_is_sent_when_none_are_offered() -> None:
    model, client = adapter(FakeMessage([Block(type="text", text="hi")]))
    model.complete(request())
    assert "tools" not in client.sent[0]


def test_the_catalogue_is_sent_in_the_vendor_shape() -> None:
    spec = ToolSpec(name="get_x", description="d", input_schema={"type": "object"})
    model, client = adapter(FakeMessage([Block(type="text", text="hi")]))

    model.complete(request(tools=(spec,)))

    assert client.sent[0]["tools"] == [
        {"name": "get_x", "description": "d", "input_schema": {"type": "object"}}
    ]


def test_a_tool_exchange_is_replayed_in_the_vendor_shape() -> None:
    asked = (call("a", {"x": 1}, id="c1"), call("b", {}, id="c2"))
    replay = ChatRequest(
        prompt_id="p",
        prompt_version="v1",
        messages=(
            Message(role="system", content="sys"),
            Message(role="user", content="q"),
            Message(role="assistant", content="let me look", tool_calls=asked),
            Message(role="tool", content='{"ok":1}', tool_call_id="c1"),
            Message(role="tool", content='{"error":{}}', tool_call_id="c2", is_error=True),
        ),
        budget=budget(),
    )
    model, client = adapter(FakeMessage([Block(type="text", text="done")]))

    model.complete(replay)

    turns = client.sent[0]["messages"]
    assert [turn["role"] for turn in turns] == ["user", "assistant", "user"]
    assert turns[1]["content"] == [
        {"type": "text", "text": "let me look"},
        {"type": "tool_use", "id": "c1", "name": "a", "input": {"x": 1}},
        {"type": "tool_use", "id": "c2", "name": "b", "input": {}},
    ]
    # Every result for one assistant turn arrives together, in one user turn.
    assert turns[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": '{"ok":1}', "is_error": False},
        {"type": "tool_result", "tool_use_id": "c2", "content": '{"error":{}}', "is_error": True},
    ]


def test_tool_use_blocks_become_untrusted_tool_calls() -> None:
    reply = FakeMessage(
        [Block(type="tool_use", id="t1", name="get_hotel_kpis", input={"date_from": "x"})],
        stop_reason="tool_use",
    )
    model, _ = adapter(reply)

    response = model.complete(request())

    assert response.text == ""
    assert response.finish_reason == "tool_use"
    assert response.tool_calls == (call("get_hotel_kpis", {"date_from": "x"}, id="t1"),)


def test_a_call_cut_off_at_the_token_limit_is_an_invalid_response() -> None:
    reply = FakeMessage(
        [Block(type="tool_use", id="t1", name="x", input={})], stop_reason="max_tokens"
    )
    with pytest.raises(LlmInvalidResponseError):
        adapter(reply)[0].complete(request())


def test_a_call_without_an_id_is_an_invalid_response() -> None:
    reply = FakeMessage([Block(type="tool_use", name="x", input={})], stop_reason="tool_use")
    with pytest.raises(LlmInvalidResponseError):
        adapter(reply)[0].complete(request())


def test_non_object_arguments_become_empty_rather_than_crashing() -> None:
    reply = FakeMessage(
        [Block(type="tool_use", id="t1", name="x", input="not an object")], stop_reason="tool_use"
    )
    assert adapter(reply)[0].complete(request()).tool_calls[0].arguments == {}


# ======================================================================================
# D. The registry
# ======================================================================================


def test_exactly_the_five_tools_are_registered() -> None:
    """§7.2's six minus `search_hotel_knowledge`, deferred until its service exists (A1)."""
    registry = build_default_registry()

    assert registry.names() == EXPECTED_TOOLS
    assert "search_hotel_knowledge" not in registry


def test_every_tool_module_is_registered_and_nothing_else_is() -> None:
    modules = sorted(p.stem for p in (COPILOT / "tools").glob("*.py") if p.stem != "__init__")
    assert modules == [
        "daily_series",
        "demand_forecast",
        "forecast_accuracy",
        "hotel_kpis",
        "revenue_breakdown",
    ]
    tools = (daily_series, demand_forecast, forecast_accuracy, hotel_kpis, revenue_breakdown)
    declared = sorted(module.CONTRACT.name for module in tools)
    assert tuple(declared) == EXPECTED_TOOLS


def test_registration_is_deterministic() -> None:
    first, second = build_default_registry(), build_default_registry()
    assert first.names() == second.names()
    assert [c.input_schema() for c in first.contracts()] == [
        c.input_schema() for c in second.contracts()
    ]


@pytest.mark.parametrize(
    "name", ["nope", "", "GET_HOTEL_KPIS", "get_hotel_kpis ", "get_hotel_kpis\x00", "__class__"]
)
def test_an_unknown_name_fails_closed_without_echoing_it(name: str) -> None:
    """Security test C."""
    with pytest.raises(UnknownToolError) as refused:
        build_default_registry().get(name)
    # The refusal is a fixed sentence: untrusted text is never echoed into an error.
    assert str(refused.value) == "No tool is registered under the requested name."


def test_a_non_string_name_fails_closed() -> None:
    registry = build_default_registry()
    with pytest.raises(UnknownToolError):
        registry.get(cast(str, 42))
    assert cast(str, None) not in registry


class GoodArguments(ToolArguments):
    date_from: dt.date


class GoodOutput(ToolOutput):
    value: int


def contract(name: str = "probe_tool", **overrides: Any) -> ToolContract:
    values: dict[str, Any] = {
        "name": name,
        "description": "A probe.",
        "min_role": HotelRole.VIEWER,
        "input_model": GoodArguments,
        "output_model": GoodOutput,
        "delegates_to": "Nothing.nothing",
    }
    return ToolContract(**{**values, **overrides})


def noop(_context: ToolContext, _arguments: Any) -> ToolOutput:
    return GoodOutput(value=1)


def test_duplicate_registration_fails() -> None:
    """Security test D."""
    registry = ToolRegistry()
    registry.register(contract(), noop)

    with pytest.raises(ToolRegistrationError, match="already registered"):
        registry.register(contract(), noop)
    assert registry.names() == ("probe_tool",)


def test_the_default_tools_cannot_be_registered_twice() -> None:
    registry = build_default_registry()
    with pytest.raises(ToolRegistrationError):
        registry.register(hotel_kpis.CONTRACT, hotel_kpis.run)


@pytest.mark.parametrize("name", ["Bad", "a", "has space", "x" * 65, "1starts_digit", "dash-ed"])
def test_a_malformed_name_is_refused(name: str) -> None:
    with pytest.raises(ToolRegistrationError):
        ToolRegistry().register(contract(name), noop)


def test_an_input_model_that_tolerates_unknown_keys_is_refused() -> None:
    class Lax(BaseModel):
        model_config = ConfigDict(extra="ignore")
        date_from: dt.date

    with pytest.raises(ToolRegistrationError, match="forbid unknown keys"):
        ToolRegistry().register(contract(input_model=Lax), noop)


@pytest.mark.parametrize("field_name", TENANT_FIELDS)
def test_an_input_naming_a_tenant_is_refused_at_registration(field_name: str) -> None:
    """Security test A, structurally: such a tool cannot exist in a registry at all."""
    hostile = type(
        "Hostile",
        (ToolArguments,),
        {"__annotations__": {field_name: str}},
    )
    with pytest.raises(ToolRegistrationError, match="may take a tenant"):
        ToolRegistry().register(contract(input_model=hostile), noop)


def test_a_tenant_hidden_in_a_nested_input_model_is_refused() -> None:
    class Scope(ToolArguments):
        hotel_public_id: uuid.UUID

    class Wrapper(ToolArguments):
        scope: Scope

    with pytest.raises(ToolRegistrationError):
        ToolRegistry().register(contract(input_model=Wrapper), noop)


def test_an_output_naming_a_hotel_is_refused() -> None:
    class Leaky(ToolOutput):
        hotel_public_id: uuid.UUID

    with pytest.raises(ToolRegistrationError, match="returns no hotel"):
        ToolRegistry().register(contract(output_model=Leaky), noop)


def test_the_registry_cannot_dispatch_dynamically() -> None:
    """No path from a model's string to arbitrary code: a dict lookup and static imports only."""
    for module in (registry_module, catalogue_module, loop_module, invocation_module):
        source = source_of(Path(inspect.getfile(module)))
        for forbidden in ["importlib", "__import__", "getattr(", "eval(", "exec(", "globals("]:
            assert forbidden not in source, f"{module.__name__} uses {forbidden}"


# ======================================================================================
# E. The five tools
# ======================================================================================

TOOL_MODULES: dict[str, tuple[Any, type[BaseModel], HotelRole, str]] = {
    "get_hotel_kpis": (hotel_kpis, OverviewResponse, HotelRole.VIEWER, "none"),
    "get_daily_series": (daily_series, DailySeriesResponse, HotelRole.VIEWER, "none"),
    "get_revenue_breakdown": (
        revenue_breakdown,
        RevenueBreakdownResponse,
        HotelRole.VIEWER,
        "none",
    ),
    "get_demand_forecast": (
        demand_forecast,
        DemandPredictionResponse,
        HotelRole.VIEWER,
        "records_served_prediction",
    ),
    "get_forecast_accuracy": (
        forecast_accuracy,
        ForecastAccuracyResponse,
        HotelRole.MANAGER,
        "none",
    ),
}

REQUIRED_ARGUMENTS = {
    "get_hotel_kpis": ["date_from", "date_to"],
    "get_daily_series": ["date_from", "date_to"],
    "get_revenue_breakdown": ["date_from", "date_to"],
    "get_demand_forecast": ["target_date"],
    "get_forecast_accuracy": ["as_of_date", "window_from", "window_to"],
}


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_each_contract_declares_its_role_and_side_effect(name: str) -> None:
    module, _, role, side_effect = TOOL_MODULES[name]
    assert module.CONTRACT.min_role == role
    assert module.CONTRACT.side_effect == side_effect


def test_only_the_forecast_tool_declares_a_side_effect() -> None:
    """The Stage 7.6 decision: no business write; the one declared side effect only."""
    declared = {c.name for c in build_default_registry().contracts() if c.side_effect != "none"}
    assert declared == {"get_demand_forecast"}


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_each_output_is_the_service_response_minus_the_hotel(name: str) -> None:
    module, response, _, _ = TOOL_MODULES[name]
    assert set(module.CONTRACT.output_model.model_fields) == (
        set(response.model_fields) - {HOTEL_IDENTIFIER_FIELD}
    )
    assert HOTEL_IDENTIFIER_FIELD in module.CONTRACT.withheld


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_each_input_has_exactly_its_required_arguments(name: str) -> None:
    schema = TOOL_MODULES[name][0].CONTRACT.input_schema()
    assert schema["additionalProperties"] is False
    assert sorted(schema["properties"]) == sorted(REQUIRED_ARGUMENTS[name])
    assert sorted(schema["required"]) == sorted(REQUIRED_ARGUMENTS[name])


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
@pytest.mark.parametrize("forbidden", ["hotel_id", "hotel_public_id", "tenant_id", "property_id"])
def test_no_tool_schema_contains_a_tenant_identifier(name: str, forbidden: str) -> None:
    """Security test A, over the real tools, input AND output, at every depth."""
    contract_ = TOOL_MODULES[name][0].CONTRACT
    for schema in (contract_.input_schema(), contract_.output_model.model_json_schema()):
        assert forbidden not in set(property_names(schema))
        assert forbidden not in json.dumps(schema)


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_each_tool_delegates_to_exactly_its_declared_method(name: str) -> None:
    module = TOOL_MODULES[name][0]
    method = module.CONTRACT.delegates_to.split(".")[-1]
    source = source_of(Path(inspect.getfile(module)))
    assert f".{method}(" in source
    assert source.count("context.services.") == 1


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_a_tool_adds_no_business_logic_sql_or_model_call(name: str) -> None:
    path = Path(inspect.getfile(TOOL_MODULES[name][0]))
    for imported in imports_of(path):
        for forbidden in ["sqlalchemy", "app.repositories", "app.db", "app.llm", "app.ml"]:
            assert not imported.startswith(forbidden), f"{path.name} imports {imported}"
    source = source_of(path)
    for forbidden in ["commit", "session", "execute(", "select(", "sum(", "mean(", "round("]:
        assert forbidden not in source, f"{path.name} contains {forbidden!r}"


def test_the_forecast_tool_does_not_let_the_model_choose_a_horizon() -> None:
    assert "horizon" not in demand_forecast.CONTRACT.input_model.model_fields
    assert "SERVED_HORIZON_DAYS" in source_of(Path(inspect.getfile(demand_forecast)))


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_no_description_speaks_about_authorization(name: str) -> None:
    """Authorization is code. Nothing the model reads claims or grants access."""
    text = TOOL_MODULES[name][0].CONTRACT.description.lower()
    for word in ["authori", "permission", "role", "admin", "allowed", "access", "ignore"]:
        assert word not in text, word


class RecordingServices:
    """Stands in for the three services and records what each tool asked of them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, method: str) -> Callable[..., Any]:
        def record(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((method, args, kwargs))
            raise LookupError("recorded")

        return record


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_a_tool_passes_the_context_hotel_and_never_an_argument_hotel(name: str) -> None:
    """Security test B, at the tool: the hotel argument is the context's, always."""
    module = TOOL_MODULES[name][0]
    recorder = RecordingServices()
    services = cast(ToolServices, _Bundle(recorder))
    arguments = {
        "get_hotel_kpis": {"date_from": "2026-01-01", "date_to": "2026-01-31"},
        "get_daily_series": {"date_from": "2026-01-01", "date_to": "2026-01-31"},
        "get_revenue_breakdown": {"date_from": "2026-01-01", "date_to": "2026-01-31"},
        "get_demand_forecast": {"target_date": "2026-06-01"},
        "get_forecast_accuracy": {
            "as_of_date": "2026-06-01",
            "window_from": "2026-01-01",
            "window_to": "2026-03-31",
        },
    }[name]
    parsed = module.CONTRACT.input_model.model_validate(arguments)

    with pytest.raises(LookupError):
        module.run(ToolContext(hotel_public_id=HOTEL, services=services), parsed)

    [(method, args, _)] = recorder.calls
    assert method == module.CONTRACT.delegates_to.split(".")[-1]
    assert args[0] == HOTEL


class _Bundle:
    def __init__(self, recorder: RecordingServices) -> None:
        self.analytics = recorder
        self.demand_prediction = recorder
        self.forecast_performance = recorder


# ======================================================================================
# F. The catalogue
# ======================================================================================


def test_the_catalogue_is_deterministic_and_sorted() -> None:
    """Security test J."""
    registry = build_default_registry()
    first = build_catalogue(registry, reversed(registry.names()))
    second = build_catalogue(build_default_registry(), registry.names())

    assert [spec.name for spec in first] == list(EXPECTED_TOOLS)
    assert json.dumps([vars_of(s) for s in first], sort_keys=False) == json.dumps(
        [vars_of(s) for s in second], sort_keys=False
    )


def vars_of(spec: ToolSpec) -> dict[str, Any]:
    return {"name": spec.name, "description": spec.description, "schema": spec.input_schema}


def test_the_catalogue_carries_no_authorization_or_tenant_information() -> None:
    serialised = json.dumps(
        [vars_of(s) for s in build_catalogue(build_default_registry(), EXPECTED_TOOLS)]
    ).lower()
    for forbidden in [
        "hotel_id",
        "hotel_public_id",
        "tenant",
        "property_id",
        "min_role",
        "viewer",
        "manager",
        "owner",
        "delegates_to",
        "side_effect",
        "select ",
        "sql",
    ]:
        assert forbidden not in serialised, forbidden


def test_the_catalogue_includes_only_what_was_permitted() -> None:
    specs = build_catalogue(build_default_registry(), ["get_hotel_kpis", "get_hotel_kpis"])
    assert [s.name for s in specs] == ["get_hotel_kpis"]


def test_the_catalogue_refuses_a_name_it_does_not_hold() -> None:
    with pytest.raises(UnknownToolError):
        build_catalogue(build_default_registry(), ["get_hotel_kpis", "drop_tables"])


# ======================================================================================
# G. The loop
# ======================================================================================


class Executor:
    """Returns scripted outcomes in order and records every call it was handed."""

    def __init__(self, *outcomes: ToolOutcome) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[ToolCall] = []

    def __call__(self, tool_call: ToolCall) -> ToolOutcome:
        self.calls.append(tool_call)
        return self._outcomes.pop(0) if self._outcomes else ok(tool_call.name)


SPECS = build_catalogue(build_default_registry(), EXPECTED_TOOLS)


def run(model: Any, execute: Executor, **bounds: int) -> LoopResult:
    return ToolLoop(model, **bounds).run(
        prompt_id="probe",
        prompt_version="v1",
        messages=(Message(role="user", content="How did we do in January?"),),
        budget=budget(),
        tools=SPECS,
        execute=execute,
    )


def turn(*names: str) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=tuple(call(n, {}, id=f"id-{i}-{n}") for i, n in enumerate(names))
    )


def test_the_documented_bounds() -> None:
    assert (MAX_TOOL_ROUNDS, MAX_CALLS_PER_ROUND, MAX_TOOL_FAILURES) == (3, 4, 2)


def test_1_an_answer_without_tools_completes() -> None:
    model = ScriptedModel(["the answer"])
    result = run(model, Executor())

    assert result.complete and result.stop_reason == "completed"
    assert result.text == "the answer"
    assert (result.rounds, result.model_calls, result.outcomes) == (0, 1, ())
    assert model.calls[0].tools == SPECS


def test_2_a_successful_tool_then_completion() -> None:
    model = ScriptedModel([turn("get_hotel_kpis"), "occupancy was 71%"])
    execute = Executor(ok())

    result = run(model, execute)

    assert result.complete and result.text == "occupancy was 71%"
    assert (result.rounds, result.model_calls) == (1, 2)
    second = model.calls[1].messages
    assert second[-2].role == "assistant" and second[-2].tool_calls[0].name == "get_hotel_kpis"
    assert second[-1].role == "tool" and not second[-1].is_error
    assert json.loads(second[-1].content) == {"value": 1}


def test_3_a_first_failure_is_returned_to_the_model_flagged_and_it_may_recover() -> None:
    model = ScriptedModel([turn("get_hotel_kpis"), turn("get_hotel_kpis"), "recovered"])
    result = run(model, Executor(failed(), ok()))

    assert result.complete and result.failures == 1
    error_turn = model.calls[1].messages[-1]
    assert error_turn.role == "tool" and error_turn.is_error
    assert json.loads(error_turn.content) == {
        "error": {"code": "VALIDATION_ERROR", "message": "bad range"}
    }


def test_4_a_second_failure_ends_the_loop_immediately() -> None:
    """Security test L, and §5.7 row 4."""
    model = ScriptedModel([turn("get_hotel_kpis"), turn("a", "b", "c")])
    execute = Executor(failed(), failed(), ok())

    result = run(model, execute)

    assert not result.complete and result.stop_reason == "tool_failed"
    # The second round's calls b and c were not run, and the model was not asked again.
    assert [c.name for c in execute.calls] == ["get_hotel_kpis", "a"]
    assert result.model_calls == 2
    assert [o.succeeded for o in result.outcomes] == [False, False]


def test_two_failures_in_one_round_also_end_it() -> None:
    model = ScriptedModel([turn("a", "b", "c")])
    execute = Executor(failed(), failed())

    result = run(model, execute)

    assert result.stop_reason == "tool_failed"
    assert len(execute.calls) == 2


def test_a_tool_error_never_becomes_a_success() -> None:
    model = ScriptedModel([turn("get_hotel_kpis"), "answer"])
    result = run(model, Executor(failed()))

    assert result.outcomes[0].succeeded is False
    assert model.calls[1].messages[-1].is_error is True
    assert result.failures == 1


def test_5_the_round_limit_stops_with_a_labelled_partial_result() -> None:
    """Security test K: bounded. At most rounds + 1 model calls."""
    model = ScriptedModel([turn("get_hotel_kpis")] * (MAX_TOOL_ROUNDS + 1))
    execute = Executor()

    result = run(model, execute)

    assert not result.complete and result.stop_reason == "max_rounds"
    assert result.rounds == MAX_TOOL_ROUNDS
    assert result.model_calls == MAX_TOOL_ROUNDS + 1
    assert len(execute.calls) == MAX_TOOL_ROUNDS


def test_6_the_per_round_cap_stops_before_running_any_call() -> None:
    model = ScriptedModel([turn(*["get_hotel_kpis"] * (MAX_CALLS_PER_ROUND + 1))])
    execute = Executor()

    result = run(model, execute)

    assert result.stop_reason == "tool_call_cap"
    assert execute.calls == []


def test_exactly_the_cap_is_allowed() -> None:
    model = ScriptedModel([turn(*["get_hotel_kpis"] * MAX_CALLS_PER_ROUND), "done"])
    execute = Executor()

    assert run(model, execute).complete
    assert len(execute.calls) == MAX_CALLS_PER_ROUND


@pytest.mark.parametrize(
    ("model", "code"),
    [
        (FailingModel(LlmUnavailableError), "LLM_UNAVAILABLE"),
        (FailingModel(LlmBudgetExhaustedError), "LLM_BUDGET_EXHAUSTED"),
        (FailingModel(LlmRateLimitedError), "LLM_RATE_LIMITED"),
        (FailingModel(LlmInvalidResponseError), "LLM_INVALID_RESPONSE"),
        (guard(ScriptedModel(), enabled=False), "LLM_DISABLED"),
    ],
)
def test_7_to_9_a_declared_model_failure_is_a_labelled_hard_stop(model: Any, code: str) -> None:
    result = run(model, Executor())

    assert not result.complete and result.stop_reason == "model_failed"
    assert result.model_error is not None and result.model_error.code == code
    assert result.model_calls == 1


def test_10_an_open_breaker_is_a_labelled_hard_stop_and_the_model_is_never_called() -> None:
    """Security test M, through the loop."""
    breaker = CircuitBreaker(CircuitPolicy(failure_threshold=1), monotonic=Clock())
    breaker.record_failure(breaker.acquire())
    inner = ScriptedModel("never")

    result = run(guard(inner, breaker=breaker), Executor())

    assert result.stop_reason == "model_failed"
    assert isinstance(result.model_error, LlmCircuitOpenError)
    assert inner.calls == []


def test_a_model_failure_after_a_tool_round_keeps_the_outcomes() -> None:
    class ThenFail:
        def __init__(self) -> None:
            self.inner = ScriptedModel([turn("get_hotel_kpis")])
            self.count = 0

        def complete(self, request: ChatRequest) -> ChatResponse:
            self.count += 1
            if self.count > 1:
                raise LlmUnavailableError()
            return self.inner.complete(request)

    result = run(ThenFail(), Executor(ok()))

    assert result.stop_reason == "model_failed"
    assert [o.succeeded for o in result.outcomes] == [True]


def test_an_undeclared_exception_is_not_swallowed() -> None:
    class Broken:
        def complete(self, request: ChatRequest) -> ChatResponse:
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        run(Broken(), Executor())


def test_the_loop_is_structurally_bounded() -> None:
    """No `while`, no recursion: the bound is the `for` over a range."""
    tree = ast.parse((COPILOT / "loop.py").read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
    run_method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    called = {
        node.func.attr
        for node in ast.walk(run_method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "run" not in called
    assert "for _ in range(self._max_rounds + 1)" in ast.unparse(run_method)


@pytest.mark.parametrize(
    "bounds",
    [{"max_rounds": -1}, {"max_calls_per_round": 0}, {"max_failures": 0}],
)
def test_the_bounds_must_be_sane(bounds: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ToolLoop(ScriptedModel(), **bounds)


def test_the_loop_never_holds_a_hotel() -> None:
    source = source_of(COPILOT / "loop.py")
    for forbidden in ["hotel", "HotelScopeResolver", "Session", "AuditTrail"]:
        assert forbidden not in source


def test_the_tool_failure_error_stays_declared_and_unraised() -> None:
    """FM4 is a labelled partial result; nothing raises LLM_TOOL_FAILED (see errors.py)."""
    assert DECLARED_FAILURES["LLM_TOOL_FAILED"] is LlmToolFailedError
    for path in [*COPILOT.rglob("*.py"), APP / "services" / "tool_invocation.py"]:
        assert "LlmToolFailedError" not in source_of(path)


# ======================================================================================
# H. The invocation service, against fakes
# ======================================================================================

ROLE_ORDER = [HotelRole.VIEWER, HotelRole.STAFF, HotelRole.MANAGER, HotelRole.OWNER]


class FakeHotel:
    id = 41


class FakeScope:
    def __init__(self, role: HotelRole | None) -> None:
        self.role = role
        self.log: list[str] = []

    def require_hotel(self, hotel_public_id: uuid.UUID) -> FakeHotel:
        self.log.append(f"hotel:{hotel_public_id}")
        if self.role is None or hotel_public_id != HOTEL:
            raise NotFoundError()
        return FakeHotel()

    def require_hotel_with_role(self, hotel_public_id: uuid.UUID, required: HotelRole) -> FakeHotel:
        self.log.append(f"role:{required.value}")
        hotel = self.require_hotel(hotel_public_id)
        assert self.role is not None
        if ROLE_ORDER.index(self.role) < ROLE_ORDER.index(required):
            raise ForbiddenError()
        return hotel


class _Diag:
    def __init__(self, table_name: str) -> None:
        self.table_name = table_name
        self.constraint_name = f"fk_{table_name}_something"


class _DriverError(Exception):
    def __init__(self, table_name: str) -> None:
        super().__init__("boom")
        self.diag = _Diag(table_name)
        self.sqlstate = "23503"


def integrity_error(table_name: str) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, _DriverError(table_name))


class FakeAudit:
    def __init__(self, *, fail: bool = False, raises: Exception | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail = fail
        self.raises = raises

    def record(
        self,
        action: AuditAction,
        resource_type: AuditResourceType,
        resource_reference: str,
        *,
        hotel_id: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("audit store unavailable")
        if self.raises is not None:
            raise self.raises
        unapproved = set(details or {}) - SAFE_AUDIT_DETAIL_KEYS
        assert not unapproved, unapproved
        self.events.append(
            {
                "action": action,
                "resource_type": resource_type,
                "reference": resource_reference,
                "hotel_id": hotel_id,
                "details": dict(details or {}),
            }
        )


class FakeSession:
    def __init__(self) -> None:
        self.log: list[str] = []

    def commit(self) -> None:
        self.log.append("commit")

    def rollback(self) -> None:
        self.log.append("rollback")


class Harness:
    def __init__(
        self,
        role: HotelRole | None,
        *,
        audit_fails: bool = False,
        audit_raises: Exception | None = None,
    ) -> None:
        self.ran: list[str] = []
        self.scope = FakeScope(role)
        self.audit = FakeAudit(fail=audit_fails, raises=audit_raises)
        self.session = FakeSession()
        registry = ToolRegistry()
        registry.register(contract("viewer_tool"), self._runner("viewer_tool"))
        registry.register(
            contract("manager_tool", min_role=HotelRole.MANAGER), self._runner("manager_tool")
        )
        registry.register(contract("failing_tool"), self._raiser(ValidationError("bad range")))
        registry.register(
            contract("broken_tool"), self._raiser(RuntimeError("row: guest=Alice card=4242"))
        )
        self.service = ToolInvocationService(
            cast(Any, self.session),
            registry,
            cast(Any, self.scope),
            cast(Any, self.audit),
            cast(ToolServices, None),
            monotonic=iter([0.0, 0.25] * 50).__next__,
        )
        self.offered = frozenset(registry.names())

    def _runner(self, name: str) -> Callable[[ToolContext, Any], ToolOutput]:
        def run_tool(context: ToolContext, _arguments: Any) -> ToolOutput:
            self.ran.append(f"{name}@{context.hotel_public_id}")
            return GoodOutput(value=7)

        return run_tool

    def _raiser(self, error: Exception) -> Callable[[ToolContext, Any], ToolOutput]:
        def run_tool(_context: ToolContext, _arguments: Any) -> ToolOutput:
            self.ran.append("raised")
            raise error

        return run_tool

    def invoke(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolOutcome:
        return self.service.invoke(
            HOTEL,
            name,
            {"date_from": "2026-01-01"} if arguments is None else arguments,
            offered=self.offered,
        )


def test_a_permitted_call_runs_against_the_request_hotel_and_is_audited() -> None:
    harness = Harness(HotelRole.VIEWER)

    outcome = harness.invoke("viewer_tool")

    assert outcome.succeeded and outcome.output == {"value": 7}
    assert harness.ran == [f"viewer_tool@{HOTEL}"]
    [event] = harness.audit.events
    assert event["action"] is AuditAction.TOOL_INVOKED
    assert event["resource_type"] is AuditResourceType.TOOL
    assert event["reference"] == "viewer_tool"
    assert event["hotel_id"] == FakeHotel.id
    assert event["details"] == {
        "outcome": "succeeded",
        "error_code": None,
        "duration_ms": 250,
        "arguments_sha256": arguments_sha256({"date_from": "2026-01-01"}),
    }
    assert harness.session.log == ["commit"]


def test_authorization_happens_before_validation_and_before_execution() -> None:
    """Security tests E and F: a viewer never reaches a manager tool, whatever the arguments."""
    harness = Harness(HotelRole.VIEWER)

    for arguments in ({"date_from": "2026-01-01"}, {"garbage": True}, {"hotel_id": 2}):
        outcome = harness.invoke("manager_tool", arguments)
        assert outcome.outcome == "forbidden"
        assert outcome.error_code == "FORBIDDEN"

    assert harness.ran == []
    assert [e["details"]["outcome"] for e in harness.audit.events] == ["forbidden"] * 3


def test_a_manager_reaches_the_manager_tool() -> None:
    harness = Harness(HotelRole.MANAGER)
    assert harness.invoke("manager_tool").succeeded


def test_a_tool_failure_does_not_bypass_authorization() -> None:
    """Security test G: after failures of every kind, the role check still runs and refuses."""
    harness = Harness(HotelRole.VIEWER)
    harness.invoke("failing_tool")
    harness.invoke("broken_tool")
    harness.invoke("viewer_tool", {"hotel_public_id": str(OTHER_HOTEL)})

    outcome = harness.invoke("manager_tool")

    assert outcome.outcome == "forbidden"
    assert "manager_tool" not in " ".join(harness.ran)


def test_an_unknown_name_is_audited_without_storing_what_the_model_said() -> None:
    harness = Harness(HotelRole.OWNER)
    hostile = "drop_all_tables; hotel=22222222-2222-2222-2222-222222222222"

    outcome = harness.invoke(hostile)

    assert outcome.outcome == "unknown_tool" and outcome.tool is None
    assert outcome.error_code == "UNKNOWN_TOOL"
    [event] = harness.audit.events
    assert event["reference"] == "unknown"
    assert hostile not in json.dumps(event, default=str)
    assert "role:" not in " ".join(harness.scope.log)


def test_a_registered_tool_that_was_not_offered_is_unknown() -> None:
    harness = Harness(HotelRole.OWNER)
    outcome = harness.service.invoke(
        HOTEL, "manager_tool", {"date_from": "2026-01-01"}, offered=frozenset({"viewer_tool"})
    )
    assert outcome.outcome == "unknown_tool"
    assert harness.ran == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"date_from": "2026-01-01", "hotel_id": 2},
        {"date_from": "2026-01-01", "hotel_public_id": str(OTHER_HOTEL)},
        {"date_from": "2026-01-01", "tenant_id": "b"},
        {"date_from": "hotel 22222222-2222-2222-2222-222222222222"},
        {},
        cast(Any, ["not", "a", "mapping"]),
    ],
)
def test_arguments_that_try_to_name_a_hotel_are_refused_and_never_run(arguments: Any) -> None:
    """Security tests B and I at the service: invalid, audited, not executed."""
    harness = Harness(HotelRole.OWNER)

    outcome = harness.invoke("viewer_tool", arguments)

    assert outcome.outcome == "invalid_arguments"
    assert outcome.error_code == "INVALID_ARGUMENTS"
    assert harness.ran == []
    assert harness.audit.events[0]["hotel_id"] == FakeHotel.id


def test_a_typed_service_failure_is_returned_not_raised() -> None:
    harness = Harness(HotelRole.VIEWER)
    outcome = harness.invoke("failing_tool")

    assert outcome.outcome == "failed"
    assert (outcome.error_code, outcome.error_message) == ("VALIDATION_ERROR", "bad range")
    assert harness.session.log == ["rollback", "commit"]


def test_an_unexpected_error_becomes_the_generic_internal_error() -> None:
    harness = Harness(HotelRole.VIEWER)
    outcome = harness.invoke("broken_tool")

    assert outcome.outcome == "error" and outcome.error_code == "INTERNAL_ERROR"
    assert "Alice" not in (outcome.error_message or "")
    assert "Alice" not in outcome.model_content()
    assert "Alice" not in json.dumps(harness.audit.events, default=str)


def test_a_hotel_the_caller_cannot_see_is_a_404_and_is_not_audited() -> None:
    harness = Harness(None)
    with pytest.raises(NotFoundError):
        harness.invoke("viewer_tool")
    assert harness.audit.events == []
    assert harness.ran == []


def test_a_failure_to_audit_means_no_result_reaches_the_loop() -> None:
    harness = Harness(HotelRole.VIEWER, audit_fails=True)
    with pytest.raises(RuntimeError, match="audit store"):
        harness.invoke("viewer_tool")
    assert harness.session.log == ["rollback"]


def test_an_audit_integrity_failure_is_this_servers_fault_not_the_callers() -> None:
    """The Stage 4.5.16 guard every audit writer carries: 500, naming no relation."""
    harness = Harness(HotelRole.VIEWER, audit_raises=integrity_error("audit_events"))

    with pytest.raises(InternalFaultError) as raised:
        harness.invoke("viewer_tool")

    assert "audit_events" not in raised.value.message
    assert harness.session.log == ["rollback"]


def test_any_other_integrity_failure_is_the_generic_conflict() -> None:
    harness = Harness(HotelRole.VIEWER, audit_raises=integrity_error("bookings"))

    with pytest.raises(ConflictError) as raised:
        harness.invoke("viewer_tool")

    assert "bookings" not in raised.value.message


def test_the_permitted_tools_follow_the_role() -> None:
    viewer = Harness(HotelRole.VIEWER).service.permitted_tools(HOTEL)
    manager = Harness(HotelRole.MANAGER).service.permitted_tools(HOTEL)

    assert "manager_tool" not in viewer and "viewer_tool" in viewer
    assert "manager_tool" in manager
    assert list(viewer) == sorted(viewer)
    with pytest.raises(NotFoundError):
        Harness(None).service.permitted_tools(HOTEL)


def test_the_argument_fingerprint_is_canonical() -> None:
    assert arguments_sha256({"b": 1, "a": 2}) == arguments_sha256({"a": 2, "b": 1})
    assert arguments_sha256({"a": 1}) != arguments_sha256({"a": 2})
    assert re.fullmatch(r"[0-9a-f]{64}", arguments_sha256({"x": object()}))


def test_the_audit_details_carry_no_free_text() -> None:
    """Security test H at the call site: four keys, none of them content."""
    tree = ast.parse((APP / "services" / "tool_invocation.py").read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "details":
            assert isinstance(node.value, ast.Dict)
            keys |= {str(cast(ast.Constant, k).value) for k in node.value.keys}
    assert keys == {"outcome", "error_code", "duration_ms", "arguments_sha256"}
    for forbidden in ["question", "answer", "prompt", "content", "arguments", "output", "text"]:
        assert forbidden not in keys


def test_the_invocation_service_follows_the_service_layer_rules() -> None:
    source = source_of(APP / "services" / "tool_invocation.py")
    assert "ForbiddenError" not in source
    assert "exc_info" not in source and "logger.exception" not in source
    assert "commit()" in source and "rollback()" in source
    imported = imports_of(APP / "services" / "tool_invocation.py")
    for forbidden in ["app.llm", "app.repositories", "anthropic"]:
        assert not any(name.startswith(forbidden) for name in imported), forbidden


# ======================================================================================
# The package boundary
# ======================================================================================


def test_the_copilot_package_reaches_no_database_and_no_vendor() -> None:
    for path in COPILOT.rglob("*.py"):
        for imported in imports_of(path):
            for forbidden in [
                "sqlalchemy",
                "app.repositories",
                "app.db",
                "anthropic",
                "openai",
                "langchain",
                "httpx",
                "requests",
                "app.llm.providers",
                "app.llm.factory",
            ]:
                assert not imported.startswith(forbidden), f"{path.name} imports {imported}"


def test_no_route_reaches_the_tool_machinery_directly() -> None:
    """Stage 7.6 had no endpoint; Stage 7.7 added one, and it reaches tools only via services.

    Restated to what it protected: no ROUTER imports the registry, the loop or the invocation
    service. The composition root (`deps.py`) assembles them; the copilot route sees only
    `CopilotService`. The RAG and memory non-goals below are unchanged.
    """
    assert (APP / "services" / "copilot.py").exists()
    for path in (APP / "api" / "v1").rglob("*.py"):
        for imported in imports_of(path):
            assert not imported.startswith("app.copilot"), path.name
            assert not imported.startswith("app.services.tool_invocation"), path.name
    for word in ["embedding", "pgvector", "vector", "retriev", "conversation", "recommend"]:
        for path in COPILOT.rglob("*.py"):
            assert word not in source_of(path).lower(), (word, path.name)


def test_the_only_service_that_reaches_the_copilot_is_the_invocation_service() -> None:
    reaching = sorted(
        path.name
        for path in (APP / "services").glob("*.py")
        if any(i.startswith("app.copilot") for i in imports_of(path))
    )
    # Stage 7.7: the copilot service composes the catalogue, the loop and the figure check.
    assert reaching == ["copilot.py", "tool_invocation.py"]


def test_an_outcome_is_json_for_the_model_and_hides_nothing() -> None:
    assert json.loads(ok().model_content()) == {"value": 1}
    assert json.loads(failed().model_content())["error"]["code"] == "VALIDATION_ERROR"


def test_the_date_range_base_forbids_extras() -> None:
    with pytest.raises(Exception):  # noqa: B017 -- pydantic's ValidationError, any flavour
        DateRangeArguments.model_validate(
            {"date_from": "2026-01-01", "date_to": "2026-01-02", "hotel_id": 1}
        )


def test_an_app_error_is_what_a_tool_may_raise_to_mean_failed() -> None:
    assert issubclass(ValidationError, AppError)
    assert isinstance(LlmUnavailableError(), LlmError)
