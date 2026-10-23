"""The one real adapter. The only module in this repository permitted to name a vendor SDK.

Stage 7.5. §5.1: "**No service imports a provider SDK.** The import closure of
`app/services/copilot.py` reaches `app/llm/base.py` and nothing vendor-specific — the same
discipline `app.ml.artifact_store` uses for `ml.artifact`, and testable the same way."

This is the other end of that discipline. Everything vendor-shaped stops here: the client type,
the exception classes, the message format, the way a model name is spelled. Above this file the
application knows only `ChatModel`, `ChatRequest` and `ChatResponse`.

## The import is deferred, and that is the whole dependency story

`anthropic` is **not** in `backend/requirements.txt` and is therefore not in the deployed image.
It lives in `backend/requirements-llm.txt`, installed only where someone wants a live provider.
So this module must be importable without it — and it is: the SDK is imported inside
:meth:`AnthropicChatModel._client`, not at module scope.

That is exactly the arrangement `app.ml.artifact_store` uses for `ml.artifact`, and for the same
reason: in a runtime that does not carry the dependency, the failure has to be a served, declared
error rather than an `ImportError` at startup that takes every unrelated endpoint down with it. A
test imports this module with the SDK absent and asserts it succeeds.

## Translation, and nothing else

This adapter does not retry, does not time itself out, does not validate structured output and
does not enforce a budget. All four are §5.7 rules that `app.llm.boundary.GuardedChatModel`
applies once for every provider — an adapter that implemented them too would be a second copy to
keep in step. Its whole job is: build the vendor's request, make the call, map the vendor's
failures onto the declared taxonomy, and return a `ChatResponse`.

**The SDK's own retries are switched off** (`SDK_MAX_RETRIES = 0`, R2.6B). The client used to be
built without the setting, so the SDK's default retry policy -- unexamined then, because neither
CI nor the image installs the SDK -- could turn one `complete` into several billed HTTP requests
that nothing here counted. Now one attempt is one HTTP request, and the boundary's §5.7 policy is
the only retry. That does not make a failed call free: a request the provider received may still
be billed, and a timed-out attempt is abandoned rather than cancelled (see the boundary).

## Which model answered

`ChatResponse.model` keeps its long-standing meaning for the application -- the provider's
model id, or the configured one when the response names none. The id the provider ACTUALLY
returned is also put in `diagnostics["response_model"]`, and only when the response carried
one, so an evaluation can tell the configured name from the served model and record a missing
one as unknown instead of copying the configuration.

## The key is read once and never travels

The API key is read from settings at construction and handed to the client. It is never logged,
never placed in an error message, never attached to a response and never put in `diagnostics`. A
test asserts the string does not appear in any raised error or returned object.

## Not exercised against the live API in CI

No test here makes a network call, and none requires a credential — §5.6 and the Stage 7.5
acceptance criteria both say so. What the tests cover is the translation: that a vendor error of
each kind becomes the right declared failure, that the request is shaped as the SDK expects, and
that nothing vendor-specific escapes. Where the optional SDK is installed -- a developer machine,
not CI or the image -- `tests/backend/test_anthropic_sdk_compat.py` also runs the real SDK's
request building and response parsing over an in-memory transport; CI skips it. Whether the live
endpoint behaves as its documentation says is not something a test in this repository can
establish, and this module does not claim it.

## Tool use (Stage 7.6)

§5.4: "The provider adapter translates the registry's contracts into whatever the vendor's tool
format is." Translation only, in both directions:

- `ChatRequest.tools` becomes the `tools` parameter — name, description, `input_schema` — and is
  omitted entirely when the request offers none, so a Stage 7.5 request is sent unchanged.
- An assistant turn that made calls becomes a content list of `tool_use` blocks, and a run of
  consecutive `tool` turns becomes **one** user turn of `tool_result` blocks, because this API
  requires every result for one assistant turn to arrive together.
- `tool_use` blocks in the answer become `ToolCall`s, verbatim. The adapter does not check the
  name against anything or validate the arguments: it has no registry and must not grow one.

An answer that asked for tools may carry no text, which is not malformed. An answer cut off at
the token limit **while** asking for a tool is: its arguments may be truncated, and a partial
argument object is exactly the coerced guess §5.5 forbids, so it is the invalid-response failure.

## The endpoint is chosen here, never by the environment (R2.7D)

The client is **always** built with an explicit `base_url`: the configured `LLM_BASE_URL` when
one is set, otherwise `DEFAULT_BASE_URL`. Left unset, the SDK would read `ANTHROPIC_BASE_URL`
from the process environment -- a variable this application does not own, which another tool in
the same shell may have set -- and send the API key to whatever it names. That fallback is
never reached.

A configured `LLM_BASE_URL` must be an absolute `https://` URL with a host, and no credentials,
query, fragment or surrounding whitespace; a path prefix and a port are allowed. It is passed to
the SDK exactly as written -- never trimmed, lower-cased or otherwise rewritten -- and anything
else is refused when the adapter is constructed, before a client exists, with a message that does
not echo the value (a URL can carry credentials).

## Extended thinking is a setting, off by default, and never replayed (R2.7F, R2.7I)

`LLM_THINKING` is one of `THINKING_MODES`. `disabled` -- the default since R2.7I -- sends
`thinking={"type": "disabled"}` on every request. `provider_default` sends no `thinking`
parameter, so the model's own default applies; it is an explicit opt-in only. Anything else is
refused at construction, before a client exists.

Thinking blocks in an answer are never text: `_text_of` reads only `text` blocks and `_calls_of`
only `tool_use` blocks, so a `thinking` or `redacted_thinking` block reaches no caller. Nor are
they kept: an assistant turn that made calls is replayed as its text and its `tool_use` blocks
only. The provider documents that thinking blocks within a tool-use turn must be passed back
complete and unmodified, so **tool use with thinking on is not supported here**: under
`provider_default`, a model that thinks by default would have its turns replayed without them.
That combination is neither implemented nor verified, and must not be described as supported.
`disabled` asks for no thinking blocks, which is why it is the default. What the live API does
with it is not verified by this repository either -- the tests establish only what is sent.

## Headers from the environment are refused (R2.7I)

The SDK reads `ANTHROPIC_CUSTOM_HEADERS` from the process environment itself and merges those
headers into every request, after the application's own. This adapter passes no headers of its
own, so a built client that carries any came from the environment; it is discarded and the call
is refused with a fixed message that names no header and no value. `Settings` refuses an enabled
copilot with the variable set before this is reached; this is the second check, at the moment
the SDK actually reads it. The adapter itself never reads the environment.
"""

from __future__ import annotations

import importlib.metadata
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from app.llm.base import (
    RESPONSE_MODEL_KEY,
    ChatRequest,
    ChatResponse,
    FinishReason,
    TokenUsage,
    ToolCall,
)
from app.llm.errors import (
    LlmInvalidResponseError,
    LlmRateLimitedError,
    LlmUnavailableError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    pass

#: The provider's own name for itself, reported on every response for attribution.
PROVIDER_NAME = "anthropic"

#: The installed distribution whose version an evaluation records (`installed_sdk_version`).
SDK_DISTRIBUTION = "anthropic"

#: Passed to the client explicitly. Zero: the boundary owns retries (see the module docstring).
SDK_MAX_RETRIES = 0

#: The endpoint used when `LLM_BASE_URL` is not configured. Passed explicitly, so the SDK's own
#: `ANTHROPIC_BASE_URL` environment fallback can never select one (module docstring).
DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Why a configured `LLM_BASE_URL` was refused. Fixed text: the value itself is never echoed.
INVALID_BASE_URL_MESSAGE = (
    "LLM_BASE_URL must be an absolute https:// URL with a host and no credentials, query, "
    "fragment or surrounding whitespace."
)

#: The supported `LLM_THINKING` values. The first is the default (R2.7I); the second sends no
#: `thinking` parameter at all.
THINKING_MODES = ("disabled", "provider_default")

#: Why a thinking mode was refused. Fixed text, naming only the supported values.
INVALID_THINKING_MESSAGE = "LLM_THINKING must be one of: disabled, provider_default."

#: Why a built client was discarded. Fixed text: no header name or value is ever included.
ENVIRONMENT_HEADERS_MESSAGE = (
    "The provider client carries headers this application did not set, taken from "
    "ANTHROPIC_CUSTOM_HEADERS in the environment. Unset it; no request was sent."
)

#: What `disabled` sends, verbatim, as the SDK's `thinking` parameter.
THINKING_DISABLED = {"type": "disabled"}


def resolve_base_url(configured: str | None) -> str:
    """The endpoint the client is built with: the configured one if valid, else the default.

    `None` -- nothing configured -- is the default. Every other value is checked and returned
    unchanged, or refused with `ValueError`. An empty string is refused rather than read as
    "unset": it is a configured value that names no endpoint.
    """
    if configured is None:
        return DEFAULT_BASE_URL
    if not _is_absolute_https_url(configured):
        raise ValueError(INVALID_BASE_URL_MESSAGE)
    return configured


def _is_absolute_https_url(value: str) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        return False
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a malformed or out-of-range port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
        and "?" not in value
        and "#" not in value
    )


#: How the vendor's stop reasons map onto this seam's normalised vocabulary.
#:
#: Written out rather than passed through, so a value the vendor adds later becomes `unknown`
#: here instead of appearing in an application's response as a string it has never seen. A
#: caller branching on a `finish_reason` this repository has not defined is a caller coupled to
#: a release note.
_FINISH_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "refusal": "content_filter",
    "tool_use": "tool_use",
}


class AnthropicChatModel:
    """A `ChatModel` backed by the Anthropic Messages API.

    Satisfies the protocol structurally: it does not import or inherit from `app.llm.base`'s
    `ChatModel`, it simply has `complete`. That is the point of a `Protocol` — see `base.py`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        thinking: str = "disabled",
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        #: Resolved and validated now, so an invalid endpoint is refused before any client
        #: exists, and the client is never built without one.
        self._base_url = resolve_base_url(base_url)
        #: Checked now for the same reason. See the module docstring.
        if thinking not in THINKING_MODES:
            raise ValueError(INVALID_THINKING_MESSAGE)
        self._thinking = thinking
        #: Injectable so a test can supply a stand-in with the SDK's shape and never import the
        #: SDK. When None, `_client` builds a real one on first use.
        self._injected = client
        self._built: Any | None = None

    # --- the vendor boundary ------------------------------------------------------------------

    def _client(self) -> Any:
        """Build the vendor client, importing the SDK here and nowhere else.

        The import is inside this method rather than at module scope so that the module can be
        imported in a runtime that does not carry the dependency — which is every runtime this
        repository currently ships. See the module docstring.
        """
        if self._injected is not None:
            return self._injected
        if self._built is None:
            try:
                import anthropic
            except ImportError as missing:
                # Not an `ImportError` escaping to a caller: a runtime without the SDK is a
                # runtime where the assistant is unavailable, which is a declared failure.
                raise LlmUnavailableError() from missing
            # `base_url` is always passed: never let the SDK fall back to ANTHROPIC_BASE_URL.
            built = anthropic.Anthropic(
                api_key=self._api_key,
                base_url=self._base_url,
                max_retries=SDK_MAX_RETRIES,
            )
            if _carries_environment_headers(built):
                raise ValueError(ENVIRONMENT_HEADERS_MESSAGE)
            self._built = built
        return self._built

    # --- the protocol -------------------------------------------------------------------------

    def complete(self, request: ChatRequest) -> ChatResponse:
        """One call. No retry, no timeout, no validation — the boundary owns all three."""
        client = self._client()
        system, turns = self._split(request)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.budget.max_output_tokens,
            "system": system,
            "messages": turns,
            "timeout": request.budget.timeout_seconds,
        }
        if self._thinking == "disabled":
            # A fresh copy per request: nothing downstream can alter the constant.
            kwargs["thinking"] = dict(THINKING_DISABLED)
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": dict(spec.input_schema),
                }
                for spec in request.tools
            ]

        started = time.perf_counter()
        try:
            message = client.messages.create(**kwargs)
        except Exception as failure:
            raise self._translate(failure) from failure
        elapsed_ms = (time.perf_counter() - started) * 1000

        stop_reason = getattr(message, "stop_reason", "") or ""
        returned_model = getattr(message, "model", None)
        named = returned_model if isinstance(returned_model, str) and returned_model else None
        calls = self._calls_of(message)
        if calls and stop_reason == "max_tokens":
            # Cut off while writing a call: the arguments may be truncated. See module docstring.
            raise LlmInvalidResponseError()

        return ChatResponse(
            text=self._text_of(message, allow_empty=bool(calls)),
            parsed=None,
            usage=self._usage_of(message),
            latency_ms=elapsed_ms,
            provider=PROVIDER_NAME,
            model=named or self._model,
            finish_reason=_FINISH_REASONS.get(stop_reason, "unknown"),
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            tool_calls=calls,
            diagnostics={} if named is None else {RESPONSE_MODEL_KEY: named},
        )

    # --- translation --------------------------------------------------------------------------

    @staticmethod
    def _split(request: ChatRequest) -> tuple[str, list[dict[str, Any]]]:
        """System turns are a parameter in this API, not a message. Separate them.

        Several system messages are joined rather than dropped or refused: the seam permits them
        and losing one silently would change what the model was asked.

        Tool turns are merged: every consecutive `tool` message becomes one `tool_result` block
        in a single user turn, which is the shape this API requires.
        """
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        turns: list[dict[str, Any]] = []
        for m in request.messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                result = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.content,
                    "is_error": m.is_error,
                }
                previous = turns[-1] if turns else None
                if (
                    previous is not None
                    and previous["role"] == "user"
                    and isinstance(previous["content"], list)
                    and all(block.get("type") == "tool_result" for block in previous["content"])
                ):
                    previous["content"].append(result)
                else:
                    turns.append({"role": "user", "content": [result]})
                continue
            if m.role == "assistant" and m.tool_calls:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": dict(call.arguments),
                    }
                    for call in m.tool_calls
                )
                turns.append({"role": "assistant", "content": blocks})
                continue
            turns.append({"role": m.role, "content": m.content})
        return system, turns

    @staticmethod
    def _calls_of(message: Any) -> tuple[ToolCall, ...]:
        """The `tool_use` blocks of an answer, as untrusted `ToolCall`s, in the order given.

        Arguments that are not an object become an empty mapping rather than a crash: the
        registry validates them against the tool's schema, which will refuse whatever is
        missing, and that refusal is the declared path for bad arguments. A call with no id is
        malformed — its result could not be paired back to it — and is the invalid-response
        failure.
        """
        blocks = getattr(message, "content", None) or []
        calls: list[ToolCall] = []
        for block in blocks:
            if getattr(block, "type", None) != "tool_use":
                continue
            call_id = getattr(block, "id", None)
            if not isinstance(call_id, str) or not call_id:
                raise LlmInvalidResponseError()
            arguments = getattr(block, "input", None)
            calls.append(
                ToolCall(
                    id=call_id,
                    name=str(getattr(block, "name", "")),
                    arguments=dict(arguments) if isinstance(arguments, dict) else {},
                )
            )
        return tuple(calls)

    @staticmethod
    def _text_of(message: Any, *, allow_empty: bool = False) -> str:
        """Concatenate the text blocks of a content list, ignoring block types we do not model.

        A response with no text block at all is malformed *for this seam* — unless it asked for
        tools, in which case the calls are the answer — so it becomes the declared
        invalid-response failure rather than an empty string, which a caller would render as a
        blank answer.
        """
        blocks = getattr(message, "content", None) or []
        parts = [
            getattr(block, "text", "") for block in blocks if getattr(block, "type", None) == "text"
        ]
        text = "".join(parts)
        if not text and not allow_empty:
            raise LlmInvalidResponseError()
        return text

    @staticmethod
    def _usage_of(message: Any) -> TokenUsage:
        """Token counts as the provider reported them. Zero when it reported none."""
        usage = getattr(message, "usage", None)
        return TokenUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

    @staticmethod
    def _translate(failure: Exception) -> Exception:
        """Map a vendor exception onto the declared taxonomy of §5.7.

        Matched on class **name** rather than by importing the SDK's exception types, so this
        function works in a runtime without the dependency and a test can exercise every branch
        without installing it. The names are the SDK's public, documented ones.

        Anything unrecognised becomes `LlmUnavailableError` — the conservative choice. A vendor
        failure this adapter has not seen is one it cannot reason about, and treating it as
        retryable-then-503 is the behaviour that neither retries a refusal nor swallows a fault.
        """
        if isinstance(failure, LlmInvalidResponseError | LlmRateLimitedError | LlmUnavailableError):
            return failure

        name = type(failure).__name__
        if name in {"RateLimitError", "TooManyRequestsError"}:
            return LlmRateLimitedError()
        if name in {"APITimeoutError", "APIConnectionError", "InternalServerError"}:
            return LlmUnavailableError()
        if name in {"APIResponseValidationError", "UnprocessableEntityError"}:
            return LlmInvalidResponseError()
        return LlmUnavailableError()


def _carries_environment_headers(client: Any) -> bool:
    """Whether a built client holds custom headers -- which, since this adapter passes none,
    can only have come from `ANTHROPIC_CUSTOM_HEADERS`.

    The SDK stores them in `_custom_headers` (pinned `anthropic==0.125.0`). Fail-closed: a
    client without that attribute -- an SDK that moved it -- counts as carrying them, so the
    check cannot silently stop checking. Only emptiness is examined; nothing is read out.
    """
    custom = getattr(client, "_custom_headers", None)
    return not isinstance(custom, dict) or bool(custom)


def installed_sdk_version() -> str | None:
    """The installed SDK's version, from package metadata, or `None` when it is not installed.

    Read from metadata rather than by importing the SDK, so it answers in a runtime without the
    dependency -- which is every runtime this repository ships.
    """
    try:
        return importlib.metadata.version(SDK_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


__all__ = [
    "DEFAULT_BASE_URL",
    "ENVIRONMENT_HEADERS_MESSAGE",
    "INVALID_BASE_URL_MESSAGE",
    "INVALID_THINKING_MESSAGE",
    "PROVIDER_NAME",
    "RESPONSE_MODEL_KEY",
    "SDK_DISTRIBUTION",
    "SDK_MAX_RETRIES",
    "THINKING_DISABLED",
    "THINKING_MODES",
    "AnthropicChatModel",
    "installed_sdk_version",
    "resolve_base_url",
]
