"""The local adapter: a model served by Ollama on THIS machine (R2.7O).

Like the Anthropic adapter, this module is translation and nothing else: no retry, no deadline of
its own, no structured-output validation and no budget -- `app.llm.boundary.GuardedChatModel`
applies all four once for every provider. Above this file the application knows only
`ChatModel`, `ChatRequest` and `ChatResponse`.

## The one module in `app.llm` that opens a connection itself

Ollama has no SDK in this repository: the adapter speaks its native HTTP API, `POST /api/chat`,
with the standard library's `urllib.request`, so the deployed image gains no dependency. That
makes this the single, named exemption to the package's transport ban
(`tests/backend/test_llm_boundary.py`): no other module in `app.llm` may import `urllib.request`,
`http.client`, `socket`, `httpx`, `requests`, `aiohttp`, `urllib3` or `ollama`. The exemption is
narrow by construction:

- **Loopback only.** The base URL must be http(s) on `127.0.0.1`, `localhost` or `[::1]`
  (`app.core.config.is_loopback_url`): refused when the settings load with the copilot
  enabled on `ollama`, and always again here, at construction.
- **No proxy.** The opener is built with an EMPTY `ProxyHandler`, so no proxy setting -- from
  the process environment or the operating system -- can route a request elsewhere.
- **No redirect.** A 3xx is refused, not followed: a local server cannot send a request on.
- **No credential.** No key exists for this provider, and no authorization header is sent.

## Translation

Request: system turns join into one leading `system` message; an assistant turn that made calls
carries `tool_calls`; a `tool` turn names the tool it answers (`tool_name`, resolved from the
call it answers in the same request). A failed call is already an `{"error": ...}` object in its
content, so nothing is lost by Ollama having no error flag. `options` always carries `num_ctx`
-- the server's own default window may be far smaller than a prompt with a tool catalogue --
`num_predict` (the request's output budget), `temperature`, `seed` and `presence_penalty: 0`.
`think: false` is sent when thinking is disabled; under `provider_default` it is omitted.

Response: `content` is the text; a `thinking` field is never text. Each tool call becomes a
`ToolCall`: the server's id when it gives one, else a deterministic `call_<n>_<i>` (`n` = the
number of messages in the request, `i` = the call's position), unique within a conversation.
String arguments are parsed as JSON; anything that is not then a JSON object becomes `{}`, which
the registry's schema check refuses -- the declared path for bad arguments. A call cut off by the
token limit, duplicate call ids, or an answer with neither text nor calls is the invalid-response
failure.

## Failures

Connection refused, a timeout, a redirect, any non-2xx status or any other transport fault is
`LlmUnavailableError`; a body that is not the documented shape (or exceeds `MAX_RESPONSE_BYTES`)
is `LlmInvalidResponseError`.

*R2.8M.* Each rejection carries `InvalidResponseDiagnostics`: a reason naming the branch that
raised it (`oversized_response`, `malformed_json`, `malformed_structure`, `incomplete_response`,
`non_string_text`, `length_with_tool_calls`, `empty_response`, `malformed_tool_calls`,
`duplicate_tool_call_ids`) and what the parsed body safely says -- Ollama's `done_reason` if it is
a short identifier, `eval_count`, how many calls were parsed, and whether text or calls were
present. Never the body, the text or an argument. Nothing is logged here; the boundary logs the
reason once.

The transport is given the request's own budget as its timeout, but `urllib` applies that timeout
to each socket operation -- connecting, then each read -- not to the request as a whole. It is
not a total deadline: an attempt can outlive the budget, up to about twice it in the worst case
(a slow connect, then a slow read). The boundary still enforces the real deadline and abandons
the attempt; the abandoned connection then closes when its own socket operation times out.
Ollama's non-streaming reply arrives in one piece at the end, so in practice the read timeout
fires near the budget. (Not changed in R2.7T; documented after R2.7S.)

What a real model answers, how well and how fast is NOT established by any test here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from app.core.config import INVALID_OLLAMA_URL_MESSAGE, is_loopback_url
from app.llm.base import (
    RESPONSE_MODEL_KEY,
    ChatRequest,
    ChatResponse,
    FinishReason,
    Message,
    TokenUsage,
    ToolCall,
)
from app.llm.errors import (
    InvalidResponseDiagnostics,
    LlmInvalidResponseError,
    LlmUnavailableError,
    safe_completion_reason,
)

#: The provider's name for itself, reported on every response for attribution.
PROVIDER_NAME = "ollama"

#: The native chat endpoint, relative to the base URL.
CHAT_PATH = "/api/chat"

#: The default base URL: the local server on its standard port.
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

#: Supported `LLM_THINKING` values, as in the Anthropic adapter. The first is the default.
THINKING_MODES = ("disabled", "provider_default")

#: Why a thinking mode was refused. Fixed text, naming only the supported values.
INVALID_THINKING_MESSAGE = "LLM_THINKING must be one of: disabled, provider_default."

#: Always sent: the model's own default (1.5 for qwen3.5) penalises repeating tokens -- figures
#: copied from a tool result included.
PRESENCE_PENALTY = 0

#: The largest response body read. Anything longer is refused as malformed, not truncated.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Ollama's `done_reason` values that mean the answer was cut off at the output budget.
_LENGTH_REASONS = frozenset({"length"})


class Transport(Protocol):
    """Sends one JSON body and returns the raw response body. Injectable for tests."""

    def post_json(self, url: str, body: bytes, timeout: float) -> bytes: ...


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx then surfaces as an `HTTPError` and is refused."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def build_loopback_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """The only opener this adapter uses: no proxy from anywhere, and no redirect followed.

    `ProxyHandler({})` is explicit and empty, so `urllib` does not consult the process or the
    operating system for a proxy. Extra *handlers* exist for tests, which serve canned responses
    without a socket; they never replace the two rules above.
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RefuseRedirects(), *handlers
    )


class UrllibTransport:
    """POST over the loopback opener. No authorization header is ever added.

    `timeout` bounds each socket operation (connect, each read), not the whole request -- see
    the module docstring.
    """

    def __init__(self, opener: urllib.request.OpenerDirector | None = None) -> None:
        self._opener = opener if opener is not None else build_loopback_opener()

    def post_json(self, url: str, body: bytes, timeout: float) -> bytes:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with self._opener.open(request, timeout=timeout) as response:
            raw: bytes = response.read(MAX_RESPONSE_BYTES + 1)
        return raw


class OllamaChatModel:
    """A `ChatModel` backed by a local Ollama server's native chat API."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_OLLAMA_URL,
        num_ctx: int = 16_384,
        temperature: float = 0.0,
        seed: int = 7151,
        thinking: str = "disabled",
        transport: Transport | None = None,
    ) -> None:
        if not is_loopback_url(base_url):
            raise ValueError(INVALID_OLLAMA_URL_MESSAGE)
        if thinking not in THINKING_MODES:
            raise ValueError(INVALID_THINKING_MESSAGE)
        self._model = model
        self._url = base_url.rstrip("/") + CHAT_PATH
        self._num_ctx = num_ctx
        self._temperature = temperature
        self._seed = seed
        self._thinking = thinking
        self._transport: Transport = transport if transport is not None else UrllibTransport()

    # --- the protocol -------------------------------------------------------------------------

    def complete(self, request: ChatRequest) -> ChatResponse:
        """One call. No retry, no deadline of its own, no validation -- the boundary owns them."""
        body = json.dumps(self.payload(request), ensure_ascii=False).encode("utf-8")
        started = time.perf_counter()
        try:
            raw = self._transport.post_json(self._url, body, request.budget.timeout_seconds)
        except urllib.error.HTTPError as failure:
            # Any non-2xx -- a refused redirect included: the server answered, but not usefully.
            raise LlmUnavailableError() from failure
        except Exception as failure:
            # Refused or dropped connection, timeout, a truncated read: whatever went wrong on
            # the way, it is the conservative declared failure, as in the Anthropic adapter.
            raise LlmUnavailableError() from failure
        elapsed_ms = (time.perf_counter() - started) * 1000
        if len(raw) > MAX_RESPONSE_BYTES:
            raise _rejection("oversized_response")
        return self._response(request, self._decode(raw), elapsed_ms)

    # --- request ------------------------------------------------------------------------------

    def payload(self, request: ChatRequest) -> dict[str, Any]:
        """The exact JSON body sent for *request*. Public so a test can pin it."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages(request.messages),
            "stream": False,
            "options": {
                "num_ctx": self._num_ctx,
                "num_predict": request.budget.max_output_tokens,
                "temperature": self._temperature,
                "seed": self._seed,
                "presence_penalty": PRESENCE_PENALTY,
            },
        }
        if self._thinking == "disabled":
            body["think"] = False
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": dict(spec.input_schema),
                    },
                }
                for spec in request.tools
            ]
        return body

    @staticmethod
    def _messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        names: dict[str, str] = {}
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "assistant" and m.tool_calls:
                for call in m.tool_calls:
                    names[call.id] = call.name
                out.append(
                    {
                        "role": "assistant",
                        "content": m.content,
                        "tool_calls": [
                            {"function": {"name": call.name, "arguments": dict(call.arguments)}}
                            for call in m.tool_calls
                        ],
                    }
                )
            elif m.role == "tool":
                name = names.get(m.tool_call_id or "")
                if name is None:
                    # The loop always sends a result after the call it answers; one that names
                    # no earlier call is a programming error, not something to guess around.
                    raise ValueError("A tool result names no earlier call in this request.")
                out.append({"role": "tool", "content": m.content, "tool_name": name})
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    # --- response -----------------------------------------------------------------------------

    @staticmethod
    def _decode(raw: bytes) -> Mapping[str, Any]:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as malformed:
            raise _rejection("malformed_json") from malformed
        if not isinstance(body, dict):
            raise _rejection("malformed_structure")
        if not isinstance(body.get("message"), dict):
            raise _rejection("malformed_structure", body)
        if body.get("done") is False:
            raise _rejection("incomplete_response", body)
        return body

    def _response(
        self, request: ChatRequest, body: Mapping[str, Any], elapsed_ms: float
    ) -> ChatResponse:
        message: Mapping[str, Any] = body["message"]
        text = message.get("content") or ""
        if not isinstance(text, str):
            raise _rejection("non_string_text", body)
        try:
            calls = self._calls(message.get("tool_calls"), turn=len(request.messages))
        except LlmInvalidResponseError as rejected:
            # The call parser knows the reason; the body adds what it safely reports.
            raise LlmInvalidResponseError(
                diagnostics=replace(
                    rejected.diagnostics,
                    completion_reason=safe_completion_reason(body.get("done_reason")),
                    output_tokens=_reported(body.get("eval_count")),
                    has_text=bool(text),
                )
            ) from rejected
        done_reason = body.get("done_reason")
        if calls and done_reason in _LENGTH_REASONS:
            # Cut off while writing a call: its arguments may be truncated (§5.5).
            raise _rejection(
                "length_with_tool_calls",
                body,
                tool_calls_parsed=len(calls),
                has_tool_calls=True,
                has_text=bool(text),
            )
        if not text and not calls:
            # Nothing to show and nothing to run -- a thinking-only answer included.
            raise _rejection(
                "empty_response", body, tool_calls_parsed=0, has_tool_calls=False, has_text=False
            )
        returned = body.get("model")
        named = returned if isinstance(returned, str) and returned else None
        return ChatResponse(
            text=text,
            parsed=None,
            usage=TokenUsage(
                input_tokens=_count(body.get("prompt_eval_count")),
                output_tokens=_count(body.get("eval_count")),
            ),
            latency_ms=elapsed_ms,
            provider=PROVIDER_NAME,
            model=named or self._model,
            finish_reason=_finish_reason(done_reason, bool(calls)),
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            tool_calls=calls,
            diagnostics={} if named is None else {RESPONSE_MODEL_KEY: named},
        )

    @staticmethod
    def _calls(raw: object, *, turn: int) -> tuple[ToolCall, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list):
            raise _rejection("malformed_tool_calls", has_tool_calls=True)
        calls: list[ToolCall] = []
        for index, entry in enumerate(raw):
            function = entry.get("function") if isinstance(entry, dict) else None
            if not isinstance(function, dict):
                raise _rejection(
                    "malformed_tool_calls", tool_calls_parsed=len(calls), has_tool_calls=True
                )
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise _rejection(
                    "malformed_tool_calls", tool_calls_parsed=len(calls), has_tool_calls=True
                )
            given = entry.get("id")
            call_id = given if isinstance(given, str) and given else f"call_{turn}_{index}"
            calls.append(
                ToolCall(id=call_id, name=name, arguments=_arguments(function.get("arguments")))
            )
        if len({call.id for call in calls}) != len(calls):
            # Results could not be paired back to their calls.
            raise _rejection(
                "duplicate_tool_call_ids", tool_calls_parsed=len(calls), has_tool_calls=True
            )
        return tuple(calls)


def _reported(raw: object) -> int | None:
    """A count the provider reported, or `None` when it reported none -- never a default."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


def _rejection(
    reason: str,
    body: Mapping[str, Any] | None = None,
    *,
    tool_calls_parsed: int | None = None,
    has_tool_calls: bool | None = None,
    has_text: bool | None = None,
) -> LlmInvalidResponseError:
    """The declared failure for *reason*, with what the parsed *body* safely says (R2.8M): its
    completion reason if it is a short identifier, and its output-token count. No content."""
    return LlmInvalidResponseError(
        diagnostics=InvalidResponseDiagnostics(
            reason=reason,
            provider=PROVIDER_NAME,
            completion_reason=None
            if body is None
            else safe_completion_reason(body.get("done_reason")),
            output_tokens=None if body is None else _reported(body.get("eval_count")),
            tool_calls_parsed=tool_calls_parsed,
            has_tool_calls=has_tool_calls,
            has_text=has_text,
        )
    )


def _arguments(raw: object) -> dict[str, Any]:
    """An argument object, or `{}` -- which the registry's schema check then refuses."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _count(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


def _finish_reason(done_reason: object, has_calls: bool) -> FinishReason:
    if has_calls:
        return "tool_use"
    if done_reason == "stop":
        return "stop"
    if done_reason in _LENGTH_REASONS:
        return "length"
    return "unknown"


__all__ = [
    "CHAT_PATH",
    "DEFAULT_OLLAMA_URL",
    "INVALID_THINKING_MESSAGE",
    "MAX_RESPONSE_BYTES",
    "PRESENCE_PENALTY",
    "PROVIDER_NAME",
    "THINKING_MODES",
    "OllamaChatModel",
    "Transport",
    "UrllibTransport",
    "build_loopback_opener",
]
