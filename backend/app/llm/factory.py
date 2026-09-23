"""Settings in, a guarded `ChatModel` out. The one place a vendor is chosen.

Stage 7.5. §5.1: a request "does **not** carry a provider name, a model name or an API key —
those are resolved from settings by the provider factory, so no service ever names a vendor."
This is that factory, and it is the only module above `app/llm/providers/` that knows a vendor
name exists.

    Settings
       |
    build_chat_model()        <- resolves provider, model, key, budget ceiling
       |
    GuardedChatModel          <- §5.7's timeout, retry and budget rules
       |
    AnthropicChatModel        <- the vendor

What comes back is always a `ChatModel`, whether or not the deployment has a provider. With
`llm_enabled` false the guard is built around a model that is never reached and every call
raises `LlmDisabledError` — so a caller written against this seam works identically in a
deployment that has no provider at all, and gets a declared 503 rather than an `AttributeError`
on a `None` it forgot to check.

**No dependency injection wiring here.** Stage 7.5 adds no endpoint, so there is nothing to
inject into: `app/api/deps.py` is untouched. The stage that adds a route adds the `Depends`
provider there, calling this function.
"""

from __future__ import annotations

from app.core.config import Settings
from app.llm.base import ChatModel, ChatRequest, ChatResponse
from app.llm.boundary import GuardedChatModel
from app.llm.errors import LlmDisabledError


class _UnreachableModel:
    """Stands where a provider would, in a deployment that has none.

    Never called: `GuardedChatModel` raises `LlmDisabledError` before it reaches its inner
    model. This exists so the guard always has something to wrap, rather than the type being
    `ChatModel | None` and every future caller having to handle a case the factory could have
    handled once.

    Its `complete` raises the same error anyway, so that a future refactor which stops checking
    the flag first fails loudly rather than making a real call.
    """

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise LlmDisabledError()


def build_chat_model(settings: Settings) -> ChatModel:
    """The application's `ChatModel`, configured and guarded.

    Returns a guarded model in every case. A caller cannot tell from the type whether a provider
    is configured, and should not: the answer is a `LlmDisabledError` at call time, which is the
    documented behaviour and the one a route will translate into its 503.

    An enabled deployment with no API key is a **misconfiguration**, not a disabled one, and is
    refused here at construction rather than at the first request. A deployment that has said
    `llm_enabled=true` has asked for a working provider; discovering at 3am that it has been
    answering `LLM_DISABLED` for a week because a secret was not mounted is the failure mode this
    check exists to prevent.
    """
    if not settings.llm_enabled:
        return GuardedChatModel(_UnreachableModel(), enabled=False)

    if not settings.llm_api_key:
        raise ValueError(
            "llm_enabled is true but llm_api_key is not set. Set the key, or set "
            "llm_enabled=false to run without a provider."
        )

    provider = _build_provider(settings)
    return GuardedChatModel(
        provider,
        enabled=True,
        max_output_tokens=settings.llm_max_output_tokens,
    )


def _build_provider(settings: Settings) -> ChatModel:
    """Construct the configured adapter.

    The import is deferred so that `app.llm.factory` -- which a future `deps.py` will import at
    module scope -- does not drag the adapter, and through it the vendor SDK's import path, into
    a process that has no provider configured.

    `llm_provider` is a `Literal`, so an unknown value is a settings validation error long
    before it reaches this function. The `raise` below is therefore unreachable in practice and
    is here so that adding a second literal without adding its branch fails immediately.
    """
    if settings.llm_provider == "anthropic":
        from app.llm.providers.anthropic_provider import AnthropicChatModel

        return AnthropicChatModel(
            api_key=settings.llm_api_key or "",
            model=settings.llm_model,
            base_url=settings.llm_base_url,
        )
    raise ValueError(f"No adapter is built for provider {settings.llm_provider!r}.")


__all__ = ["build_chat_model"]
