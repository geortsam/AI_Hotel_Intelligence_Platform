"""``POST /hotels/{hotel_public_id}/copilot/ask`` -- one question, one labelled answer (Stage 7.7).

A thin route, like every other: it declares who may call it, charges the budget, and hands the
validated question to `CopilotService`. The design lives in that service's docstring; the
contract a client relies on lives in the OpenAPI description below and in
`app.schemas.copilot`.

**The order is the security property.** `require_copilot_member` resolves the hotel through the
access policy (401 without a token; the hotel's 404 for a non-member, identical to "no such
hotel"); `copilot_budget` depends on that same dependency and so runs strictly after it, charging
the caller's allowance and then the hotel's. Only then does the service run. A non-member can
therefore neither spend a property's allowance nor learn from a 429 that it exists.

No ``app.llm`` import here: the route knows the service and the schemas, and nothing about
models, providers or prompts.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, status

from app.api.deps import CopilotServiceDep, copilot_budget, require_copilot_member
from app.schemas.common import ErrorResponse
from app.schemas.copilot import MAX_QUESTION_LENGTH, CopilotAnswerResponse, CopilotAskCreate

router = APIRouter(prefix="/hotels/{hotel_public_id}/copilot", tags=["copilot"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the hotel.")]

RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "No hotel with this public identifier exists, or the caller is not a member of it. "
            "The two are deliberately indistinguishable, and neither charges any allowance."
        ),
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            f'The body is not exactly `{{"question": ...}}` with 1-{MAX_QUESTION_LENGTH} '
            "characters after trimming. Any other field is refused, not ignored."
        ),
    },
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": ErrorResponse,
        "description": (
            "`LLM_BUDGET_EXHAUSTED`: the caller's or the hotel's hourly copilot allowance is "
            "spent; `Retry-After` gives the seconds until it returns. Also returned, without "
            "`Retry-After`, as `LLM_RATE_LIMITED` when the provider refused for rate, or "
            "`LLM_BUDGET_EXHAUSTED` when a single request exceeded the per-request ceiling -- "
            "in both cases only when no tool had yet run."
        ),
    },
    status.HTTP_502_BAD_GATEWAY: {
        "model": ErrorResponse,
        "description": (
            "`LLM_INVALID_RESPONSE`: the provider's answer was unusable, after one retry, before "
            "any tool had run."
        ),
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": (
            "`LLM_DISABLED` when this deployment has no language model enabled, or "
            "`LLM_UNAVAILABLE` when the provider could not be reached -- including while the "
            "circuit breaker is open -- before any tool had run."
        ),
    },
}


@router.post(
    "/ask",
    response_model=CopilotAnswerResponse,
    status_code=status.HTTP_200_OK,
    summary="Ask the copilot a question about this hotel",
    description=(
        "Answers one question about this hotel using read-only data tools over the hotel's own "
        "analytics, demand forecast and forecast accuracy. Stateless: nothing from a previous "
        "question is remembered. "
        "**The question and the results of the tools the model calls are sent to the "
        "configured external language-model provider.** The question and the answer are not "
        "stored; one accounting record per request is kept, holding the prompt version, the "
        "upstream model, how the request ended and its token and latency cost. "
        "A 200 is either a complete answer (`complete: true`) or a labelled partial one "
        "(`complete: false`, a `stop_reason` and a fixed `notice`): a lookup limit, a second "
        "failed lookup, or a model failure after lookups had run. Every number in `answer` "
        "was returned by a lookup in this request; an answer containing any other number is "
        "withheld and labelled `ungrounded_figures`. "
        "Rate-limited per caller and per hotel, hourly."
    ),
    dependencies=[Depends(require_copilot_member), Depends(copilot_budget)],
    responses=RESPONSES,
)
def ask_copilot(
    hotel_public_id: HotelPath,
    payload: CopilotAskCreate,
    service: CopilotServiceDep,
) -> CopilotAnswerResponse:
    """Delegate. Authorization and the budget have already run; see the module docstring."""
    return service.ask(hotel_public_id, payload.question)
