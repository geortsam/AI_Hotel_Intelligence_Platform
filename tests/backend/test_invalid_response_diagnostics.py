"""R2.8M: every invalid-response branch names its reason, and nothing it carries is content.

Every provider here is a fake: a transport returning fixed bytes for the Ollama adapter, the
SDK-shaped stand-in of `test_llm_boundary.py` for the Anthropic one. No server is contacted.
`MARKER` is planted wherever a model could put content -- text, tool arguments, a completion
reason -- and must never appear in a diagnostic, an error's string, or a log record.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from pydantic import BaseModel

from app.llm.boundary import GuardedChatModel
from app.llm.circuit import CircuitBreaker
from app.llm.errors import (
    DECLARED_FAILURES,
    INVALID_RESPONSE_REASONS,
    STRUCTURAL_REASONS,
    InvalidResponseDiagnostics,
    LlmInvalidResponseError,
    safe_completion_reason,
)
from app.llm.providers.ollama_provider import MAX_RESPONSE_BYTES, OllamaChatModel
from app.llm.testing import ScriptedModel
from tests.backend.test_copilot import raising_app
from tests.backend.test_llm_boundary import adapter, block, message_of, request_for

MARKER = "DIAG-CONTENT-MARKER-7f3a"
UNUSABLE = "The assistant returned an unusable answer."


class FixedTransport:
    """Returns the same bytes for every request, and counts the requests."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.posts = 0

    def post_json(self, url: str, body: bytes, timeout: float) -> bytes:
        self.posts += 1
        return self.raw


def ollama_body(**fields: Any) -> bytes:
    return json.dumps(fields).encode("utf-8")


def call(name: str = "get_demand_forecast", call_id: str | None = None, **function: Any) -> dict:
    entry: dict[str, Any] = {
        "function": {"name": name, "arguments": {"target_date": MARKER}, **function}
    }
    if call_id is not None:
        entry["id"] = call_id
    return entry


def expected(reason: str, **known: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "reason": reason,
        "provider": "ollama",
        "completion_reason": None,
        "output_tokens": None,
        "tool_calls_parsed": None,
        "has_tool_calls": None,
        "has_text": None,
    }
    fields.update(known)
    fields["structural"] = reason in STRUCTURAL_REASONS
    return fields


#: (id, raw body, what the diagnostics must say). The two R2.8J-like bodies are SYNTHETIC: the
#: real rejected answer was never recorded, and these do not claim to reconstruct it -- they are
#: the two shapes the adapter's rules allow for an answer cut off at 1,024 tokens.
OLLAMA_CASES: list[tuple[str, bytes, dict[str, Any]]] = [
    (
        "oversized",
        b"x" * (MAX_RESPONSE_BYTES + 1),
        expected("oversized_response"),
    ),
    ("not_json", b"{not json " + MARKER.encode(), expected("malformed_json")),
    ("not_an_object", b"[1, 2]", expected("malformed_structure")),
    (
        "no_message",
        ollama_body(done=True, done_reason="stop", eval_count=3),
        expected("malformed_structure", completion_reason="stop", output_tokens=3),
    ),
    (
        "not_done",
        ollama_body(done=False, done_reason="load", message={"content": MARKER}),
        expected("incomplete_response", completion_reason="load"),
    ),
    (
        "non_string_text",
        ollama_body(done=True, eval_count=4, message={"content": 5}),
        expected("non_string_text", output_tokens=4),
    ),
    (
        "synthetic_r28j_length_with_calls",
        ollama_body(
            done=True,
            done_reason="length",
            eval_count=1024,
            message={"content": "", "tool_calls": [call(), call(), call()]},
        ),
        expected(
            "length_with_tool_calls",
            completion_reason="length",
            output_tokens=1024,
            tool_calls_parsed=3,
            has_tool_calls=True,
            has_text=False,
        ),
    ),
    (
        "synthetic_r28j_empty",
        ollama_body(done=True, done_reason="length", eval_count=1024, message={"content": ""}),
        expected(
            "empty_response",
            completion_reason="length",
            output_tokens=1024,
            tool_calls_parsed=0,
            has_tool_calls=False,
            has_text=False,
        ),
    ),
    (
        "tool_calls_not_a_list",
        ollama_body(
            done=True, done_reason="stop", eval_count=5, message={"content": "", "tool_calls": {}}
        ),
        expected(
            "malformed_tool_calls",
            completion_reason="stop",
            output_tokens=5,
            has_tool_calls=True,
            has_text=False,
        ),
    ),
    (
        "entry_without_function",
        ollama_body(done=True, message={"content": MARKER, "tool_calls": [call(), {"x": 1}]}),
        expected("malformed_tool_calls", tool_calls_parsed=1, has_tool_calls=True, has_text=True),
    ),
    (
        "call_without_name",
        ollama_body(done=True, message={"content": "", "tool_calls": [call(name="")]}),
        expected("malformed_tool_calls", tool_calls_parsed=0, has_tool_calls=True, has_text=False),
    ),
    (
        "duplicate_ids",
        ollama_body(
            done=True,
            done_reason="stop",
            message={"content": "", "tool_calls": [call(call_id="a"), call(call_id="a")]},
        ),
        expected(
            "duplicate_tool_call_ids",
            completion_reason="stop",
            tool_calls_parsed=2,
            has_tool_calls=True,
            has_text=False,
        ),
    ),
    (
        "missing_optional_metadata",
        ollama_body(done=True, message={"content": ""}),
        expected("empty_response", tool_calls_parsed=0, has_tool_calls=False, has_text=False),
    ),
    (
        "unsafe_metadata_dropped",
        ollama_body(
            done=True,
            done_reason=f"Stopped: {MARKER}",
            eval_count="1024",
            message={"content": ""},
        ),
        expected("empty_response", tool_calls_parsed=0, has_tool_calls=False, has_text=False),
    ),
]


def assert_content_free(error: LlmInvalidResponseError) -> None:
    for rendered in (repr(error.diagnostics), str(error), json.dumps(error.diagnostics.as_dict())):
        assert MARKER not in rendered


def assert_unchanged_semantics(error: LlmInvalidResponseError) -> None:
    assert (error.code, error.message, error.status_code) == ("LLM_INVALID_RESPONSE", UNUSABLE, 502)
    assert DECLARED_FAILURES["LLM_INVALID_RESPONSE"] is LlmInvalidResponseError


# --- the Ollama adapter ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "diagnostics"),
    [(raw, diagnostics) for _, raw, diagnostics in OLLAMA_CASES],
    ids=[case_id for case_id, _, _ in OLLAMA_CASES],
)
def test_each_ollama_rejection_names_its_reason_and_what_the_body_safely_says(
    raw: bytes, diagnostics: dict[str, Any]
) -> None:
    model = OllamaChatModel(model="m", transport=FixedTransport(raw))

    with pytest.raises(LlmInvalidResponseError) as rejected:
        model.complete(request_for())

    assert rejected.value.diagnostics.as_dict() == diagnostics
    assert rejected.value.reason == diagnostics["reason"]
    assert_unchanged_semantics(rejected.value)
    assert_content_free(rejected.value)


def test_a_completion_reason_is_kept_only_when_it_is_a_short_identifier() -> None:
    for safe in ("stop", "length", "load", "max_tokens", "end_turn"):
        assert safe_completion_reason(safe) == safe
    for unsafe in (None, 7, "", "Stop", "stop now", "x" * 40, f"length {MARKER}"):
        assert safe_completion_reason(unsafe) is None


# --- the Anthropic adapter ------------------------------------------------------------------------


class APIResponseValidationError(Exception):
    """Named as the SDK names it; `_translate` matches on the class name."""


ANTHROPIC_CASES: list[tuple[str, object, dict[str, Any]]] = [
    (
        "length_with_tool_calls",
        message_of(
            block(type="tool_use", id="c1", name="x", input={"q": MARKER}),
            stop_reason="max_tokens",
        ),
        {
            "reason": "length_with_tool_calls",
            "completion_reason": "max_tokens",
            "output_tokens": 7,
            "tool_calls_parsed": 1,
            "has_tool_calls": True,
            "has_text": False,
        },
    ),
    (
        "tool_use_without_id",
        message_of(block(type="tool_use", name="x", input={"q": MARKER})),
        {
            "reason": "malformed_tool_calls",
            "completion_reason": "end_turn",
            "output_tokens": 7,
            "tool_calls_parsed": 0,
            "has_tool_calls": True,
            "has_text": None,
        },
    ),
    (
        "empty",
        message_of(stop_reason="max_tokens"),
        {
            "reason": "empty_response",
            "completion_reason": "max_tokens",
            "output_tokens": 7,
            "tool_calls_parsed": 0,
            "has_tool_calls": False,
            "has_text": False,
        },
    ),
    (
        "sdk_validation_error",
        APIResponseValidationError(MARKER),
        {
            "reason": "provider_response_validation",
            "completion_reason": None,
            "output_tokens": None,
            "tool_calls_parsed": None,
            "has_tool_calls": None,
            "has_text": None,
        },
    ),
]


@pytest.mark.parametrize(
    ("result", "known"),
    [(result, known) for _, result, known in ANTHROPIC_CASES],
    ids=[case_id for case_id, _, _ in ANTHROPIC_CASES],
)
def test_each_anthropic_rejection_names_its_reason(result: object, known: dict[str, Any]) -> None:
    with pytest.raises(LlmInvalidResponseError) as rejected:
        adapter(result).complete(request_for())

    diagnostics = rejected.value.diagnostics.as_dict()
    assert diagnostics == {
        **known,
        "provider": "anthropic",
        "structural": known["reason"] in STRUCTURAL_REASONS,
    }
    assert_unchanged_semantics(rejected.value)
    assert_content_free(rejected.value)


# --- the boundary: logged once, by reason; never retried; same failure ----------------------------


def boundary_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.llm.boundary"]


def test_an_adapter_rejection_passes_the_boundary_unretried_and_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, raw, diagnostics = next(
        c for c in OLLAMA_CASES if c[0] == "synthetic_r28j_length_with_calls"
    )
    transport = FixedTransport(raw)
    guarded = GuardedChatModel(OllamaChatModel(model="m", transport=transport))

    with (
        caplog.at_level(logging.INFO, logger="app.llm.boundary"),
        pytest.raises(LlmInvalidResponseError) as rejected,
    ):
        guarded.complete(request_for())

    assert transport.posts == 1  # no retry -- as before R2.8M
    assert rejected.value.diagnostics.as_dict() == diagnostics
    [record] = boundary_records(caplog)
    assert (record.outcome, record.reason, record.attempts) == (  # type: ignore[attr-defined]
        "invalid_response",
        "length_with_tool_calls",
        1,
    )
    for record in caplog.records:
        assert MARKER not in record.getMessage()
        assert MARKER not in repr(record.__dict__)


def test_rejected_answers_still_do_not_count_against_the_circuit() -> None:
    _, raw, _ = next(c for c in OLLAMA_CASES if c[0] == "synthetic_r28j_empty")
    transport = FixedTransport(raw)
    guarded = GuardedChatModel(
        OllamaChatModel(model="m", transport=transport), breaker=CircuitBreaker()
    )

    for _ in range(7):  # more than the breaker's failure threshold
        with pytest.raises(LlmInvalidResponseError):
            guarded.complete(request_for())

    assert transport.posts == 7  # the provider was reached every time: the breaker stayed closed


class Answer(BaseModel):
    value: int


def test_a_schema_failure_keeps_its_one_retry_and_names_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = ScriptedModel([f"not json {MARKER}", f"still not json {MARKER}"])
    guarded = GuardedChatModel(inner, sleep=lambda _: None)

    with (
        caplog.at_level(logging.INFO, logger="app.llm.boundary"),
        pytest.raises(LlmInvalidResponseError) as rejected,
    ):
        guarded.complete(request_for(schema=Answer))

    assert len(inner.calls) == 2  # one retry, unchanged
    assert rejected.value.diagnostics == InvalidResponseDiagnostics("schema_validation_failed")
    [record] = boundary_records(caplog)
    assert record.reason == "schema_validation_failed"  # type: ignore[attr-defined]
    assert MARKER not in repr(record.__dict__)


def test_other_boundary_events_log_exactly_what_they_did(
    caplog: pytest.LogCaptureFixture,
) -> None:
    guarded = GuardedChatModel(ScriptedModel(["fine"]))

    with caplog.at_level(logging.INFO, logger="app.llm.boundary"):
        guarded.complete(request_for())

    [record] = boundary_records(caplog)
    assert record.outcome == "answered"  # type: ignore[attr-defined]
    assert not hasattr(record, "reason")


# --- compatibility, coverage and the client's view -----------------------------------------------


def test_an_error_raised_without_diagnostics_is_other_invalid_response() -> None:
    error = LlmInvalidResponseError()
    assert error.reason == "other_invalid_response"
    assert_unchanged_semantics(error)
    assert LlmInvalidResponseError("Custom text.").message == "Custom text."


def test_every_reason_is_emitted_by_some_branch() -> None:
    emitted = {d["reason"] for _, _, d in OLLAMA_CASES} | {
        k["reason"] for _, _, k in ANTHROPIC_CASES
    }
    emitted |= {"schema_validation_failed", "other_invalid_response"}  # tested above
    assert emitted == set(INVALID_RESPONSE_REASONS)


def test_the_structural_reasons_are_exactly_the_rules_applied_to_a_readable_body() -> None:
    """R2.8N: an oversized body is refused before it is parsed, so it says nothing about shape."""
    expected = frozenset({"length_with_tool_calls", "empty_response", "duplicate_tool_call_ids"})
    assert expected == STRUCTURAL_REASONS
    assert InvalidResponseDiagnostics("oversized_response").structural is False
    assert STRUCTURAL_REASONS.issubset(INVALID_RESPONSE_REASONS)
    for reason in INVALID_RESPONSE_REASONS:
        assert InvalidResponseDiagnostics(reason).structural is (reason in STRUCTURAL_REASONS)


def test_the_client_never_sees_the_diagnostics() -> None:
    _, raw, _ = next(c for c in OLLAMA_CASES if c[0] == "synthetic_r28j_length_with_calls")
    with pytest.raises(LlmInvalidResponseError) as rejected:
        OllamaChatModel(model="m", transport=FixedTransport(raw)).complete(request_for())

    response = raising_app(rejected.value).get("/x")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "LLM_INVALID_RESPONSE"
    for hidden in ("diagnostics", "length_with_tool_calls", "completion_reason", "1024", MARKER):
        assert hidden not in response.text, hidden
