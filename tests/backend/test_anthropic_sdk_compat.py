"""R2.7F: the INSTALLED Anthropic SDK parsing real response bodies -- with no network.

Under `anthropic==0.42.0` on Python 3.14, a response carrying a `thinking` or
`redacted_thinking` block raised `AttributeError` inside the SDK's own parser (it set
`__discriminator__` on a `typing.Union`, which 3.14 forbids), after the request had been billed.
Upstream fixed the line in 0.72.1; the optional pin is now `0.125.0`.

Nothing here replaces the SDK with a stand-in. A real `anthropic.Anthropic` client builds the
request, and its real parser reads the response; only the HTTP transport is `httpx.MockTransport`,
which answers in memory and opens no socket. Sockets are disabled for every test anyway, and an
attempt fails the test at teardown even if something caught the refusal.

The whole module is skipped where the SDK is not installed -- CI and the image, which never
install `backend/requirements-llm.txt`. So these tests do NOT run in CI: they run on a developer
machine that has installed the optional SDK, and a green CI run says nothing about them. The
adapter's behaviour without the SDK is covered by `test_llm_boundary.py`, which CI does run.
"""

from __future__ import annotations

import importlib.metadata
import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Both imported before any test patches `socket`: `ssl` subclasses `socket.socket` at import.
import httpx
import pytest

from app.llm.base import Budget, ChatRequest, Message, ToolCall, ToolSpec
from app.llm.boundary import GuardedChatModel
from app.llm.errors import LlmInvalidResponseError
from app.llm.providers.anthropic_provider import DEFAULT_BASE_URL, AnthropicChatModel

anthropic = pytest.importorskip("anthropic")

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_KEY = "test-key-not-real"
MODEL = "claude-sonnet-5"

TEXT: dict[str, Any] = {"type": "text", "text": "May occupancy was 70.00% across 434 room nights."}
TOOL_USE: dict[str, Any] = {
    "type": "tool_use",
    "id": "toolu_r27f_0001",
    "name": "get_hotel_kpis",
    "input": {"start": "2026-05-01", "end": "2026-05-31", "metrics": ["occupancy"]},
}
THINKING: dict[str, Any] = {
    "type": "thinking",
    "thinking": "PRIVATE-REASONING: the user asks about May.",
    "signature": "c2lnbmF0dXJlLW5vdC1yZWFs",
}
REDACTED: dict[str, Any] = {"type": "redacted_thinking", "data": "PRIVATE-REDACTED-PAYLOAD"}


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
def no_ambient_provider_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK reads these when a value is not passed. Removed unread, so none can matter."""
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def message(*content: dict[str, Any], stop_reason: str = "end_turn") -> dict[str, Any]:
    """A Messages API response body, as the provider documents it."""
    return {
        "id": "msg_r27f",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": list(content),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 45},
    }


class Endpoint:
    """The provider's HTTP endpoint, in memory: answers in order, keeps each request BODY.

    Headers are never kept -- they carry the (fake) key -- and nothing is printed.
    """

    def __init__(self, *bodies: dict[str, Any]) -> None:
        self._bodies = list(bodies)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json=self._bodies.pop(0))


def real_client(endpoint: Endpoint) -> Any:
    return anthropic.Anthropic(
        api_key=TEST_KEY,
        base_url=DEFAULT_BASE_URL,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(endpoint)),
    )


def real_adapter(endpoint: Endpoint, *, thinking: str | None = None) -> AnthropicChatModel:
    """The adapter over the real client; `thinking` omitted means the adapter's own default."""
    if thinking is None:
        return AnthropicChatModel(api_key=TEST_KEY, model=MODEL, client=real_client(endpoint))
    return AnthropicChatModel(
        api_key=TEST_KEY, model=MODEL, thinking=thinking, client=real_client(endpoint)
    )


KPI_TOOL = ToolSpec(
    name="get_hotel_kpis",
    description="Occupancy and room nights for a period.",
    input_schema={"type": "object", "properties": {"start": {"type": "string"}}},
)


def ask(*messages: Message) -> ChatRequest:
    return ChatRequest(
        prompt_id="r27f.compat",
        prompt_version="1",
        messages=(
            Message(role="system", content="Answer from tool results only."),
            Message(role="user", content="What was occupancy in May?"),
            *messages,
        ),
        budget=Budget(timeout_seconds=30, max_output_tokens=1024),
        tools=(KPI_TOOL,),
    )


# --- the installed SDK is the approved one -------------------------------------------------


def test_the_installed_sdk_is_the_pinned_one() -> None:
    """What this module proves is about the pinned SDK; a different install proves nothing."""
    optional = (REPOSITORY_ROOT / "backend" / "requirements-llm.txt").read_text(encoding="utf-8")
    [pin] = [line for line in optional.splitlines() if line.startswith("anthropic==")]

    assert pin == f"anthropic=={importlib.metadata.version('anthropic')}"
    assert anthropic.__version__ == importlib.metadata.version("anthropic")


# --- the SDK's own parser ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "classes"),
    [
        pytest.param([TEXT, TOOL_USE], ["TextBlock", "ToolUseBlock"], id="text+tool_use"),
        pytest.param(
            [THINKING, TEXT, TOOL_USE],
            ["ThinkingBlock", "TextBlock", "ToolUseBlock"],
            id="thinking+text+tool_use",
        ),
        pytest.param([REDACTED, TEXT], ["RedactedThinkingBlock", "TextBlock"], id="redacted+text"),
    ],
)
def test_the_installed_sdk_parses_each_payload_without_the_314_crash(
    content: list[dict[str, Any]], classes: list[str]
) -> None:
    """The parse that raised `AttributeError` under 0.42.0 on Python 3.14 now completes.

    Every block keeps its type, its position and its fields -- including a thinking block's
    signature and a redacted block's opaque data.
    """
    endpoint = Endpoint(message(*content))

    parsed = real_client(endpoint).messages.create(
        model=MODEL, max_tokens=1024, messages=[{"role": "user", "content": "q"}]
    )

    assert [type(block).__name__ for block in parsed.content] == classes
    for block, sent in zip(parsed.content, content, strict=True):
        for key, value in sent.items():
            assert getattr(block, key) == value
    assert len(endpoint.sent) == 1


def test_a_block_type_newer_than_the_sdk_does_not_crash_the_parse() -> None:
    """The fallback path for an unknown discriminator -- the very path that crashed on 3.14."""
    endpoint = Endpoint(message({"type": "a_block_type_from_the_future", "x": 1}, TEXT))

    parsed = real_client(endpoint).messages.create(
        model=MODEL, max_tokens=1024, messages=[{"role": "user", "content": "q"}]
    )

    assert [block.type for block in parsed.content] == ["a_block_type_from_the_future", "text"]


# --- the adapter, over the real parser ---------------------------------------------------------


def test_text_and_tool_use_are_extracted() -> None:
    response = real_adapter(Endpoint(message(TEXT, TOOL_USE, stop_reason="tool_use"))).complete(
        ask()
    )

    assert response.text == TEXT["text"]
    assert response.tool_calls == (
        ToolCall(id=TOOL_USE["id"], name=TOOL_USE["name"], arguments=TOOL_USE["input"]),
    )
    assert response.finish_reason == "tool_use"
    assert response.diagnostics == {"response_model": MODEL}


def test_thinking_is_never_answer_text_and_the_calls_survive_it() -> None:
    response = real_adapter(
        Endpoint(message(THINKING, TEXT, TOOL_USE, stop_reason="tool_use"))
    ).complete(ask())

    assert response.text == TEXT["text"]
    assert [call.id for call in response.tool_calls] == [TOOL_USE["id"]]
    assert dict(response.tool_calls[0].arguments) == TOOL_USE["input"]
    assert "PRIVATE-REASONING" not in repr(response)
    assert THINKING["signature"] not in repr(response)


def test_redacted_thinking_is_never_answer_text() -> None:
    response = real_adapter(Endpoint(message(REDACTED, TEXT))).complete(ask())

    assert response.text == TEXT["text"]
    assert response.tool_calls == ()
    assert "PRIVATE-REDACTED-PAYLOAD" not in repr(response)


def test_an_unknown_block_is_not_mistaken_for_text_although_the_sdk_types_it_as_one() -> None:
    """The SDK's fallback gives an unknown block the `TextBlock` class. The adapter reads `type`."""
    unknown = {"type": "a_block_type_from_the_future", "text": "NOT-AN-ANSWER"}

    response = real_adapter(Endpoint(message(unknown, TEXT))).complete(ask())

    assert response.text == TEXT["text"]


def test_thinking_alone_cut_off_at_the_token_limit_is_the_declared_invalid_response() -> None:
    """Thinking tokens count toward `max_tokens`; with no text and no call there is no answer.

    The adapter's long-standing rule -- no text and no calls is `LLM_INVALID_RESPONSE`, a 502 --
    applies unchanged, and per §5.7 Amendment A10 the boundary does not retry it: one request.
    """
    endpoint = Endpoint(message(THINKING, stop_reason="max_tokens"))
    guarded = GuardedChatModel(
        real_adapter(endpoint), sleep=lambda _s: None, jitter=lambda _lo, _hi: 0.0
    )

    with pytest.raises(LlmInvalidResponseError) as raised:
        guarded.complete(ask())

    assert raised.value.status_code == 502
    assert len(endpoint.sent) == 1


def test_a_call_cut_off_at_the_token_limit_after_thinking_is_still_refused() -> None:
    endpoint = Endpoint(message(THINKING, TOOL_USE, stop_reason="max_tokens"))

    with pytest.raises(LlmInvalidResponseError):
        real_adapter(endpoint).complete(ask())


# --- what the real SDK actually sends ----------------------------------------------------------


def test_provider_default_sends_no_thinking_parameter() -> None:
    endpoint = Endpoint(message(TEXT))

    real_adapter(endpoint, thinking="provider_default").complete(ask())

    [body] = endpoint.sent
    assert "thinking" not in body
    assert body["model"] == MODEL


def test_disabled_sends_thinking_disabled_on_the_wire() -> None:
    endpoint = Endpoint(message(TEXT))

    real_adapter(endpoint, thinking="disabled").complete(ask())

    [body] = endpoint.sent
    assert body["thinking"] == {"type": "disabled"}


def test_a_tool_use_turn_is_replayed_with_its_ids_and_arguments_unchanged() -> None:
    """With thinking disabled there are no thinking blocks to lose, and the replayed turn is the
    provider's own: the same text, the same call id, name and arguments, then the result."""
    endpoint = Endpoint(
        message(TEXT, TOOL_USE, stop_reason="tool_use"),
        message({"type": "text", "text": "Occupancy in May was 70.00%."}),
    )
    model = real_adapter(endpoint, thinking="disabled")

    first = model.complete(ask())
    [call] = first.tool_calls
    second = model.complete(
        ask(
            Message(role="assistant", content=first.text, tool_calls=first.tool_calls),
            Message(role="tool", content='{"occupancy": "0.7000"}', tool_call_id=call.id),
        )
    )

    assert second.text == "Occupancy in May was 70.00%."
    replay = endpoint.sent[1]
    assert replay["thinking"] == {"type": "disabled"}
    assert replay["messages"][1] == {"role": "assistant", "content": [TEXT, TOOL_USE]}
    assert replay["messages"][2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": TOOL_USE["id"],
                "content": '{"occupancy": "0.7000"}',
                "is_error": False,
            }
        ],
    }


# --- R2.7I: the default on the wire, and headers from the environment --------------------------


def test_the_default_sends_thinking_disabled_on_the_wire() -> None:
    endpoint = Endpoint(message(TEXT))

    real_adapter(endpoint).complete(ask())

    [body] = endpoint.sent
    assert body["thinking"] == {"type": "disabled"}


#: Sentinel header lines. Never real; the tests check they are never echoed.
INJECTED = "X-Probe-R27i: SENTINEL-HEADER-VALUE-9f3"


def test_the_installed_sdk_really_applies_environment_headers_over_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The threat, shown with the real SDK: headers from the environment reach a bare client and
    are merged AFTER its own, so one can even replace the API key header."""
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", f"{INJECTED}\nX-Api-Key: SENTINEL-OTHER-KEY")

    bare = anthropic.Anthropic(api_key=TEST_KEY, base_url=DEFAULT_BASE_URL, max_retries=0)

    headers = bare.default_headers
    assert headers["X-Probe-R27i"] == "SENTINEL-HEADER-VALUE-9f3"
    assert headers["X-Api-Key"] == "SENTINEL-OTHER-KEY"


def test_the_adapter_refuses_the_real_client_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.llm.providers.anthropic_provider import ENVIRONMENT_HEADERS_MESSAGE

    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", INJECTED)
    model = AnthropicChatModel(api_key=TEST_KEY, model=MODEL)  # a REAL client, built on use

    with pytest.raises(ValueError) as refused:
        model.complete(ask())

    assert str(refused.value) == ENVIRONMENT_HEADERS_MESSAGE
    assert "SENTINEL-HEADER-VALUE-9f3" not in str(refused.value)
    assert model._built is None  # discarded; `no_network` proves no socket was opened


def test_without_the_variable_the_real_client_passes_the_check() -> None:
    """Guards the fail-closed check against SDK drift: on the pinned SDK the attribute it reads
    exists and is empty, so a clean environment is not refused."""
    from app.llm.providers.anthropic_provider import _carries_environment_headers

    model = AnthropicChatModel(api_key=TEST_KEY, model=MODEL)
    client = model._client()

    assert client._custom_headers == {}
    assert _carries_environment_headers(client) is False
    assert str(client.base_url).startswith(DEFAULT_BASE_URL)
