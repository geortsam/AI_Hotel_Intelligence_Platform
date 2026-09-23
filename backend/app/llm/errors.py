"""The failure taxonomy of `docs/v2-architecture.md` §5.7, as typed errors.

Stage 7.5. Six failures are declared there, once, "so no stage improvises it". Each becomes a
subclass of the existing :class:`app.core.errors.AppError`, which already carries the status
code, the machine-readable code and the client-facing message that the error handler turns into
the one ``ErrorResponse`` envelope this API has always used.

**They are six classes rather than one with a code parameter**, deliberately. A caller that wants
to retry a timeout and refuse a rate limit has to be able to tell them apart with ``except``, and
a single ``LlmError("LLM_RATE_LIMITED")`` makes that a string comparison. The architecture
specifies different behaviour per failure; different behaviour wants different types.

## What a message may not contain

No provider name, no model name, no API key, no prompt text, no request id of the vendor's, no
stack and no upstream body. §5.7: "the response uses the existing ``ErrorResponse`` envelope and
leaks no provider detail, no prompt and no stack." Which vendor failed and why is an operator's
question, answered in the log; a client able to read it would be reading the deployment's
contract with a third party off an error body.

This mirrors :class:`app.core.errors.ModelUnavailableError`, whose docstring makes the same
argument for the demand model's seven distinct causes behind one sentence.

## No endpoint raises any of these yet

Stage 7.5 adds no route. These types exist so that the stage which does add one has a taxonomy to
raise from rather than inventing one under deadline, and so the boundary below
(`app.llm.boundary`) can classify a provider's failure once, in the one place that knows what the
provider did.
"""

from __future__ import annotations

from fastapi import status

from app.core.errors import AppError

#: What every message here refuses to say. Asserted by a test rather than left to review.
GENERIC_LLM_MESSAGE = "The assistant is not available right now."


class LlmError(AppError):
    """Base for every declared language-model failure.

    Exists so that a caller which genuinely wants "any LLM failure" can say so in one
    ``except`` without catching :class:`AppError` and swallowing a 404 with it. Nothing raises
    this class itself.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "LLM_ERROR"
    message = GENERIC_LLM_MESSAGE


class LlmDisabledError(LlmError):
    """``llm_enabled`` is false. §5.7 row 6: 503 with ``LLM_DISABLED``.

    The documented posture for a deployment that has not been given a provider: the application
    starts, every V1 endpoint serves, and the language-model paths answer a clean 503. This is
    the same stance ``ml/demand-forecast`` takes when the artifact is unavailable, and it is why
    the flag is a first-class setting rather than the absence of an API key.

    Not an error in the operational sense. A deployment may run this way forever.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "LLM_DISABLED"
    message = "The assistant is not enabled on this server."


class LlmUnavailableError(LlmError):
    """The provider timed out. §5.7 row 1: one retry with jitter, then 503 ``LLM_UNAVAILABLE``.

    Raised only after the retry has also failed — the boundary owns that rule, so a caller
    seeing this error knows two attempts were made and neither returned.

    503 rather than 504: the request was well-formed and the fault is a dependency this server
    could not reach, which is exactly the argument
    :class:`app.core.errors.DatabaseUnavailableError` makes. A later attempt may succeed.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "LLM_UNAVAILABLE"
    message = GENERIC_LLM_MESSAGE


class LlmRateLimitedError(LlmError):
    """The provider refused for rate. §5.7 row 2: **no retry**; 429 ``LLM_RATE_LIMITED``.

    No retry, explicitly. Retrying a rate limit is how a client turns a brief refusal into a
    longer one, and the provider has already said the answer is "not now".

    429 is the client-facing truth even though the limit is the server's contract with the
    vendor: the request cannot be satisfied now and can be retried later, which is what the
    status means.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "LLM_RATE_LIMITED"
    message = "The assistant is busy. Please try again shortly."


class LlmInvalidResponseError(LlmError):
    """Structured output did not validate. §5.7 row 3: one retry; then 502 ``LLM_INVALID_RESPONSE``.

    §5.5: "A validation failure is a failure, not a coerced guess." The alternative — repairing
    the payload, filling a missing field with a default, or taking the first parse that almost
    works — produces an object the caller cannot distinguish from one the model actually
    returned.

    502 rather than 500: this server worked correctly and an upstream returned something
    unusable, which is what a bad gateway is.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "LLM_INVALID_RESPONSE"
    message = "The assistant returned an unusable answer."


class LlmToolFailedError(LlmError):
    """A tool raised while the model was calling it. §5.7 row 4.

    **Declared, not implemented, and that is deliberate.** Stage 7.5 introduces no tools — there
    is no registry, no contract surface and no orchestration loop for a tool to fail inside — so
    there is nothing here that could raise this. It is declared now because the taxonomy is
    specified as a whole and a later stage inheriting five of six types would be inheriting a
    gap it had to name itself.

    §5.7 gives this row a *loop* behaviour rather than a status: "the tool's error is returned to
    the model once; a second failure ends the loop". That belongs to the orchestration loop of
    Stage 7.6, not to this boundary, and the status below is a placeholder that the stage adding
    tools should revisit against the response it actually wants to send.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "LLM_TOOL_FAILED"
    message = "The assistant could not complete a step of its work."


class LlmBudgetExhaustedError(LlmError):
    """A declared ceiling was reached. §5.7 row 5: 429 ``LLM_BUDGET_EXHAUSTED``.

    §4.5 makes the argument: "An unbounded LLM spend is an availability risk", and a ceiling that
    is hit must produce "a refusal with a clear error code, never a silent degradation".

    **What this covers in Stage 7.5 is the per-request ceiling only** — a request whose declared
    token budget exceeds the configured maximum, or a response whose reported usage exceeded it.
    The per-actor and per-hotel limits §4.5 also names are not here, and cannot be: they are
    keyed by who is asking, and no actor or tenant identifier is permitted to cross this
    boundary. They belong to the stage that owns the endpoint, where the caller is known.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "LLM_BUDGET_EXHAUSTED"
    message = "The assistant has reached its usage limit for this request."


#: Every declared failure, keyed by its documented code. A test walks this against §5.7 so the
#: taxonomy cannot drift from the document that specifies it.
DECLARED_FAILURES: dict[str, type[LlmError]] = {
    "LLM_DISABLED": LlmDisabledError,
    "LLM_UNAVAILABLE": LlmUnavailableError,
    "LLM_RATE_LIMITED": LlmRateLimitedError,
    "LLM_INVALID_RESPONSE": LlmInvalidResponseError,
    "LLM_TOOL_FAILED": LlmToolFailedError,
    "LLM_BUDGET_EXHAUSTED": LlmBudgetExhaustedError,
}


__all__ = [
    "DECLARED_FAILURES",
    "GENERIC_LLM_MESSAGE",
    "LlmBudgetExhaustedError",
    "LlmDisabledError",
    "LlmError",
    "LlmInvalidResponseError",
    "LlmRateLimitedError",
    "LlmToolFailedError",
    "LlmUnavailableError",
]
