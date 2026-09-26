"""Copilot conversations: multi-turn memory for one caller at one hotel (Stage 7.11).

Architecture §4.4 and Amendment A6; `docs/copilot-conversations.md` is the design document.

## Whose, and where

A conversation belongs to the hotel in its path and to the caller who created it. Every method
resolves the hotel through the scope resolver first -- a non-member gets the hotel's 404 -- and
then reads only conversations that hotel AND this caller own, in SQL. Another member, a manager or
owner included, gets the same 404 an unknown conversation gets; so does another hotel, so does an
expired conversation. A caller who has since lost membership gets the hotel's 404 first.

## A turn, stored or not at all

    1. the hotel is resolved; expired conversations at this hotel are purged (at most 100)
    2. continuing: the caller's live conversation is resolved, and refused when full (409)
    3. the copilot answers, shown this conversation's earlier turns (and nothing else),
       numbering its citation labels from where the last turn's ended, under
       `copilot_conversation@v1`
    4. the turn is stored and the conversation advanced, in one transaction

A model failure raises out of step 3 (503/502/429) and nothing is stored: a new conversation is
not created, an existing one gains no turn. A partial answer is a 200 and is stored, labelled.
No database lock is held while the model runs: step 4 inserts turn `turn_count + 1`, and a
concurrent turn that got there first makes the unique `(conversation_id, turn)` refuse this one --
a 409, never a duplicate.

## Retention, enforced rather than documented

A conversation and its turns expire `copilot_conversation_retention_days` after their last
activity. Every read, list, continue and delete admits only live conversations, by PostgreSQL's
clock, so an expired conversation is a 404 the moment it expires and never reaches a model.
Physical deletion needs no scheduler: every create and continue first purges up to
`PURGE_BATCH` expired conversations at the same hotel, and `CopilotConversationRetention.purge`
does the same across every hotel for an operator. Deletion is physical; nothing is archived.

## What is not written anywhere else

The question and the answer are written only to `copilot_messages`. Not to the audit trail --
conversation lifecycle is user content, not a hotel business change, and no audit action exists
for it -- and not to `llm_invocations`, which has no column for text. Each turn still leaves its
one `llm_invocations` row and its `tool.invoked` events, correlated by the request id the turn
stores.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    NotFoundError,
    constraint_name_of,
    internal_fault,
    sqlstate_of,
)
from app.core.request_id import current_request_id
from app.models.copilot_conversation import MAX_TURNS, CopilotConversation, CopilotMessage
from app.models.user import User
from app.repositories.copilot_conversation import CopilotConversationRepository
from app.schemas.common import Page
from app.schemas.copilot import CopilotCitation, DocumentEvidence, StopReason
from app.schemas.copilot_conversation import (
    ConversationSummary,
    ConversationTranscript,
    ConversationTurn,
    ConversationTurnResponse,
    StoredTurn,
)
from app.services.copilot import AnsweredTurn, CopilotService
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

#: The registered prompt a conversation turn is answered under: `copilot_conversation@v1`.
PROMPT_IDENTITY = ("copilot_conversation", "v1")

#: The most expired conversations one create or continue purges before it runs.
PURGE_BATCH = 100

#: The conversation list's page sizes.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

CONVERSATION_NOT_FOUND = "No conversation with this identifier exists at this hotel."
CONVERSATION_FULL = "CONVERSATION_FULL"
CONVERSATION_FULL_MESSAGE = (
    f"This conversation has reached its limit of {MAX_TURNS} turns. Start a new conversation."
)
CONVERSATION_CHANGED = (
    "Another turn was added to this conversation while this one was being answered. Read the "
    "conversation and ask again."
)


class CopilotConversationRetention:
    """Physically deletes expired conversations, their turns with them. No actor: nobody asks."""

    def __init__(
        self, session: Session, repository: CopilotConversationRepository, *, retention_days: int
    ) -> None:
        self._session = session
        self._repository = repository
        self._retention_days = retention_days

    def purge(self, *, limit: int = PURGE_BATCH, hotel_id: int | None = None) -> int:
        """Delete up to *limit* expired conversations -- at one hotel, or at every hotel."""
        try:
            deleted = self._repository.purge_expired(
                self._retention_days, limit=limit, hotel_id=hotel_id
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        if deleted:
            logger.info(
                "purged %s expired copilot conversation(s)",
                deleted,
                extra={"conversations_purged": deleted},
            )
        return deleted


class CopilotConversationService:
    """One caller's conversations at one hotel. See the module docstring."""

    def __init__(
        self,
        session: Session,
        repository: CopilotConversationRepository,
        scope: HotelScopeResolver,
        copilot: CopilotService,
        caller: User,
        *,
        retention_days: int,
    ) -> None:
        self._session = session
        self._repository = repository
        self._scope = scope
        self._copilot = copilot
        self._caller = caller
        self._retention_days = retention_days
        self._retention = CopilotConversationRetention(
            session, repository, retention_days=retention_days
        )

    # --- before any budget is charged -------------------------------------------------------

    def require_open(self, hotel_public_id: uuid.UUID, conversation_public_id: uuid.UUID) -> None:
        """The caller's live conversation exists here and can take another turn -- or raise.

        Run by the continue route BEFORE the copilot budget, so a request that cannot be
        answered -- someone else's conversation, an expired or unknown one, a full one -- is
        refused without spending the caller's or the hotel's allowance.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        conversation = self._owned(hotel.id, conversation_public_id)
        if conversation.turn_count >= MAX_TURNS:
            raise ConflictError(CONVERSATION_FULL_MESSAGE, code=CONVERSATION_FULL)

    # --- the five operations ------------------------------------------------------------------

    def create(self, hotel_public_id: uuid.UUID, question: str) -> ConversationTurnResponse:
        """Start a conversation by answering its first question. Nothing is stored on failure."""
        hotel = self._scope.require_hotel(hotel_public_id)
        self._retention.purge(hotel_id=hotel.id)

        answered = self._copilot.answer_turn(
            hotel_public_id, question, prompt_identity=PROMPT_IDENTITY
        )
        try:
            conversation = self._repository.add_conversation(
                CopilotConversation(
                    hotel_id=hotel.id,
                    actor_user_id=self._caller.id,
                    turn_count=1,
                    next_source_label=answered.next_label,
                )
            )
            self._repository.add_turn(
                self._turn_row(conversation.id, hotel.id, 1, question, answered)
            )
            public_id = conversation.public_id
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        except Exception:
            self._session.rollback()
            raise
        return self._answered(public_id, 1, question, answered)

    def continue_conversation(
        self, hotel_public_id: uuid.UUID, conversation_public_id: uuid.UUID, question: str
    ) -> ConversationTurnResponse:
        """Answer the next question, shown this conversation's earlier turns and nothing else."""
        hotel = self._scope.require_hotel(hotel_public_id)
        self._retention.purge(hotel_id=hotel.id)

        conversation = self._owned(hotel.id, conversation_public_id)
        if conversation.turn_count >= MAX_TURNS:
            raise ConflictError(CONVERSATION_FULL_MESSAGE, code=CONVERSATION_FULL)
        # Captured before the model runs: nothing below reads the conversation row again.
        conversation_id = conversation.id
        turn = conversation.turn_count + 1
        first_label = conversation.next_source_label
        earlier = [
            (stored.question, stored.answer)
            for stored in self._repository.turns(hotel.id, self._caller.id, conversation_id)
        ]

        answered = self._copilot.answer_turn(
            hotel_public_id,
            question,
            earlier=earlier,
            first_label=first_label,
            prompt_identity=PROMPT_IDENTITY,
        )
        try:
            self._repository.add_turn(
                self._turn_row(conversation_id, hotel.id, turn, question, answered)
            )
            advanced = self._repository.advance(
                hotel.id,
                self._caller.id,
                conversation_id,
                turn=turn,
                next_source_label=answered.next_label,
            )
            if advanced != 1:
                raise ConflictError(CONVERSATION_CHANGED)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        except Exception:
            self._session.rollback()
            raise
        return self._answered(conversation_public_id, turn, question, answered)

    def list_conversations(
        self, hotel_public_id: uuid.UUID, *, page: int, page_size: int
    ) -> Page[ConversationSummary]:
        """The caller's live conversations here, most recently used first."""
        hotel = self._scope.require_hotel(hotel_public_id)
        total = self._repository.count_owned(hotel.id, self._caller.id, self._retention_days)
        rows = self._repository.list_owned(
            hotel.id,
            self._caller.id,
            self._retention_days,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return Page.build(
            [
                ConversationSummary(
                    public_id=conversation.public_id,
                    created_at=conversation.created_at,
                    last_activity_at=conversation.last_activity_at,
                    expires_at=self._expires_at(conversation),
                    turn_count=conversation.turn_count,
                    first_question_preview=preview,
                )
                for conversation, preview in rows
            ],
            total,
            page,
            page_size,
        )

    def transcript(
        self, hotel_public_id: uuid.UUID, conversation_public_id: uuid.UUID
    ) -> ConversationTranscript:
        """The caller's live conversation, with every stored turn in turn order."""
        hotel = self._scope.require_hotel(hotel_public_id)
        conversation = self._owned(hotel.id, conversation_public_id)
        turns = self._repository.turns(hotel.id, self._caller.id, conversation.id)
        return ConversationTranscript(
            public_id=conversation.public_id,
            created_at=conversation.created_at,
            last_activity_at=conversation.last_activity_at,
            expires_at=self._expires_at(conversation),
            turn_count=conversation.turn_count,
            turns_remaining=MAX_TURNS - conversation.turn_count,
            turns=[
                StoredTurn(
                    turn=stored.turn,
                    question=stored.question,
                    answer=stored.answer,
                    complete=stored.complete,
                    stop_reason=cast(StopReason, stored.stop_reason),
                    document_evidence=cast(DocumentEvidence, stored.document_evidence),
                    citations=[CopilotCitation.model_validate(item) for item in stored.citations],
                    context_turns=stored.context_turns,
                    prompt_id=stored.prompt_id,
                    prompt_version=stored.prompt_version,
                    created_at=stored.created_at,
                )
                for stored in turns
            ],
        )

    def delete(self, hotel_public_id: uuid.UUID, conversation_public_id: uuid.UUID) -> None:
        """Delete the caller's live conversation and every turn in it. Physical; not archived."""
        hotel = self._scope.require_hotel(hotel_public_id)
        conversation = self._owned(hotel.id, conversation_public_id)
        try:
            deleted = self._repository.delete(
                hotel.id, self._caller.id, conversation.id, self._retention_days
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        if deleted != 1:
            raise NotFoundError(CONVERSATION_NOT_FOUND)

    # --- internals ----------------------------------------------------------------------------

    def _owned(self, hotel_id: int, public_id: uuid.UUID) -> CopilotConversation:
        conversation = self._repository.get_owned(
            hotel_id, self._caller.id, public_id, self._retention_days
        )
        if conversation is None:
            raise NotFoundError(CONVERSATION_NOT_FOUND)
        return conversation

    def _expires_at(self, conversation: CopilotConversation) -> dt.datetime:
        """Exactly `retention_days * 24` hours after the last activity -- the same instant the
        repository's SQL cuts off at. Added in UTC: adding days to a zoned datetime is wall-clock
        arithmetic, and would be an hour off across a daylight-saving change."""
        last = conversation.last_activity_at.astimezone(dt.UTC)
        return last + dt.timedelta(days=self._retention_days)

    @staticmethod
    def _turn_row(
        conversation_id: int, hotel_id: int, turn: int, question: str, answered: AnsweredTurn
    ) -> CopilotMessage:
        response = answered.response
        citations: list[dict[str, Any]] = [
            citation.model_dump(mode="json") for citation in response.citations
        ]
        return CopilotMessage(
            conversation_id=conversation_id,
            hotel_id=hotel_id,
            turn=turn,
            question=question,
            answer=response.answer,
            stop_reason=response.stop_reason,
            complete=response.complete,
            document_evidence=response.document_evidence,
            citations=citations,
            prompt_id=response.prompt_id,
            prompt_version=response.prompt_version,
            context_turns=answered.context_turns,
            request_id=current_request_id(),
        )

    @staticmethod
    def _answered(
        conversation_public_id: uuid.UUID, turn: int, question: str, answered: AnsweredTurn
    ) -> ConversationTurnResponse:
        response = answered.response
        return ConversationTurnResponse(
            conversation_public_id=conversation_public_id,
            turn=ConversationTurn(
                turn=turn,
                question=question,
                answer=response.answer,
                complete=response.complete,
                stop_reason=response.stop_reason,
                notice=response.notice,
                tools_used=response.tools_used,
                citations=response.citations,
                document_evidence=response.document_evidence,
                context_turns=answered.context_turns,
                prompt_id=response.prompt_id,
                prompt_version=response.prompt_version,
                invocation_public_id=response.invocation_public_id,
            ),
            turns_remaining=MAX_TURNS - turn,
        )

    @staticmethod
    def _translate(exc: IntegrityError) -> Exception:
        """A lost race is a conflict; a conversation deleted mid-turn is gone; anything else is
        this server's own fault -- every column was built here. No ``exc_info``: the driver
        renders the offending row, question text included, into its message."""
        logger.warning("Copilot conversation integrity error (sqlstate=%s)", sqlstate_of(exc))
        if constraint_name_of(exc) == "uq_copilot_messages_conversation_id_turn":
            return ConflictError(CONVERSATION_CHANGED)
        if constraint_name_of(exc) == "fk_copilot_messages_conversation_id_hotel_id":
            return NotFoundError(CONVERSATION_NOT_FOUND)
        return internal_fault(exc)


__all__ = [
    "CONVERSATION_FULL",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "PROMPT_IDENTITY",
    "PURGE_BATCH",
    "CopilotConversationRetention",
    "CopilotConversationService",
]
