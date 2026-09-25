"""Records one copilot request in `llm_invocations`, with the caller already bound (Stage 7.7).

The same shape as :class:`app.services.audit.AuditTrail`, deliberately:

- **the actor is bound at construction**, by the dependency chain that resolved the bearer
  token (`app.api.deps.get_llm_invocation_log`), so the copilot service records a row without
  naming an identity and therefore cannot name the wrong one;
- **the request id is read from the existing ContextVar**, so no second correlation system
  exists and the row joins the request's `tool.invoked` audit events and its log lines;
- **it stages, it does not commit.** The row joins the copilot service's open transaction and
  commits when that service commits.

What it is handed is counts, identities and costs. There is no parameter through which a
question, an answer, a prompt, a tool output or a credential could arrive, and a test pins the
signature.
"""

from __future__ import annotations

from app.core.request_id import current_request_id
from app.models.llm_invocation import LlmInvocation
from app.models.user import User
from app.repositories.llm_invocation import LlmInvocationRepository


class LlmInvocationLog:
    """Stages invocation rows on the caller's transaction, attributed to the bound actor."""

    def __init__(self, repository: LlmInvocationRepository, actor: User) -> None:
        self._repository = repository
        self._actor = actor

    def record(
        self,
        *,
        hotel_id: int,
        prompt_id: str,
        prompt_version: str,
        provider: str,
        model: str,
        stop_reason: str,
        error_code: str | None,
        rounds: int,
        model_calls: int,
        tool_calls: int,
        tool_failures: int,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ) -> LlmInvocation:
        """Stage one row. Called before the copilot service commits."""
        return self._repository.add(
            LlmInvocation(
                hotel_id=hotel_id,
                actor_user_id=self._actor.id,
                prompt_id=prompt_id,
                prompt_version=prompt_version,
                provider=provider,
                model=model,
                stop_reason=stop_reason,
                complete=stop_reason == "completed",
                error_code=error_code,
                rounds=rounds,
                model_calls=model_calls,
                tool_calls=tool_calls,
                tool_failures=tool_failures,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                request_id=current_request_id(),
            )
        )


__all__ = ["LlmInvocationLog"]
