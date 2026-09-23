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

## The key is read once and never travels

The API key is read from settings at construction and handed to the client. It is never logged,
never placed in an error message, never attached to a response and never put in `diagnostics`. A
test asserts the string does not appear in any raised error or returned object.

## Not exercised against the live API in CI

No test here makes a network call, and none requires a credential — §5.6 and the Stage 7.5
acceptance criteria both say so. What the tests cover is the translation: that a vendor error of
each kind becomes the right declared failure, that the request is shaped as the SDK expects, and
that nothing vendor-specific escapes. Whether the live endpoint behaves as its documentation says
is not something a test in this repository can establish, and this module does not claim it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.llm.base import ChatRequest, ChatResponse, FinishReason, TokenUsage
from app.llm.errors import (
    LlmInvalidResponseError,
    LlmRateLimitedError,
    LlmUnavailableError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    pass

#: The provider's own name for itself, reported on every response for attribution.
PROVIDER_NAME = "anthropic"

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
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
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
            kwargs: dict[str, Any] = {"api_key": self._api_key}
            if self._base_url is not None:
                kwargs["base_url"] = self._base_url
            self._built = anthropic.Anthropic(**kwargs)
        return self._built

    # --- the protocol -------------------------------------------------------------------------

    def complete(self, request: ChatRequest) -> ChatResponse:
        """One call. No retry, no timeout, no validation — the boundary owns all three."""
        client = self._client()
        system, turns = self._split(request)

        started = time.perf_counter()
        try:
            message = client.messages.create(
                model=self._model,
                max_tokens=request.budget.max_output_tokens,
                system=system,
                messages=turns,
                timeout=request.budget.timeout_seconds,
            )
        except Exception as failure:
            raise self._translate(failure) from failure
        elapsed_ms = (time.perf_counter() - started) * 1000

        return ChatResponse(
            text=self._text_of(message),
            parsed=None,
            usage=self._usage_of(message),
            latency_ms=elapsed_ms,
            provider=PROVIDER_NAME,
            model=getattr(message, "model", self._model),
            finish_reason=_FINISH_REASONS.get(getattr(message, "stop_reason", "") or "", "unknown"),
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
        )

    # --- translation --------------------------------------------------------------------------

    @staticmethod
    def _split(request: ChatRequest) -> tuple[str, list[dict[str, str]]]:
        """System turns are a parameter in this API, not a message. Separate them.

        Several system messages are joined rather than dropped or refused: the seam permits them
        and losing one silently would change what the model was asked.
        """
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        return system, turns

    @staticmethod
    def _text_of(message: Any) -> str:
        """Concatenate the text blocks of a content list, ignoring block types we do not model.

        A response with no text block at all is malformed *for this seam* — nothing here
        requested a non-text block — so it becomes the declared invalid-response failure rather
        than an empty string, which a caller would render as a blank answer.
        """
        blocks = getattr(message, "content", None) or []
        parts = [
            getattr(block, "text", "") for block in blocks if getattr(block, "type", None) == "text"
        ]
        text = "".join(parts)
        if not text:
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


__all__ = ["PROVIDER_NAME", "AnthropicChatModel"]
