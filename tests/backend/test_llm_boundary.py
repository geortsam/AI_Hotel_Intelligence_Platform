"""Stage 7.5: the language-model seam, its failure taxonomy, and the walls around it.

No network, no credential, no database, no endpoint. Everything below runs against the doubles in
`app.llm.testing` or against a stand-in with the vendor SDK's shape, and the vendor SDK is not
installed in this environment at all — which is itself one of the things asserted here.

What this module is responsible for:

* **the contract** — one method, the declared request and response shapes, and the exact field
  sets, so a seventh field cannot arrive on `ChatRequest` without someone choosing it;
* **the six failures of architecture §5.7** — each with its documented status, code and retry
  behaviour, and no seventh invented;
* **the boundary's rules** — the deadline is enforced rather than requested, the retry happens
  exactly once, a rate limit is never retried, a budget refusal is a refusal;
* **prompt identity** — id, version, checksum, strict rendering, and the rule that a prompt is
  not an authorization mechanism;
* **the walls** — no vendor SDK above the adapter, no session, no SQL, no tenant identifier, no
  credential in anything a caller can see.

The arithmetic-free parts of this stage are most of it: there is no model here, nothing is
measured, and no claim is made about what any provider would actually answer.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import importlib
import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import AppError
from app.llm.base import (
    Budget,
    ChatModel,
    ChatRequest,
    ChatResponse,
    TokenUsage,
)
from app.llm.boundary import MAX_ATTEMPTS, GuardedChatModel
from app.llm.errors import (
    DECLARED_FAILURES,
    LlmBudgetExhaustedError,
    LlmDisabledError,
    LlmError,
    LlmInvalidResponseError,
    LlmRateLimitedError,
    LlmToolFailedError,
    LlmUnavailableError,
)
from app.llm.factory import build_chat_model
from app.llm.prompts import REGISTRY, get_prompt
from app.llm.prompts.registry import BOUNDARY_PROBE_V1, PromptRecord
from app.llm.providers.anthropic_provider import PROVIDER_NAME, AnthropicChatModel
from app.llm.testing import (
    FailingModel,
    RecordedModel,
    ScriptedModel,
    SlowModel,
    response_for,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP = REPOSITORY_ROOT / "backend" / "app"
LLM_PACKAGE = APP / "llm"
ADAPTER = LLM_PACKAGE / "providers" / "anthropic_provider.py"

#: The one module allowed to name a vendor SDK, relative to `backend/`.
PERMITTED_SDK_MODULE = "app/llm/providers/anthropic_provider.py"

#: Vendor SDKs no module outside that one may import.
VENDOR_SDKS = ("anthropic", "openai", "cohere", "google.generativeai", "mistralai", "langchain")


def prompt() -> PromptRecord:
    return BOUNDARY_PROBE_V1


def request_for(
    *,
    timeout_seconds: float = 5.0,
    max_output_tokens: int = 100,
    schema: type[BaseModel] | None = None,
) -> ChatRequest:
    record = prompt()
    return ChatRequest(
        prompt_id=record.prompt_id,
        prompt_version=record.version,
        messages=record.render(token="ping"),
        budget=Budget(timeout_seconds=timeout_seconds, max_output_tokens=max_output_tokens),
        response_schema=schema,
    )


def guard(inner: ChatModel, **kwargs: object) -> GuardedChatModel:
    """A boundary whose sleep and jitter are inert, so no test waits for a retry."""
    defaults: dict[str, object] = {
        "sleep": lambda _seconds: None,
        "jitter": lambda _low, _high: 0.0,
    }
    return GuardedChatModel(inner, **{**defaults, **kwargs})  # type: ignore[arg-type]


# ======================================================================================
# A. The protocol
# ======================================================================================


def test_the_protocol_has_exactly_one_method() -> None:
    """A seam with two methods is a seam every adapter has to implement twice."""
    declared = [
        name
        for name in vars(ChatModel)
        if not name.startswith("_") and callable(getattr(ChatModel, name, None))
    ]

    assert declared == ["complete"]


@pytest.mark.parametrize(
    "double", [ScriptedModel(), RecordedModel({}), FailingModel(LlmUnavailableError), SlowModel()]
)
def test_every_double_satisfies_the_protocol(double: object) -> None:
    assert isinstance(double, ChatModel)


def test_the_adapter_satisfies_the_protocol() -> None:
    assert isinstance(AnthropicChatModel(api_key="x", model="m"), ChatModel)


def test_the_guard_is_itself_a_chat_model() -> None:
    """So it composes, and nothing downstream knows it is wrapped."""
    assert isinstance(guard(ScriptedModel()), ChatModel)


def test_the_request_carries_exactly_these_fields() -> None:
    """Exhaustive, so a session, a hotel id or a raw row cannot arrive without a decision."""
    assert set(ChatRequest.__dataclass_fields__) == {
        "prompt_id",
        "prompt_version",
        "messages",
        "budget",
        "response_schema",
        # Stage 7.6: section 5.1's tool catalogue, a tuple of name/description/schema specs.
        "tools",
    }


def test_the_response_carries_exactly_these_fields() -> None:
    assert set(ChatResponse.__dataclass_fields__) == {
        "text",
        "parsed",
        "usage",
        "latency_ms",
        "provider",
        "model",
        "finish_reason",
        "prompt_id",
        "prompt_version",
        "attempts",
        "completed_at",
        "diagnostics",
        # Stage 7.6: the tool calls the model asked for. Untrusted model output.
        "tool_calls",
    }


@pytest.mark.parametrize("forbidden", ["provider", "model", "api_key", "hotel", "tenant", "user"])
def test_no_request_field_names_a_vendor_or_a_tenant(forbidden: str) -> None:
    """§5.1: the request carries no provider, model or key -- and no tenant, ever."""
    for field in ChatRequest.__dataclass_fields__:
        assert forbidden not in field


def test_a_request_must_carry_a_message() -> None:
    with pytest.raises(ValueError, match="at least one message"):
        ChatRequest(
            prompt_id="p",
            prompt_version="v1",
            messages=(),
            budget=Budget(timeout_seconds=1, max_output_tokens=1),
        )


@pytest.mark.parametrize(("timeout", "tokens"), [(0, 10), (-1, 10), (5, 0), (5, -3)])
def test_a_budget_must_be_positive(timeout: float, tokens: int) -> None:
    """A zero deadline is not "no deadline" and a zero ceiling is not "unlimited"."""
    with pytest.raises(ValueError, match="greater than zero"):
        Budget(timeout_seconds=timeout, max_output_tokens=tokens)


# ======================================================================================
# B. The six declared failures
# ======================================================================================


def test_exactly_six_failures_are_declared() -> None:
    """§5.7 declares six. A seventh would be a stage improvising, which the section forbids."""
    assert len(DECLARED_FAILURES) == 6


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("LLM_DISABLED", 503),
        ("LLM_UNAVAILABLE", 503),
        ("LLM_RATE_LIMITED", 429),
        ("LLM_INVALID_RESPONSE", 502),
        ("LLM_BUDGET_EXHAUSTED", 429),
    ],
)
def test_each_failure_carries_its_documented_status(code: str, status_code: int) -> None:
    """The five §5.7 gives a status to. The sixth is a loop rule -- see below."""
    error = DECLARED_FAILURES[code]

    assert error.code == code
    assert error.status_code == status_code


def test_the_tool_failure_is_declared_but_has_no_documented_status() -> None:
    """§5.7 row 4 gives a loop behaviour, not a status, and this stage introduces no tools.

    Asserted so that the gap is visible in the suite rather than only in a docstring: the stage
    that adds tools has to decide what a tool failure returns, and finding this test is how it
    learns that the decision has not been made.
    """
    assert "LLM_TOOL_FAILED" in DECLARED_FAILURES
    assert DECLARED_FAILURES["LLM_TOOL_FAILED"] is LlmToolFailedError


def test_every_failure_is_an_app_error() -> None:
    """So the existing handler turns each into the one ErrorResponse envelope, unchanged."""
    for error in DECLARED_FAILURES.values():
        assert issubclass(error, AppError)
        assert issubclass(error, LlmError)


@pytest.mark.parametrize("error", list(DECLARED_FAILURES.values()))
def test_no_failure_message_leaks_a_vendor_a_prompt_or_a_key(error: type[LlmError]) -> None:
    """§5.7: the response "leaks no provider detail, no prompt and no stack"."""
    message = error().message.lower()

    for leak in ["anthropic", "openai", "claude", "gpt", "api key", "token=", "http", "traceback"]:
        assert leak not in message


# ======================================================================================
# C. The boundary's behaviour
# ======================================================================================


def test_a_disabled_boundary_refuses_before_reaching_the_provider() -> None:
    """Row 6, and nothing is spent: not a call, not a thread."""
    inner = ScriptedModel()

    with pytest.raises(LlmDisabledError):
        guard(inner, enabled=False).complete(request_for())

    assert inner.calls == []


def test_a_successful_call_returns_the_provider_answer() -> None:
    response = guard(ScriptedModel("hello")).complete(request_for())

    assert response.text == "hello"
    assert response.attempts == 1
    assert response.finish_reason == "stop"


def test_the_prompt_identity_is_carried_onto_the_answer() -> None:
    """§5.3: a stored answer references the prompt version that produced it."""
    response = guard(ScriptedModel()).complete(request_for())

    assert response.prompt_id == BOUNDARY_PROBE_V1.prompt_id
    assert response.prompt_version == BOUNDARY_PROBE_V1.version


def test_a_timeout_is_retried_exactly_once_and_then_refused() -> None:
    """Row 1. The deadline is enforced HERE, against a model that genuinely does not return."""
    slow = SlowModel()

    try:
        with pytest.raises(LlmUnavailableError):
            guard(slow).complete(request_for(timeout_seconds=0.05))
    finally:
        slow.release()

    assert len(slow.calls) == MAX_ATTEMPTS == 2


def test_a_slow_call_that_beats_the_retry_still_answers() -> None:
    """The retry is a second chance, not a second failure: a provider that recovers is served."""
    slow = SlowModel()
    slow.release()

    response = guard(slow).complete(request_for(timeout_seconds=5))

    assert response.text == "ok"
    assert len(slow.calls) == 1


def test_a_rate_limit_is_never_retried() -> None:
    """Row 2, explicitly: retrying a refusal is how a brief one becomes a longer one."""
    failing = FailingModel(LlmRateLimitedError)

    with pytest.raises(LlmRateLimitedError):
        guard(failing).complete(request_for())

    assert len(failing.calls) == 1


def test_a_provider_failure_that_resolves_on_the_second_attempt_is_not_visible() -> None:
    """A retryable failure that succeeds second time is an answer, and says it took two."""
    slow = SlowModel(block_seconds=0.2)

    response = guard(slow, sleep=lambda _s: slow.release(), jitter=lambda _a, _b: 0.0).complete(
        request_for(timeout_seconds=0.05)
    )

    assert response.attempts == 2
    assert response.text == "ok"


# --- structured output ---------------------------------------------------------------------


class Answer(BaseModel):
    sentiment: str
    confidence: float


def test_structured_output_is_validated_and_returned_parsed() -> None:
    payload = json.dumps({"sentiment": "positive", "confidence": 0.5})

    response = guard(ScriptedModel(payload)).complete(request_for(schema=Answer))

    assert isinstance(response.parsed, Answer)
    assert response.parsed.sentiment == "positive"


def test_malformed_structured_output_is_retried_once_then_refused() -> None:
    """Row 3. §5.5: "a validation failure is a failure, not a coerced guess"."""
    inner = ScriptedModel(["not json", "still not json"])

    with pytest.raises(LlmInvalidResponseError):
        guard(inner).complete(request_for(schema=Answer))

    assert len(inner.calls) == MAX_ATTEMPTS


def test_structured_output_that_validates_on_the_retry_is_accepted() -> None:
    good = json.dumps({"sentiment": "neutral", "confidence": 0.1})
    inner = ScriptedModel(["{", good])

    response = guard(inner).complete(request_for(schema=Answer))

    assert response.parsed is not None
    assert response.attempts == 2


def test_a_text_request_parses_nothing() -> None:
    """No schema means no validation and no parsed object -- not an empty one."""
    response = guard(ScriptedModel("plain")).complete(request_for())

    assert response.parsed is None


# --- budget --------------------------------------------------------------------------------


def test_a_request_over_the_deployment_ceiling_is_refused_before_the_call() -> None:
    """Row 5, before spending. §4.5: a refusal with a code, never a silent degradation."""
    inner = ScriptedModel()

    with pytest.raises(LlmBudgetExhaustedError):
        guard(inner, max_output_tokens=50).complete(request_for(max_output_tokens=500))

    assert inner.calls == []


def test_a_response_that_overspent_its_request_budget_is_refused() -> None:
    """An answer already paid for is still an answer that broke the ceiling."""
    inner = ScriptedModel(output_tokens=999)

    with pytest.raises(LlmBudgetExhaustedError):
        guard(inner).complete(request_for(max_output_tokens=10))


def test_a_response_within_budget_is_served() -> None:
    response = guard(ScriptedModel(output_tokens=5), max_output_tokens=100).complete(
        request_for(max_output_tokens=50)
    )

    assert response.usage.output_tokens == 5


def test_no_ceiling_configured_means_only_the_request_budget_applies() -> None:
    response = guard(ScriptedModel(output_tokens=5)).complete(request_for(max_output_tokens=10))

    assert response.usage == TokenUsage(input_tokens=10, output_tokens=5)


# --- determinism ----------------------------------------------------------------------------


def test_two_identical_calls_differ_only_in_measured_duration() -> None:
    """Nothing here reads a wall clock that a caller can observe."""
    first = guard(ScriptedModel("same"), monotonic=lambda: 0.0).complete(request_for())
    second = guard(ScriptedModel("same"), monotonic=lambda: 0.0).complete(request_for())

    assert first == second


def test_the_completion_time_is_injected_rather_than_read() -> None:
    stamped = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.UTC)

    response = guard(ScriptedModel(), now=lambda: stamped).complete(request_for())

    assert response.completed_at == stamped


def test_no_clock_is_read_on_the_boundary_path() -> None:
    """Every notion of "when" is injected, so a test can pin it and two runs compare."""
    source = (LLM_PACKAGE / "boundary.py").read_text(encoding="utf-8")
    stripped = re.sub(r'"""[\s\S]*?"""', "", source)

    for forbidden in [".now(", ".today(", ".utcnow("]:
        assert forbidden not in stripped, forbidden


# ======================================================================================
# D. Prompts
# ======================================================================================


def test_a_prompt_has_an_identity_a_version_and_a_checksum() -> None:
    record = prompt()

    assert record.identity == "boundary_probe@v1"
    assert record.version == "v1"
    assert re.fullmatch(r"[0-9a-f]{64}", record.checksum)


def test_the_checksum_is_reproducible_by_hand() -> None:
    """The same shape `accuracy_v1` uses: compact JSON, sorted keys, UTF-8, SHA-256.

    Recomputed here rather than compared to a literal, so the test states the algorithm instead
    of pinning a value someone would update by pasting whatever the code now produces.
    """
    record = prompt()
    canonical = json.dumps(
        record.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=list
    )

    assert record.checksum == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_changing_any_field_changes_the_checksum() -> None:
    """Otherwise the checksum is decoration rather than evidence."""
    record = prompt()
    moved = [
        PromptRecord(
            prompt_id="other",
            version=record.version,
            system=record.system,
            template=record.template,
            variables=record.variables,
        ),
        PromptRecord(
            prompt_id=record.prompt_id,
            version="v2",
            system=record.system,
            template=record.template,
            variables=record.variables,
        ),
        PromptRecord(
            prompt_id=record.prompt_id,
            version=record.version,
            system=record.system + " Be brief.",
            template=record.template,
            variables=record.variables,
        ),
    ]

    checksums = {candidate.checksum for candidate in moved}
    assert record.checksum not in checksums
    assert len(checksums) == len(moved)


def test_a_prompt_is_frozen() -> None:
    """A record editable after construction has a checksum describing its past."""
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        prompt().version = "v2"  # type: ignore[misc]


def test_rendering_fills_the_declared_variables() -> None:
    messages = prompt().render(token="abc")

    assert [m.role for m in messages] == ["system", "user"]
    assert "abc" in messages[1].content
    assert "{{" not in messages[1].content


def test_rendering_refuses_an_undeclared_variable() -> None:
    with pytest.raises(ValueError, match="unexpected variable"):
        prompt().render(token="a", hotel_id="7")


def test_rendering_refuses_a_missing_variable() -> None:
    """A silently empty slot is a prompt that says something nobody wrote."""
    with pytest.raises(ValueError, match="missing variable"):
        prompt().render()


def test_a_template_using_an_undeclared_variable_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="undeclared variable"):
        PromptRecord(prompt_id="x", version="v1", system="s", template="{{ a }}", variables=())


def test_a_template_declaring_an_unused_variable_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="unused variable"):
        PromptRecord(prompt_id="x", version="v1", system="s", template="none", variables=("a",))


@pytest.mark.parametrize(
    "wording",
    [
        "You are authorized to read this hotel's data.",
        "Only return data for hotel 7.",
        "You may only access the current tenant's bookings.",
    ],
)
def test_a_prompt_may_not_be_written_as_an_authorization_mechanism(wording: str) -> None:
    """Refused at construction, because the risk is a reader BELIEVING it does something."""
    with pytest.raises(ValueError, match="not an authorization mechanism"):
        PromptRecord(prompt_id="x", version="v1", system=wording, template="body", variables=())


def test_no_registered_prompt_claims_to_enforce_access() -> None:
    for record in REGISTRY.values():
        text = f"{record.system} {record.template}".lower()
        for phrase in ["authorized", "authorised", "permission", "you may only access"]:
            assert phrase not in text


def test_no_registered_prompt_carries_a_tenant_or_a_credential() -> None:
    for record in REGISTRY.values():
        text = f"{record.system} {record.template}".lower()
        for forbidden in ["hotel_id", "api_key", "password", "token=", "select ", "user_id"]:
            assert forbidden not in text


def test_the_registry_resolves_by_identity() -> None:
    assert get_prompt("boundary_probe", "v1") is BOUNDARY_PROBE_V1


def test_an_unknown_prompt_raises_rather_than_returning_nothing() -> None:
    """A `None` here would be rendered as an empty prompt and sent."""
    with pytest.raises(KeyError, match="boundary_probe@v9"):
        get_prompt("boundary_probe", "v9")


def test_this_stage_registers_only_what_it_needs() -> None:
    """Stage 7.5 needed no product prompt; Stage 7.7 added exactly one, the copilot's, and
    Stage 7.10 its second version -- v1 kept beside it, unchanged -- and Stage 7.11 the
    conversation prompt."""
    assert set(REGISTRY) == {
        "boundary_probe@v1",
        "copilot_answer@v1",
        "copilot_answer@v2",
        "copilot_conversation@v1",
    }


# ======================================================================================
# E. The doubles
# ======================================================================================


def test_the_scripted_double_returns_what_it_was_given_in_order() -> None:
    inner = ScriptedModel(["one", "two"])

    assert inner.complete(request_for()).text == "one"
    assert inner.complete(request_for()).text == "two"


def test_the_scripted_double_repeats_a_single_answer() -> None:
    inner = ScriptedModel("always")

    assert [inner.complete(request_for()).text for _ in range(3)] == ["always"] * 3


def test_the_scripted_double_fails_loudly_when_over_called() -> None:
    """Running out is itself the finding: the code under test called more times than expected."""
    inner = ScriptedModel(["only one"])
    inner.complete(request_for())

    with pytest.raises(AssertionError, match="ran out of scripted answers"):
        inner.complete(request_for())


def test_the_recorded_double_replays_a_captured_exchange() -> None:
    rendered = prompt().render(token="ping")[1].content
    inner = RecordedModel({rendered: "recorded answer"})

    assert inner.complete(request_for()).text == "recorded answer"


def test_the_recorded_double_refuses_an_uncovered_request() -> None:
    """A default here would let an adapter test pass for something never recorded."""
    with pytest.raises(AssertionError, match="no recorded answer"):
        RecordedModel({}).complete(request_for())


@pytest.mark.parametrize("error", list(DECLARED_FAILURES.values()))
def test_the_failing_double_raises_any_declared_failure(error: type[LlmError]) -> None:
    with pytest.raises(error):
        FailingModel(error).complete(request_for())


def test_the_failing_double_can_recover_after_n_calls() -> None:
    inner = FailingModel(LlmUnavailableError, succeed_after=1)

    with pytest.raises(LlmUnavailableError):
        inner.complete(request_for())
    assert inner.complete(request_for()).text == "ok"


def test_the_slow_double_blocks_until_released() -> None:
    slow = SlowModel(block_seconds=0.05)

    # Not released: returns only once its own ceiling expires, which is what makes it "slow".
    assert slow.complete(request_for()).text == "ok"


def test_no_double_reports_a_real_vendor_name() -> None:
    """A fixture saying "anthropic" would eventually read as evidence of a real call."""
    response = response_for(request_for(), "x")

    assert response.provider == "double"
    assert response.provider != PROVIDER_NAME


# ======================================================================================
# F. The adapter, without the SDK and without a network
# ======================================================================================


class FakeUsage:
    input_tokens = 11
    output_tokens = 7


class FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessage:
    def __init__(self, text: str = "hi", stop_reason: str = "end_turn") -> None:
        self.content = [FakeBlock(text)]
        self.usage = FakeUsage()
        self.stop_reason = stop_reason
        self.model = "recorded-model"


class FakeMessages:
    def __init__(self, result: object) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    """A stand-in with the SDK's shape. The SDK itself is not installed in this environment."""

    def __init__(self, result: object) -> None:
        self.messages = FakeMessages(result)


def adapter(result: object) -> AnthropicChatModel:
    return AnthropicChatModel(api_key="secret-key-value", model="m", client=FakeClient(result))


def test_the_adapter_module_imports_without_the_sdk_installed() -> None:
    """The whole dependency-isolation claim, asserted rather than asserted about.

    `anthropic` is genuinely absent here — it is in `requirements-llm.txt`, which neither CI nor
    the image installs — so this import succeeding is the proof that the deferred import works.
    """
    with pytest.raises(ImportError):
        importlib.import_module("anthropic")

    module = importlib.import_module("app.llm.providers.anthropic_provider")
    assert module.PROVIDER_NAME == "anthropic"


def test_the_adapter_translates_a_successful_message() -> None:
    response = adapter(FakeMessage("the answer")).complete(request_for())

    assert response.text == "the answer"
    assert response.provider == "anthropic"
    assert response.usage == TokenUsage(input_tokens=11, output_tokens=7)
    assert response.finish_reason == "stop"


def test_the_adapter_sends_the_system_turn_as_a_parameter() -> None:
    """This API models system as a parameter, not as a message. Losing it would change the ask."""
    model = adapter(FakeMessage())
    model.complete(request_for())

    sent = model._client().messages.calls[0]
    assert sent["system"] == BOUNDARY_PROBE_V1.system
    assert [turn["role"] for turn in sent["messages"]] == ["user"]


def test_the_adapter_passes_the_budget_through() -> None:
    model = adapter(FakeMessage())
    model.complete(request_for(timeout_seconds=9, max_output_tokens=77))

    sent = model._client().messages.calls[0]
    assert sent["max_tokens"] == 77
    assert sent["timeout"] == 9


@pytest.mark.parametrize(
    ("vendor_error", "expected"),
    [
        ("RateLimitError", LlmRateLimitedError),
        ("APITimeoutError", LlmUnavailableError),
        ("APIConnectionError", LlmUnavailableError),
        ("InternalServerError", LlmUnavailableError),
        ("APIResponseValidationError", LlmInvalidResponseError),
        ("SomethingNobodyHasSeen", LlmUnavailableError),
    ],
)
def test_the_adapter_maps_each_vendor_failure_onto_the_taxonomy(
    vendor_error: str, expected: type[LlmError]
) -> None:
    """Matched by class name, so every branch is reachable with the SDK uninstalled."""
    failure = type(vendor_error, (Exception,), {})()

    with pytest.raises(expected):
        adapter(failure).complete(request_for())


def test_an_unknown_stop_reason_becomes_unknown_rather_than_a_guess() -> None:
    response = adapter(FakeMessage(stop_reason="some_new_reason")).complete(request_for())

    assert response.finish_reason == "unknown"


def test_a_message_with_no_text_block_is_an_invalid_response() -> None:
    """An empty string would be rendered as a blank answer by whatever displays it."""
    empty = FakeMessage()
    empty.content = []

    with pytest.raises(LlmInvalidResponseError):
        adapter(empty).complete(request_for())


def test_the_api_key_never_appears_in_an_answer_or_an_error() -> None:
    """The key is handed to the client and travels nowhere else."""
    answered = adapter(FakeMessage()).complete(request_for())
    assert "secret-key-value" not in repr(answered)

    with pytest.raises(LlmError) as raised:
        adapter(type("RateLimitError", (Exception,), {})()).complete(request_for())
    assert "secret-key-value" not in str(raised.value)
    assert "secret-key-value" not in repr(raised.value)


def test_a_missing_sdk_at_call_time_is_a_declared_failure_not_an_import_error() -> None:
    """A runtime without the dependency serves a 503, it does not crash the process."""
    with pytest.raises(LlmUnavailableError):
        AnthropicChatModel(api_key="k", model="m").complete(request_for())


# ======================================================================================
# G. The factory and settings
# ======================================================================================


def test_the_language_model_is_disabled_by_default() -> None:
    """A deployment that sets nothing spends nothing."""
    assert Settings(environment="test").llm_enabled is False


def test_a_disabled_deployment_still_gets_a_chat_model() -> None:
    """So no caller has to handle `None`, and the answer is a declared failure at call time."""
    model = build_chat_model(Settings(environment="test"))

    assert isinstance(model, ChatModel)
    with pytest.raises(LlmDisabledError):
        model.complete(request_for())


def test_enabling_without_a_key_is_refused_at_construction() -> None:
    """A misconfiguration, not a disabled deployment: fail now rather than at 3am."""
    with pytest.raises(ValueError, match="llm_api_key is not set"):
        build_chat_model(Settings(environment="test", llm_enabled=True))


def test_an_enabled_deployment_builds_a_guarded_provider() -> None:
    model = build_chat_model(
        Settings(environment="test", llm_enabled=True, llm_api_key="k", llm_model="m")
    )

    assert isinstance(model, GuardedChatModel)


def test_the_settings_never_default_to_a_credential() -> None:
    assert Settings(environment="test").llm_api_key is None


def test_the_v1_application_is_unaffected_by_the_flag() -> None:
    """Acceptance criterion 1: `llm_enabled=false` leaves the app fully functional.

    Asserted on the surface rather than by description: the OpenAPI document is byte-identical
    whether or not the flag is set, because Stage 7.5 adds no route either way.
    """
    from app.main import create_app

    off = create_app(Settings(environment="test", debug=True, llm_enabled=False)).openapi()
    on = create_app(
        Settings(environment="test", debug=True, llm_enabled=True, llm_api_key="k")
    ).openapi()

    assert off == on
    # Stage 7.7 added the copilot route. It exists whether or not the flag is set -- with
    # the flag off it answers 503 LLM_DISABLED -- which is exactly why the documents match.
    # Stage 7.9 added five knowledge paths, none of which depends on the flag either.
    # Stage 7.11 added three conversation paths: like `ask`, they exist with the flag off.
    assert len(off["paths"]) == 63


# ======================================================================================
# H. The walls
# ======================================================================================


def module_imports(path: Path) -> set[str]:
    """Every module named in an import statement, including deferred ones inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def python_files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in str(p))


def test_only_the_adapter_names_a_vendor_sdk() -> None:
    """The mechanical enforcement of §5.1, over the WHOLE application.

    Not a convention and not a review checklist: every module under `backend/app/` is parsed,
    and any import of a vendor SDK outside the one permitted adapter fails here.
    """
    offenders: list[str] = []
    for path in python_files_under(APP):
        relative = path.relative_to(APP.parent).as_posix()
        if relative == PERMITTED_SDK_MODULE:
            continue
        for imported in module_imports(path):
            if any(imported == sdk or imported.startswith(f"{sdk}.") for sdk in VENDOR_SDKS):
                offenders.append(f"{relative} imports {imported}")

    assert offenders == []


def test_the_permitted_module_is_the_one_that_actually_imports_the_sdk() -> None:
    """Guards the exemption above: were the adapter to stop importing it, this would be vacuous."""
    assert "anthropic" in module_imports(ADAPTER)


def test_the_sdk_import_is_deferred_rather_than_at_module_scope() -> None:
    """Which is what lets the module load in a runtime that does not carry the dependency."""
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    top_level = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }

    assert "anthropic" not in top_level


def test_only_the_copilot_service_reaches_the_llm_package() -> None:
    """Stage 7.5 wired nothing up; Stage 7.7 wired exactly one service, the copilot.

    Restated rather than deleted: the guard still fails for any OTHER service reaching the seam,
    and it pins what the copilot may reach -- the protocol and the prompt registry, never an
    adapter, the factory or a double.
    """
    services = python_files_under(APP / "services")
    assert len(services) > 20

    for path in services:
        reached = {i for i in module_imports(path) if i.startswith("app.llm")}
        if path.name == "copilot.py":
            assert reached == {"app.llm.base", "app.llm.prompts.registry"}, reached
        else:
            assert reached == set(), f"{path.name} imports {sorted(reached)}"


def test_no_router_reaches_the_llm_package() -> None:
    """No ROUTER reaches the seam. Stage 7.7's composition root may, and only three modules.

    `app/api/deps.py` builds the guarded model (factory), types it (base) and raises the budget
    refusal (errors). Every endpoint module -- the copilot's included -- imports none of it.
    """
    for path in python_files_under(APP / "api"):
        reached = {i for i in module_imports(path) if i.startswith("app.llm")}
        if path.name == "deps.py":
            assert reached == {"app.llm.base", "app.llm.errors", "app.llm.factory"}, reached
        else:
            assert reached == set(), f"{path.name} imports {sorted(reached)}"


@pytest.mark.parametrize(
    "forbidden",
    [
        "sqlalchemy",
        "app.db.session",
        "app.repositories",
        "app.models",
        "app.services",
        "psycopg",
        "httpx",
        "requests",
    ],
)
def test_the_llm_package_reaches_no_database_and_no_transport(forbidden: str) -> None:
    """§4: no session, no SQL, no row. Structural, not advisory.

    `httpx` and `requests` too: transport belongs to the vendor SDK inside the adapter, and a
    hand-rolled HTTP call anywhere in this package would be a second, unguarded provider path.
    """
    for path in python_files_under(LLM_PACKAGE):
        for imported in module_imports(path):
            assert not (imported == forbidden or imported.startswith(f"{forbidden}.")), (
                f"{path.name} imports {imported}"
            )


@pytest.mark.parametrize(
    "forbidden",
    ["hotel_id", "hotel_public_id", "tenant", "session.execute", "SELECT ", "INSERT ", "cursor"],
)
def test_no_module_in_the_package_names_a_tenant_or_a_query(forbidden: str) -> None:
    """Read over the executable text, with docstrings stripped: the prose explains these rules."""
    for path in python_files_under(LLM_PACKAGE):
        source = re.sub(r'"""[\s\S]*?"""', "", path.read_text(encoding="utf-8"))
        assert forbidden not in source, f"{path.name} names {forbidden!r}"


def test_the_package_defines_no_tool_no_retrieval_and_no_agent() -> None:
    """Stage 7.5's non-goals, asserted so a later stage's work cannot land here early."""
    for path in python_files_under(LLM_PACKAGE):
        source = re.sub(r'"""[\s\S]*?"""', "", path.read_text(encoding="utf-8"))
        for forbidden in ["embedding", "vector", "retriev", "def tool", "ToolRegistry", "Agent"]:
            assert forbidden.lower() not in source.lower(), f"{path.name} contains {forbidden!r}"


def test_nothing_in_the_package_reads_an_environment_variable_directly() -> None:
    """Configuration arrives as `Settings`; a second reader is a second source of truth."""
    for path in python_files_under(LLM_PACKAGE):
        source = re.sub(r'"""[\s\S]*?"""', "", path.read_text(encoding="utf-8"))
        assert "os.environ" not in source
        assert "getenv" not in source


# ======================================================================================
# I. Dependency isolation
# ======================================================================================


def test_the_provider_sdk_is_not_a_base_dependency() -> None:
    """The image installs `requirements.txt` and nothing else, so this is what keeps it out."""
    base = (REPOSITORY_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")

    for sdk in VENDOR_SDKS:
        assert sdk not in base.lower()


def test_the_provider_sdk_is_declared_and_pinned_in_its_own_file() -> None:
    optional = (REPOSITORY_ROOT / "backend" / "requirements-llm.txt").read_text(encoding="utf-8")
    pins = [
        line.strip()
        for line in optional.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert pins == ["anthropic==0.42.0"]


def test_the_image_installs_only_the_base_requirements() -> None:
    """Acceptance: the Docker image builds with no provider SDK and no credential."""
    dockerfile = (REPOSITORY_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "requirements-llm.txt" not in dockerfile
    for sdk in VENDOR_SDKS:
        assert sdk not in dockerfile.lower()
    for secret in ["LLM_API_KEY", "ANTHROPIC_API_KEY"]:
        assert secret not in dockerfile


def test_no_credential_is_committed_anywhere_in_the_package() -> None:
    for path in python_files_under(LLM_PACKAGE):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"sk-[A-Za-z0-9_-]{16,}", source), path.name
        assert "ANTHROPIC_API_KEY" not in source


def test_the_ml_requirements_claim_about_llm_clients_is_still_true() -> None:
    """`ml/requirements-ml.txt` says no LLM client is declared there. 7.5 did not change it."""
    ml = (REPOSITORY_ROOT / "ml" / "requirements-ml.txt").read_text(encoding="utf-8")
    pins = [line.strip() for line in ml.splitlines() if line.strip() and not line.startswith("#")]

    assert pins == ["scikit-learn==1.9.1"]


# ======================================================================================
# J. No network
# ======================================================================================


def test_no_test_in_this_module_can_reach_a_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.6: "no test in CI ever makes a network call". Asserted by removing the ability.

    Sockets are disabled for the duration, and the whole seam is exercised underneath: if any
    path here opened one, this test would fail rather than quietly succeeding on a machine that
    happens to be online.
    """
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test attempted to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    response = guard(ScriptedModel("offline")).complete(request_for())
    assert response.text == "offline"

    with pytest.raises(LlmUnavailableError):
        AnthropicChatModel(api_key="k", model="m").complete(request_for())

    assert adapter(FakeMessage("still offline")).complete(request_for()).text == "still offline"
