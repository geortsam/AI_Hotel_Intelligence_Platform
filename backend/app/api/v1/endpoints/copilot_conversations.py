"""Copilot conversations (Stage 7.11). Five operations, all hotel-scoped, all the caller's own.

    POST   /hotels/{h}/copilot/conversations               start one: answers turn 1      201
    GET    /hotels/{h}/copilot/conversations               the caller's conversations     200
    GET    /hotels/{h}/copilot/conversations/{c}           one transcript                 200
    POST   /hotels/{h}/copilot/conversations/{c}/messages  the next turn                  200
    DELETE /hotels/{h}/copilot/conversations/{c}           delete, turns included         204

Any member may start a conversation; only its creator may read, continue or delete it. Everyone
else -- another viewer, a manager, the owner, another hotel's member -- gets the same 404 an
unknown or expired conversation gets.

The two POSTs ask the model and are charged against the existing copilot allowance: starting one
after membership is proved (`copilot_budget`), continuing one only after the conversation is
proved the caller's own, live and not full (`conversation_turn_budget`). A continued turn is 200,
not 201: it creates no separately addressable resource -- it belongs to the conversation, whose
URL does not change. The design, the retention rule and the context budget are in
`docs/copilot-conversations.md`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import (
    CopilotConversationServiceDep,
    conversation_turn_budget,
    copilot_budget,
    require_copilot_member,
)
from app.schemas.common import ErrorResponse, Page
from app.schemas.copilot_conversation import (
    ConversationQuestionCreate,
    ConversationSummary,
    ConversationTranscript,
    ConversationTurnResponse,
)
from app.services.copilot_conversation import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}/copilot", tags=["copilot"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the hotel.")]
ConversationPath = Annotated[
    uuid.UUID, Path(description="Public identifier of one of the caller's conversations.")
]

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "No such hotel or the caller is not a member of it; or no conversation with this "
            "identifier that the caller created at this hotel, or it has expired. "
            "Indistinguishable by design."
        ),
    }
}
MODEL_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "The question is empty, too long, or the body carries another field.",
    },
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": ErrorResponse,
        "description": (
            "`LLM_BUDGET_EXHAUSTED` (the caller's or the hotel's hourly allowance, with "
            "`Retry-After`) or `LLM_RATE_LIMITED` (the provider). Nothing is stored."
        ),
    },
    status.HTTP_502_BAD_GATEWAY: {
        "model": ErrorResponse,
        "description": "`LLM_INVALID_RESPONSE` before any tool had run. Nothing is stored.",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": (
            "`LLM_DISABLED` or `LLM_UNAVAILABLE` before any tool had run. Nothing is stored."
        ),
    },
}
CONFLICT: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": (
            "`CONVERSATION_FULL` when the conversation already holds 20 turns; or a generic "
            "conflict when another turn was stored while this one was being answered."
        ),
    }
}

WHAT_LEAVES = (
    "**The question, this conversation's earlier questions and answers (at most 6 turns and "
    "12,000 characters, whole turns, citation labels removed) and the results of the tools the "
    "model calls are sent to the configured external language-model provider.** The question "
    "and the answer as served are stored with the conversation, which expires -- turns "
    "included -- 30 days (by default) after it was last used, and can be deleted at any time. "
    "They are never written to the audit trail or the accounting record."
)


@router.post(
    "/conversations",
    response_model=ConversationTurnResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a conversation with the copilot",
    description=(
        "Answers the first question and, if it was answered (completely or partially), stores "
        "the conversation with that turn. A model failure stores nothing and creates no "
        f"conversation. Any member. {WHAT_LEAVES}"
    ),
    dependencies=[Depends(require_copilot_member), Depends(copilot_budget)],
    responses={**NOT_FOUND, **MODEL_ERRORS},
)
def start_conversation(
    hotel_public_id: HotelPath,
    payload: ConversationQuestionCreate,
    service: CopilotConversationServiceDep,
) -> ConversationTurnResponse:
    return service.create(hotel_public_id, payload.question)


@router.get(
    "/conversations",
    response_model=Page[ConversationSummary],
    summary="List your conversations at this hotel",
    description=(
        "The caller's own live conversations at this hotel, most recently used first. Other "
        "members' conversations are never listed."
    ),
    responses={**NOT_FOUND},
)
def list_conversations(
    hotel_public_id: HotelPath,
    service: CopilotConversationServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[ConversationSummary]:
    return service.list_conversations(hotel_public_id, page=page, page_size=page_size)


@router.get(
    "/conversations/{conversation_public_id}",
    response_model=ConversationTranscript,
    summary="Read one of your conversations",
    description="Every stored turn, in order. Only the conversation's creator can read it.",
    responses={**NOT_FOUND},
)
def read_conversation(
    hotel_public_id: HotelPath,
    conversation_public_id: ConversationPath,
    service: CopilotConversationServiceDep,
) -> ConversationTranscript:
    return service.transcript(hotel_public_id, conversation_public_id)


@router.post(
    "/conversations/{conversation_public_id}/messages",
    response_model=ConversationTurnResponse,
    status_code=status.HTTP_200_OK,
    summary="Continue one of your conversations",
    description=(
        "Answers the next question, shown this conversation's earlier turns as context -- never "
        "as evidence: figures and citations must come from this turn's own lookups. A model "
        "failure stores nothing. Only the conversation's creator can continue it. "
        f"{WHAT_LEAVES}"
    ),
    dependencies=[Depends(require_copilot_member), Depends(conversation_turn_budget)],
    responses={**NOT_FOUND, **MODEL_ERRORS, **CONFLICT},
)
def continue_conversation(
    hotel_public_id: HotelPath,
    conversation_public_id: ConversationPath,
    payload: ConversationQuestionCreate,
    service: CopilotConversationServiceDep,
) -> ConversationTurnResponse:
    return service.continue_conversation(hotel_public_id, conversation_public_id, payload.question)


@router.delete(
    "/conversations/{conversation_public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of your conversations",
    description=(
        "Deletes the conversation and every turn in it, physically. Only its creator can."
    ),
    dependencies=[Depends(require_copilot_member)],
    responses={**NOT_FOUND},
)
def delete_conversation(
    hotel_public_id: HotelPath,
    conversation_public_id: ConversationPath,
    service: CopilotConversationServiceDep,
) -> None:
    service.delete(hotel_public_id, conversation_public_id)
