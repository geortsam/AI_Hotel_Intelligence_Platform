"""R2.7O: `kpis_may_occupancy` end to end through the REAL copilot stack, over a SCRIPTED Ollama.

The harness, `CopilotService`, the bounded `ToolLoop`, `ToolInvocationService` with its registry
and argument validation, the guarded boundary and the factory-built `OllamaChatModel` are all the
real ones. Only the adapter's transport is replaced: a script of `/api/chat` bodies, so no socket
is opened -- every socket is refused and recorded -- and no model is called.

What this proves is the WIRING: request shape, tool replay with `tool_name`, the deterministic
call ids, the argument path and the scoring. It says nothing about how `qwen3.5:4b` itself
chooses tools, states figures, cites or how fast it answers; those remain unverified until a
separately approved local run.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from app.core.config import Settings
from app.llm.base import Budget
from app.llm.boundary import GuardedChatModel
from app.llm.factory import build_chat_model
from app.llm.providers.ollama_provider import OllamaChatModel
from tests.evaluation import harness
from tests.evaluation.questions import COPILOT_EVAL_V1

MAY = {"date_from": "2026-05-01", "date_to": "2026-05-31"}
FINAL = (
    "From 1 May 2026 to 31 May 2026 occupancy was 70.00%: 434 of 620 available room nights "
    "were occupied."
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts: list[str] = []

    def refuse(*args: object, **kwargs: object) -> None:
        attempts.append(repr(args[:1]))
        raise AssertionError("a test attempted to open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield
    assert attempts == [], f"a test attempted to open a socket: {attempts}"


@pytest.fixture(autouse=True)
def no_ambient_llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_THINKING", "LLM_BASE_URL", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


class ScriptedOllama:
    """The local server, scripted: answers in order; keeps every decoded request body."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        self._replies = list(replies)
        self.sent: list[dict[str, Any]] = []

    def post_json(self, url: str, body: bytes, timeout: float) -> bytes:
        assert url == "http://127.0.0.1:11434/api/chat"
        self.sent.append(json.loads(body))
        return json.dumps(self._replies.pop(0)).encode("utf-8")


def reply(
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    *,
    done_reason: str = "stop",
    thinking: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if thinking is not None:
        message["thinking"] = thinking
    return {
        "model": "qwen3.5:4b",
        "message": message,
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": 1200,
        "eval_count": 40,
    }


def kpis(arguments: Any = None) -> dict[str, Any]:
    return {
        "function": {"name": "get_hotel_kpis", "arguments": MAY if arguments is None else arguments}
    }


def run_case(script: ScriptedOllama) -> dict[str, Any]:
    """The production construction path, then the scripted transport in place of urllib."""
    settings = Settings(_env_file=None, environment="test", llm_enabled=True)
    assert settings.llm_provider == "ollama"
    guarded = build_chat_model(settings)
    assert isinstance(guarded, GuardedChatModel)
    assert isinstance(guarded._inner, OllamaChatModel)
    guarded._inner._transport = script

    selected = replace(
        COPILOT_EVAL_V1,
        cases=tuple(c for c in COPILOT_EVAL_V1.cases if c.case_id == "kpis_may_occupancy"),
    )
    report = harness.run(
        selected,
        lambda _case: guarded,
        mode="live",
        provider=settings.llm_provider,
        model=settings.effective_llm_model,
        recorded_on="2026-10-22",
        budget=Budget(timeout_seconds=30.0, max_output_tokens=1024),
    )
    [case] = report["cases"]
    return dict(case)


def passed(case: dict[str, Any]) -> None:
    assert case["complete"] is True
    assert case["stop_reason"] == "completed"
    assert case["tools_called"] == ["get_hotel_kpis"]
    assert case["tool_selection"] is True
    assert case["expected_figures_present"] is True
    assert case["missing_figures"] == []
    assert case["grounded"] is True
    assert case["ungrounded_figures"] == []


# --- the scripted happy path ---------------------------------------------------------------------


def test_the_kpis_case_passes_and_the_tool_round_is_replayed_correctly() -> None:
    script = ScriptedOllama(reply(tool_calls=[kpis()]), reply(FINAL))

    case = run_case(script)

    passed(case)
    assert (case["input_tokens"], case["output_tokens"]) == (2400, 80)
    first, second = script.sent
    assert first["stream"] is False and first["think"] is False
    assert first["options"] == {
        "num_ctx": 16_384,
        "num_predict": 1024,
        "temperature": 0.0,
        "seed": 7151,
        "presence_penalty": 0,
    }
    assert "get_hotel_kpis" in [t["function"]["name"] for t in first["tools"]]
    assert [m["role"] for m in first["messages"]] == ["system", "user"]

    # The second request replays the round: the call as asked, then its result by tool name.
    system, user, assistant, tool = second["messages"]
    assert (system["role"], user["role"]) == ("system", "user")
    assert assistant == {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "get_hotel_kpis", "arguments": MAY}}],
    }
    assert tool["role"] == "tool" and tool["tool_name"] == "get_hotel_kpis"
    result = json.loads(tool["content"])
    assert "error" not in result
    assert "434" in tool["content"] and "0.7" in tool["content"]


def test_string_arguments_reach_the_tool_as_the_same_object() -> None:
    script = ScriptedOllama(reply(tool_calls=[kpis(json.dumps(MAY))]), reply(FINAL))

    passed(run_case(script))


def test_thinking_in_the_answers_never_becomes_answer_text() -> None:
    script = ScriptedOllama(
        reply(tool_calls=[kpis()], thinking="PRIVATE-PLAN"),
        reply(FINAL, thinking="PRIVATE-REASONING 99.9%"),
    )

    case = run_case(script)

    passed(case)  # 99.9% would have been an ungrounded figure had it reached the text


# --- negative variants -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"date_from": "May 2026", "date_to": "2026-05-31"},
        {"date_from": "2026-05-01"},
        {"date_from": "2026-05-01", "date_to": "2026-05-31", "hotel_id": "other"},
        '{"date_from": "2026-05-01"',
    ],
    ids=["unparseable-date", "missing-field", "extra-field", "broken-json"],
)
def test_bad_arguments_are_refused_by_the_registry_and_returned_as_an_error_once(bad: Any) -> None:
    """The registry refuses; the model sees an error object once, corrects itself, and answers."""
    script = ScriptedOllama(
        reply(tool_calls=[kpis(bad)]),
        reply(tool_calls=[kpis()]),
        reply(FINAL),
    )

    case = run_case(script)

    assert case["complete"] is True
    assert case["expected_figures_present"] is True
    refused_round = script.sent[1]["messages"][-1]
    assert refused_round["role"] == "tool" and refused_round["tool_name"] == "get_hotel_kpis"
    assert "error" in json.loads(refused_round["content"])
    assert len(script.sent) == 3


def test_two_bad_calls_end_the_case_as_a_labelled_partial_result() -> None:
    bad = {"date_from": "May"}
    script = ScriptedOllama(reply(tool_calls=[kpis(bad)]), reply(tool_calls=[kpis(bad)]))

    case = run_case(script)

    assert case["complete"] is False
    assert case["stop_reason"] == "tool_failed"


def test_a_thinking_only_final_answer_is_a_model_failure_not_a_blank_answer() -> None:
    script = ScriptedOllama(
        reply(tool_calls=[kpis()]),
        reply("", thinking="PRIVATE-REASONING", done_reason="length"),
    )

    case = run_case(script)

    assert case["complete"] is False
    assert case["stop_reason"] == "model_failed"
    assert len(script.sent) == 2  # an adapter-rejected answer is not retried (§5.7 A10)


def test_a_call_cut_off_by_the_token_limit_is_the_declared_invalid_response() -> None:
    """On the FIRST call there is no partial work to label, so the copilot surfaces the declared
    failure itself (its existing contract) -- and the truncated call never runs."""
    script = ScriptedOllama(reply(tool_calls=[kpis()], done_reason="length"))

    case = run_case(script)

    assert case["complete"] is False
    assert case["error_code"] == "LLM_INVALID_RESPONSE"
    assert case["tools_called"] == []
    assert len(script.sent) == 1
