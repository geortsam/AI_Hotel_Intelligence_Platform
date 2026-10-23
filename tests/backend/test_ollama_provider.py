"""R2.7O: the local Ollama adapter, its settings and its factory wiring -- all OFFLINE.

No test here reaches a socket, let alone `127.0.0.1:11434`: every socket is refused and recorded,
and a test fails at teardown if any attempt was made. The adapter's transport is either a
stand-in (`FakeTransport`) or the REAL `urllib` opener with an in-memory handler
(`CannedHTTPHandler`) that answers without a connection -- so the request building, the proxy
policy and the redirect refusal are exercised through the standard library itself.

Nothing here establishes how a real model answers: tool choice, accuracy, latency and citation
behaviour of `qwen3.5:4b` remain unverified until a separately approved local run.
"""

from __future__ import annotations

import email.message
import io
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import (
    IMPLICIT_PROVIDER_REFUSAL,
    INVALID_OLLAMA_URL_MESSAGE,
    LLM_BASE_URL_WITH_OLLAMA_REFUSAL,
    PROVIDER_HEADERS_REFUSAL,
    Settings,
)
from app.llm.base import Budget, ChatRequest, Message, ToolCall, ToolSpec
from app.llm.boundary import GuardedChatModel
from app.llm.errors import LlmInvalidResponseError, LlmUnavailableError
from app.llm.factory import breaker_for, build_chat_model
from app.llm.providers.ollama_provider import (
    MAX_RESPONSE_BYTES,
    OllamaChatModel,
    UrllibTransport,
    build_loopback_opener,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:11434"
CHAT_URL = BASE + "/api/chat"
MODEL = "qwen3.5:4b"
KEY_SENTINEL = "sk-ollama-must-never-see-this-key-7f2a"

MAY = {"date_from": "2026-05-01", "date_to": "2026-05-31"}
KPI_TOOL = ToolSpec(
    name="get_hotel_kpis",
    description="Occupancy and room nights for a period.",
    input_schema={
        "type": "object",
        "properties": {"date_from": {"type": "string"}, "date_to": {"type": "string"}},
        "required": ["date_from", "date_to"],
        "additionalProperties": False,
    },
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Sockets refused AND recorded, as in `test_llm_boundary.py` (R2.7A)."""
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
    """The parent shell must not decide a default under test."""
    for name in (
        "LLM_ENABLED",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_THINKING",
        "OLLAMA_BASE_URL",
        "OLLAMA_NUM_CTX",
        "OLLAMA_TEMPERATURE",
        "OLLAMA_SEED",
    ):
        monkeypatch.delenv(name, raising=False)


# --- helpers -----------------------------------------------------------------------------------


def ask(*extra: Message, tools: tuple[ToolSpec, ...] = (KPI_TOOL,)) -> ChatRequest:
    return ChatRequest(
        prompt_id="r27o.ollama",
        prompt_version="1",
        messages=(
            Message(role="system", content="Answer from tool results only."),
            Message(role="user", content="What was occupancy in May 2026?"),
            *extra,
        ),
        budget=Budget(timeout_seconds=42.0, max_output_tokens=777),
        tools=tools,
    )


def answer(
    content: str = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    done_reason: str = "stop",
    model: str | None = MODEL,
    **extra: Any,
) -> dict[str, Any]:
    """A `/api/chat` response body, as the Ollama documentation describes it."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "message": message,
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": 321,
        "eval_count": 45,
        **extra,
    }
    if model is not None:
        body["model"] = model
    return body


def call(name: str = "get_hotel_kpis", arguments: Any = None, **extra: Any) -> dict[str, Any]:
    return {
        "function": {"name": name, "arguments": MAY if arguments is None else arguments},
        **extra,
    }


class FakeTransport:
    """Answers in order and keeps every request body (decoded) with its URL and timeout."""

    def __init__(self, *replies: dict[str, Any] | bytes | BaseException) -> None:
        self._replies = list(replies)
        self.sent: list[tuple[str, dict[str, Any], float]] = []

    def post_json(self, url: str, body: bytes, timeout: float) -> bytes:
        self.sent.append((url, json.loads(body), timeout))
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, bytes) else json.dumps(reply).encode("utf-8")


def adapter(*replies: Any, **kwargs: Any) -> tuple[OllamaChatModel, FakeTransport]:
    transport = FakeTransport(*replies)
    return OllamaChatModel(model=MODEL, transport=transport, **kwargs), transport


class CannedHTTPHandler(urllib.request.HTTPHandler):
    """Serves canned responses INSIDE urllib, with no socket. Records each request it sees."""

    def __init__(self, *replies: tuple[int, dict[str, str], bytes]) -> None:
        super().__init__()
        self._replies = list(replies)
        self.seen: list[urllib.request.Request] = []

    def http_open(self, req: urllib.request.Request) -> urllib.response.addinfourl:  # type: ignore[override]
        self.seen.append(req)
        status, headers, body = self._replies.pop(0)
        message = email.message.Message()
        for name, value in headers.items():
            message[name] = value
        response = urllib.response.addinfourl(io.BytesIO(body), message, req.full_url, status)
        response.msg = "canned"  # type: ignore[attr-defined]
        return response


def handlers_of(opener: urllib.request.OpenerDirector) -> list[urllib.request.BaseHandler]:
    """The handlers an opener kept (a public attribute the type stubs do not declare)."""
    handlers: list[urllib.request.BaseHandler] = getattr(opener, "handlers")  # noqa: B009
    return handlers


def ok_json(body: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "application/json"}, json.dumps(body).encode("utf-8")


def enabled(**overrides: Any) -> Settings:
    return Settings(_env_file=None, environment="test", llm_enabled=True, **overrides)


# --- settings ----------------------------------------------------------------------------------


def test_the_provider_defaults_to_the_local_model_and_the_copilot_stays_off() -> None:
    settings = Settings(_env_file=None, environment="test")

    assert settings.llm_enabled is False
    assert settings.llm_provider == "ollama"
    assert settings.llm_model is None
    assert settings.effective_llm_model == "qwen3.5:4b"
    assert settings.ollama_base_url == "http://127.0.0.1:11434"
    assert (settings.ollama_num_ctx, settings.ollama_temperature, settings.ollama_seed) == (
        16_384,
        0.0,
        7151,
    )
    assert settings.llm_thinking == "disabled"


def test_each_provider_has_its_own_default_model_and_an_explicit_one_wins() -> None:
    assert Settings(_env_file=None, llm_provider="anthropic").effective_llm_model == (
        "claude-sonnet-5"
    )
    assert Settings(_env_file=None, llm_model="qwen3.5:9b").effective_llm_model == "qwen3.5:9b"


def test_an_enabled_local_model_needs_no_key_and_the_paid_one_still_does() -> None:
    assert enabled().llm_api_key is None  # loads: ollama has no key

    with pytest.raises(ValueError, match="LLM_API_KEY is not set"):
        enabled(llm_provider="anthropic")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://[::1]:11434",
        "https://127.0.0.1:11434",
        "HTTP://LOCALHOST:11434",
        "http://127.0.0.1:11434/",
        "http://127.0.0.1:11434/ollama",
        "http://127.0.0.1",
    ],
)
def test_a_loopback_ollama_url_is_accepted(url: str) -> None:
    assert Settings(_env_file=None, ollama_base_url=url).ollama_base_url == url
    assert OllamaChatModel(model=MODEL, base_url=url, transport=FakeTransport())


@pytest.mark.parametrize(
    "url",
    [
        "",
        "127.0.0.1:11434",
        "//127.0.0.1:11434",
        "ftp://127.0.0.1:11434",
        "http://192.168.1.10:11434",
        "http://10.0.0.5:11434",
        "http://example.com:11434",
        "http://127.0.0.1.attacker.example:11434",
        "http://localhost.attacker.example",
        "http://127.0.0.2:11434",
        "http://0.0.0.0:11434",
        "http://user:secret-in-url@127.0.0.1:11434",
        "http://user@localhost:11434",
        "http://127.0.0.1:11434/?model=x",
        "http://127.0.0.1:11434/?",
        "http://127.0.0.1:11434/#fragment",
        "http://127.0.0.1:11434#",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:port",
        " http://127.0.0.1:11434",
        "http://127.0.0.1:11434 ",
        "http://127.0.0.1:11434/a path",
        "http://127.0.0.1:11434/\x7f",
        "http://127.0.0.1:11434/\nX-Injected: 1",
    ],
)
def test_an_off_machine_or_malformed_ollama_url_is_refused_at_load_and_in_the_adapter(
    url: str,
) -> None:
    """Refused at load when the copilot would use it (enabled, on ollama), and by the adapter."""
    with pytest.raises(ValidationError) as refused:
        enabled(ollama_base_url=url)
    assert INVALID_OLLAMA_URL_MESSAGE in str(refused.value)
    assert "secret-in-url" not in str(refused.value)
    assert "attacker" not in str(refused.value)

    with pytest.raises(ValueError) as at_construction:
        OllamaChatModel(model=MODEL, base_url=url, transport=FakeTransport())
    assert str(at_construction.value) == INVALID_OLLAMA_URL_MESSAGE


# --- R2.7T (H1): a foreign OLLAMA_BASE_URL must not stop an application that does not use it --

#: What another tool -- an Open WebUI container, say -- may export under the same name.
FOREIGN_OLLAMA_URL = "http://host.docker.internal:11434"


def test_a_foreign_ollama_url_does_not_block_a_disabled_copilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 1. Before R2.7T this refused to load, and the whole API with it. (That the API
    then answers is in `test_app_factory.py`, whose tests may start a TestClient.)"""
    from app.main import create_app

    monkeypatch.setenv("OLLAMA_BASE_URL", FOREIGN_OLLAMA_URL)

    settings = Settings(_env_file=None, environment="test")

    assert settings.llm_enabled is False
    assert settings.ollama_base_url == FOREIGN_OLLAMA_URL  # read, merely unused
    assert create_app(settings) is not None


def test_a_foreign_ollama_url_does_not_block_the_paid_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 2: enabled on anthropic, which never reads OLLAMA_BASE_URL."""
    monkeypatch.setenv("OLLAMA_BASE_URL", FOREIGN_OLLAMA_URL)

    settings = enabled(llm_provider="anthropic", llm_api_key="test-key-not-real")

    assert (settings.llm_enabled, settings.llm_provider) == (True, "anthropic")


def test_a_foreign_ollama_url_is_refused_when_the_copilot_would_use_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 3: enabled on ollama -- the one configuration that builds a client from it."""
    monkeypatch.setenv("OLLAMA_BASE_URL", FOREIGN_OLLAMA_URL)

    with pytest.raises(ValidationError) as refused:
        enabled()

    assert INVALID_OLLAMA_URL_MESSAGE in str(refused.value)
    assert "host.docker.internal" not in str(refused.value)


def test_the_adapter_still_refuses_when_settings_validation_is_bypassed() -> None:
    """`model_construct` skips every validator; the adapter's own check, at construction, does
    not -- so no request can be sent to a non-loopback host either way."""
    unvalidated = Settings.model_construct(
        environment="test",
        llm_enabled=True,
        llm_provider="ollama",
        ollama_base_url=FOREIGN_OLLAMA_URL,
    )

    with pytest.raises(ValueError, match="OLLAMA_BASE_URL must be"):
        build_chat_model(unvalidated)


@pytest.mark.parametrize(
    ("value", "valid"), [(4095, False), (4096, True), (262_144, True), (262_145, False)]
)
def test_the_context_window_is_bounded(value: int, valid: bool) -> None:
    if valid:
        assert Settings(_env_file=None, ollama_num_ctx=value).ollama_num_ctx == value
    else:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, ollama_num_ctx=value)


def test_llm_base_url_is_refused_beside_an_enabled_local_model() -> None:
    with pytest.raises(ValueError) as refused:
        enabled(llm_base_url="https://gateway.example.test")
    assert LLM_BASE_URL_WITH_OLLAMA_REFUSAL in str(refused.value)
    assert "gateway.example.test" not in str(refused.value)

    # Disabled, it builds nothing; with the paid provider it is that provider's own setting.
    assert (
        Settings(_env_file=None, llm_base_url="https://gateway.example.test").llm_enabled is False
    )
    assert enabled(
        llm_provider="anthropic", llm_api_key="k", llm_base_url="https://gateway.example.test"
    ).llm_base_url


def test_the_anthropic_header_rule_is_the_paid_providers_and_still_holds_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Probe: SENTINEL-HEADER")

    assert enabled().llm_provider == "ollama"  # the ollama path never reads that variable
    with pytest.raises(ValueError) as refused:
        enabled(llm_provider="anthropic", llm_api_key="k")
    assert PROVIDER_HEADERS_REFUSAL in str(refused.value)


# --- R2.7V (M2): an API key beside a provider nobody chose ---------------------------------------


def test_a_key_beside_an_implicit_provider_is_refused_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before R2.7O this configuration meant the paid provider; under the `ollama` default it
    would silently switch to the local model. Refused at load, by a fixed message."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", KEY_SENTINEL)

    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, environment="test")

    assert IMPLICIT_PROVIDER_REFUSAL in str(refused.value)
    assert KEY_SENTINEL not in str(refused.value)
    assert KEY_SENTINEL not in repr(refused.value)


def test_a_key_beside_an_implicit_provider_is_refused_from_a_dotenv_file(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"LLM_ENABLED=true\nLLM_API_KEY={KEY_SENTINEL}\n", encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=dotenv, environment="test")

    assert IMPLICIT_PROVIDER_REFUSAL in str(refused.value)
    assert KEY_SENTINEL not in str(refused.value)


@pytest.mark.parametrize("provider", ["ollama", "anthropic"])
@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_an_explicit_provider_beside_a_key_loads_from_either_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str, source: str
) -> None:
    """The switch and the key come from the environment; the provider from the environment or
    a `.env` file. Either way it was chosen, so it is honoured."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", KEY_SENTINEL)
    dotenv = tmp_path / ".env"
    if source == "environment":
        monkeypatch.setenv("LLM_PROVIDER", provider)
        dotenv.write_text("", encoding="utf-8")
    else:
        dotenv.write_text(f"LLM_PROVIDER={provider}\n", encoding="utf-8")

    settings = Settings(_env_file=dotenv, environment="test")

    assert (settings.llm_enabled, settings.llm_provider) == (True, provider)
    assert "llm_provider" in settings.model_fields_set


def test_an_explicit_local_provider_beside_a_key_builds_the_local_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built, not called: no request is made (the socket guard would fail the test)."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", KEY_SENTINEL)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    guarded = build_chat_model(Settings(_env_file=None, environment="test"))

    assert isinstance(guarded, GuardedChatModel)
    assert isinstance(guarded._inner, OllamaChatModel)


@pytest.mark.parametrize(
    "variables",
    [
        pytest.param({"LLM_ENABLED": "true"}, id="enabled-without-a-key"),
        pytest.param({"LLM_API_KEY": KEY_SENTINEL}, id="disabled-with-a-key"),
        pytest.param({"LLM_ENABLED": "true", "LLM_API_KEY": "   "}, id="whitespace-only-key"),
        pytest.param({"LLM_ENABLED": "true", "LLM_API_KEY": ""}, id="empty-key"),
    ],
)
def test_the_default_provider_still_loads_with_no_usable_key_or_the_copilot_off(
    monkeypatch: pytest.MonkeyPatch, variables: dict[str, str]
) -> None:
    """No key to misroute, or nothing built: the default is not refused. A whitespace-only key
    counts as none, as it does for the paid provider's key rule."""
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None, environment="test")

    assert settings.llm_provider == "ollama"
    assert "llm_provider" not in settings.model_fields_set


# --- the factory: exactly the selected adapter, no fallback ------------------------------------


def test_the_factory_builds_the_local_adapter_from_settings() -> None:
    settings = enabled(
        ollama_base_url="http://localhost:11434",
        ollama_num_ctx=8192,
        ollama_temperature=0.25,
        ollama_seed=99,
        llm_thinking="provider_default",
    )

    guarded = build_chat_model(settings)

    assert isinstance(guarded, GuardedChatModel)
    inner = guarded._inner
    assert isinstance(inner, OllamaChatModel)
    assert (inner._model, inner._url, inner._num_ctx, inner._temperature, inner._seed) == (
        "qwen3.5:4b",
        "http://localhost:11434/api/chat",
        8192,
        0.25,
        99,
    )
    assert inner._thinking == "provider_default"
    assert guarded._breaker is breaker_for("ollama", "qwen3.5:4b")


def test_a_failing_local_model_never_falls_back_to_the_paid_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local model fails; the paid SDK -- stood in for here -- is never touched."""
    constructed: list[object] = []

    class PaidSdk:
        def Anthropic(self, **kwargs: object) -> object:  # noqa: N802 - the SDK's own spelling
            constructed.append(kwargs)
            raise AssertionError("the paid provider was reached")

    monkeypatch.setitem(sys.modules, "anthropic", PaidSdk())
    guarded = build_chat_model(enabled(llm_provider="ollama", llm_api_key=KEY_SENTINEL))
    guarded._inner._transport = FakeTransport(urllib.error.URLError(ConnectionRefusedError()))  # type: ignore[attr-defined]

    with pytest.raises(LlmUnavailableError):
        guarded.complete(ask())

    assert constructed == []


def test_selecting_the_local_model_does_not_even_import_the_paid_adapter() -> None:
    """In a FRESH interpreter, where nothing else has imported it. Builds; calls nothing."""
    program = (
        "import sys\n"
        "from app.core.config import Settings\n"
        "from app.llm.factory import build_chat_model\n"
        "build_chat_model(Settings(_env_file=None, environment='test', llm_enabled=True))\n"
        "loaded = sorted(m for m in sys.modules if 'anthropic' in m)\n"
        "print('LOADED=' + ','.join(loaded))\n"
    )
    env = {"PYTHONPATH": str(REPOSITORY_ROOT / "backend"), "SYSTEMROOT": _system_root()}
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "LOADED=\n" in result.stdout or result.stdout.strip() == "LOADED="


def _system_root() -> str:
    import os

    return os.environ.get("SYSTEMROOT", "")


# --- the request ------------------------------------------------------------------------------


def test_the_kpis_request_is_exactly_the_documented_native_body() -> None:
    model, transport = adapter(answer("x"))

    model.complete(ask())

    [(url, body, timeout)] = transport.sent
    assert url == CHAT_URL
    assert timeout == 42.0  # the transport deadline is the request's own budget
    assert body == {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {
            "num_ctx": 16_384,
            "num_predict": 777,
            "temperature": 0.0,
            "seed": 7151,
            "presence_penalty": 0,
        },
        "messages": [
            {"role": "system", "content": "Answer from tool results only."},
            {"role": "user", "content": "What was occupancy in May 2026?"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_hotel_kpis",
                    "description": "Occupancy and room nights for a period.",
                    "parameters": dict(KPI_TOOL.input_schema),
                },
            }
        ],
    }


def test_provider_default_omits_think_and_no_tools_omits_tools() -> None:
    model, transport = adapter(answer("x"), thinking="provider_default")

    model.complete(ask(tools=()))

    [(_, body, _)] = transport.sent
    assert "think" not in body
    assert "tools" not in body


def test_an_unsupported_thinking_mode_is_refused_before_any_request() -> None:
    with pytest.raises(ValueError, match="LLM_THINKING must be one of"):
        OllamaChatModel(model=MODEL, thinking="enabled", transport=FakeTransport())


def test_a_tool_round_is_replayed_with_tool_name_and_several_system_turns_join() -> None:
    model, transport = adapter(answer("done"))
    first = ToolCall(id="call_2_0", name="get_hotel_kpis", arguments=MAY)
    second = ToolCall(id="call_2_1", name="get_daily_series", arguments={"metric": "x"})

    model.complete(
        ChatRequest(
            prompt_id="p",
            prompt_version="1",
            messages=(
                Message(role="system", content="A."),
                Message(role="system", content="B."),
                Message(role="user", content="Q"),
                Message(role="assistant", content="", tool_calls=(first, second)),
                Message(role="tool", content='{"occupancy":"0.7000"}', tool_call_id="call_2_0"),
                Message(
                    role="tool",
                    content='{"error":{"code":"X","message":"no"}}',
                    tool_call_id="call_2_1",
                    is_error=True,
                ),
            ),
            budget=Budget(timeout_seconds=5, max_output_tokens=10),
        )
    )

    [(_, body, _)] = transport.sent
    assert body["messages"] == [
        {"role": "system", "content": "A.\n\nB."},
        {"role": "user", "content": "Q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "get_hotel_kpis", "arguments": MAY}},
                {"function": {"name": "get_daily_series", "arguments": {"metric": "x"}}},
            ],
        },
        {"role": "tool", "content": '{"occupancy":"0.7000"}', "tool_name": "get_hotel_kpis"},
        {
            "role": "tool",
            "content": '{"error":{"code":"X","message":"no"}}',
            "tool_name": "get_daily_series",
        },
    ]


def test_a_result_answering_no_earlier_call_is_refused_before_any_request() -> None:
    model, transport = adapter()

    with pytest.raises(ValueError, match="names no earlier call"):
        model.complete(ask(Message(role="tool", content="{}", tool_call_id="never-made")))
    assert transport.sent == []


# --- the response ------------------------------------------------------------------------------


def test_a_text_answer_carries_usage_finish_reason_and_the_returned_model() -> None:
    model, _ = adapter(answer("May occupancy was 70.00%.", model="qwen3.5:4b-q4"))

    response = model.complete(ask())

    assert response.text == "May occupancy was 70.00%."
    assert response.tool_calls == ()
    assert response.finish_reason == "stop"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (321, 45)
    assert response.provider == "ollama"
    assert response.model == "qwen3.5:4b-q4"
    assert response.diagnostics == {"response_model": "qwen3.5:4b-q4"}


def test_a_response_naming_no_model_records_none_rather_than_the_configured_name() -> None:
    model, _ = adapter(answer("x", model=None))

    response = model.complete(ask())

    assert response.diagnostics == {}
    assert response.model == MODEL


def test_tool_calls_without_ids_get_deterministic_ones_and_given_ids_are_kept() -> None:
    model, _ = adapter(
        answer(tool_calls=[call(), call("get_daily_series", {"metric": "x"})]),
        answer(tool_calls=[call(id="srv-1")]),
    )

    generated = model.complete(ask()).tool_calls
    kept = model.complete(ask()).tool_calls

    assert [c.id for c in generated] == ["call_2_0", "call_2_1"]  # 2 = messages in the request
    assert [(c.name, dict(c.arguments)) for c in generated] == [
        ("get_hotel_kpis", MAY),
        ("get_daily_series", {"metric": "x"}),
    ]
    assert [c.id for c in kept] == ["srv-1"]


def test_ids_stay_unique_across_the_turns_of_one_conversation() -> None:
    model, _ = adapter(answer(tool_calls=[call()]), answer(tool_calls=[call()]))

    [first] = model.complete(ask()).tool_calls
    [second] = model.complete(
        ask(
            Message(role="assistant", content="", tool_calls=(first,)),
            Message(role="tool", content="{}", tool_call_id=first.id),
        )
    ).tool_calls

    assert first.id != second.id


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        (json.dumps(MAY), MAY),
        ('{"date_from": "2026-05-01"', {}),
        ('["2026-05-01", "2026-05-31"]', {}),
        ("", {}),
        (["2026-05-01"], {}),
        (None, {}),
        (42, {}),
    ],
)
def test_arguments_that_are_not_an_object_fail_closed_to_an_empty_mapping(
    raw: Any, parsed: dict[str, Any]
) -> None:
    """A JSON-string object is parsed; anything else becomes `{}`, which the registry's schema
    check refuses (missing required fields) -- the declared path for bad arguments."""
    model, _ = adapter(
        answer(tool_calls=[{"function": {"name": "get_hotel_kpis", "arguments": raw}}])
    )

    [only] = model.complete(ask()).tool_calls

    assert dict(only.arguments) == parsed


def test_thinking_is_never_answer_text() -> None:
    model, _ = adapter(
        {
            **answer("The answer."),
            "message": {
                "role": "assistant",
                "content": "The answer.",
                "thinking": "PRIVATE-REASONING",
            },
        }
    )

    response = model.complete(ask())

    assert response.text == "The answer."
    assert "PRIVATE-REASONING" not in repr(response)


def test_a_thinking_only_answer_is_the_declared_invalid_response() -> None:
    model, _ = adapter(
        {
            **answer(""),
            "done_reason": "length",
            "message": {"role": "assistant", "content": "", "thinking": "PRIVATE-REASONING"},
        }
    )

    with pytest.raises(LlmInvalidResponseError):
        model.complete(ask())


def test_a_call_cut_off_by_the_token_limit_is_refused_but_a_cut_text_is_kept() -> None:
    model, _ = adapter(
        answer(tool_calls=[call()], done_reason="length"),
        answer("A truncated but honest answ", done_reason="length"),
    )

    with pytest.raises(LlmInvalidResponseError):
        model.complete(ask())
    assert model.complete(ask()).finish_reason == "length"


def test_duplicate_call_ids_are_refused_because_results_could_not_be_paired() -> None:
    model, _ = adapter(answer(tool_calls=[call(id="same"), call(id="same")]))

    with pytest.raises(LlmInvalidResponseError):
        model.complete(ask())


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b"{}",
        json.dumps({"message": "text"}).encode(),
        json.dumps({"message": {"content": 7}, "done": True}).encode(),
        json.dumps({"message": {"content": "", "tool_calls": "x"}, "done": True}).encode(),
        json.dumps({"message": {"content": "", "tool_calls": [{"function": {}}]}}).encode(),
        json.dumps({"message": {"content": "", "tool_calls": [{"nope": 1}]}}).encode(),
        json.dumps({"message": {"content": "partial"}, "done": False}).encode(),
        json.dumps({"message": {"content": ""}, "done": True}).encode(),
    ],
)
def test_a_malformed_body_is_the_declared_invalid_response(raw: bytes) -> None:
    model, _ = adapter(raw)

    with pytest.raises(LlmInvalidResponseError):
        model.complete(ask())


def test_an_oversized_body_is_refused_not_truncated() -> None:
    model, _ = adapter(b"{" + b" " * MAX_RESPONSE_BYTES + b"}")

    with pytest.raises(LlmInvalidResponseError):
        model.complete(ask())


@pytest.mark.parametrize("bad", [None, -1, True, "12", 1.5])
def test_a_missing_or_nonsense_token_count_is_zero(bad: Any) -> None:
    model, _ = adapter(answer("x", prompt_eval_count=bad, eval_count=bad))

    usage = model.complete(ask()).usage

    assert (usage.input_tokens, usage.output_tokens) == (0, 0)


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError(ConnectionRefusedError("refused")),
        urllib.error.HTTPError(CHAT_URL, 500, "error", email.message.Message(), None),
        urllib.error.HTTPError(CHAT_URL, 404, "model not found", email.message.Message(), None),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        type("IncompleteRead", (Exception,), {})("cut"),
    ],
    ids=["refused", "http-500", "http-404", "timeout", "reset", "other-transport-fault"],
)
def test_every_transport_failure_is_the_declared_unavailable_error(failure: BaseException) -> None:
    model, _ = adapter(failure)

    with pytest.raises(LlmUnavailableError):
        model.complete(ask())


def test_a_transport_failure_through_the_boundary_is_one_request_and_a_breaker_failure() -> None:
    """§5.7 A10 unchanged: an unavailable provider is not retried by the boundary."""
    from app.llm.circuit import CircuitBreaker

    model, transport = adapter(urllib.error.URLError(ConnectionRefusedError()))
    breaker = CircuitBreaker()
    guarded = GuardedChatModel(
        model, breaker=breaker, sleep=lambda _s: None, jitter=lambda _a, _b: 0.0
    )

    with pytest.raises(LlmUnavailableError):
        guarded.complete(ask())

    assert len(transport.sent) == 1


# --- the real urllib path, without a socket ----------------------------------------------------


def test_the_real_opener_posts_json_with_no_credential_header() -> None:
    handler = CannedHTTPHandler(ok_json(answer("x")))
    model = OllamaChatModel(model=MODEL, transport=UrllibTransport(build_loopback_opener(handler)))

    model.complete(ask())

    [request] = handler.seen
    assert request.get_method() == "POST"
    assert request.full_url == CHAT_URL
    assert request.timeout == 42.0
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["content-type"] == "application/json"
    for credential in ("authorization", "x-api-key", "cookie", "proxy-authorization"):
        assert credential not in headers
    assert json.loads(request.data or b"")["model"] == MODEL  # type: ignore[arg-type]


def test_a_configured_api_key_never_reaches_the_local_server() -> None:
    """Set beside ollama, the key is simply never handed to the adapter: not in a header, not
    in the body."""
    guarded = build_chat_model(enabled(llm_provider="ollama", llm_api_key=KEY_SENTINEL))
    handler = CannedHTTPHandler(ok_json(answer("x")))
    guarded._inner._transport = UrllibTransport(build_loopback_opener(handler))  # type: ignore[attr-defined]

    guarded.complete(ask())

    [request] = handler.seen
    assert KEY_SENTINEL not in json.dumps(request.header_items())
    assert KEY_SENTINEL.encode() not in (request.data or b"")  # type: ignore[operator]
    assert KEY_SENTINEL not in repr(vars(guarded._inner))  # type: ignore[attr-defined]


def test_the_opener_ignores_every_proxy_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """With proxy variables set, a DEFAULT opener would route through them (shown first); the
    adapter's opener has only an empty ProxyHandler, and the request goes straight to loopback."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://proxy.attacker.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    default_proxies = [
        getattr(h, "proxies", {})
        for h in handlers_of(urllib.request.build_opener())
        if isinstance(h, urllib.request.ProxyHandler)
    ]
    assert any("http" in proxies for proxies in default_proxies)  # the threat is real

    handler = CannedHTTPHandler(ok_json(answer("x")))
    opener = build_loopback_opener(handler)
    # The empty ProxyHandler registers no protocol method, so urllib does not keep it -- and,
    # because one was passed, build_opener does not add its environment-reading default either.
    assert not [h for h in handlers_of(opener) if isinstance(h, urllib.request.ProxyHandler)]

    OllamaChatModel(model=MODEL, transport=UrllibTransport(opener)).complete(ask())

    [request] = handler.seen
    assert request.host == "127.0.0.1:11434"
    assert "attacker" not in request.full_url
    assert request._tunnel_host is None  # type: ignore[attr-defined]


def test_a_redirect_is_refused_and_never_followed() -> None:
    handler = CannedHTTPHandler(
        (302, {"Location": "http://elsewhere.attacker.invalid/api/chat"}, b""),
        ok_json(answer("must not be fetched")),
    )
    model = OllamaChatModel(model=MODEL, transport=UrllibTransport(build_loopback_opener(handler)))

    with pytest.raises(LlmUnavailableError):
        model.complete(ask())

    assert [r.full_url for r in handler.seen] == [CHAT_URL]


def test_a_non_success_status_through_the_real_opener_is_unavailable() -> None:
    handler = CannedHTTPHandler((500, {"Content-Type": "application/json"}, b'{"error":"x"}'))
    model = OllamaChatModel(model=MODEL, transport=UrllibTransport(build_loopback_opener(handler)))

    with pytest.raises(LlmUnavailableError):
        model.complete(ask())
