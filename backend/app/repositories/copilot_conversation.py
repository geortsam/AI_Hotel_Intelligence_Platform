"""Data access for copilot conversations and their turns (Stage 7.11).

**Ownership is this table's domain.** A conversation is readable only by its creator within its
hotel, so every read here takes the resolved `hotel_id` AND the caller's `actor_user_id`, and
applies both in the SQL WHERE clause -- nothing reads a hotel's conversations broadly and filters
in Python. That is why this module, alone among the domain repositories besides the identity and
audit ones, takes a user identity; the architecture suite's `IDENTITY_AWARE` list names it, with
that reason.

**Retention is in every query.** Each read takes `retention_days` and admits only conversations
whose `last_activity_at > now() - retention`, computed by PostgreSQL. A retention day is exactly
24 hours: the interval is built in hours, because an interval of days is calendar arithmetic in
the database session's time zone, and would make the cut-off move by an hour across a daylight
saving change on a server whose zone observes one. An expired conversation is
never read, listed, continued or deleted -- it does not exist to any caller -- and `purge_expired`
deletes expired rows physically, their turns with them (ON DELETE CASCADE).

Both identifiers are always internal keys the service resolved: the hotel through the scope
resolver, the caller from the authenticated request. Nothing a request body or a model produces
reaches this module as an identity.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, select, update
from sqlalchemy.orm import Session

from app.models.copilot_conversation import CopilotConversation, CopilotMessage

#: How many characters of a conversation's first question its summary carries.
PREVIEW_LENGTH = 100


def _retention(retention_days: int) -> ColumnElement[Any]:
    """The retention period as an interval of exactly `retention_days * 24` hours."""
    return func.make_interval(0, 0, 0, 0, retention_days * 24)


def _live(retention_days: int) -> ColumnElement[bool]:
    """Not yet expired: last used within the retention period, by the database's clock."""
    return CopilotConversation.last_activity_at > func.now() - _retention(retention_days)


class CopilotConversationRepository:
    """Conversations and turns -- always within one hotel, always one caller's own."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- writes ------------------------------------------------------------------------------

    def add_conversation(self, conversation: CopilotConversation) -> CopilotConversation:
        self._session.add(conversation)
        self._session.flush()
        return conversation

    def add_turn(self, message: CopilotMessage) -> CopilotMessage:
        self._session.add(message)
        self._session.flush()
        return message

    def advance(
        self,
        hotel_id: int,
        actor_user_id: int,
        conversation_id: int,
        *,
        turn: int,
        next_source_label: int,
    ) -> int:
        """Record that *turn* was stored: counters forward, activity now. Returns rows updated.

        Conditional on the conversation still being at `turn - 1`: a concurrent turn that got
        there first leaves this updating nothing.
        """
        result = self._session.execute(
            update(CopilotConversation)
            .where(
                CopilotConversation.id == conversation_id,
                CopilotConversation.hotel_id == hotel_id,
                CopilotConversation.actor_user_id == actor_user_id,
                CopilotConversation.turn_count == turn - 1,
            )
            .values(
                turn_count=turn,
                next_source_label=next_source_label,
                last_activity_at=func.greatest(func.now(), CopilotConversation.last_activity_at),
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def delete(
        self, hotel_id: int, actor_user_id: int, conversation_id: int, retention_days: int
    ) -> int:
        """Delete one live, owned conversation; its turns go with it. Returns rows deleted."""
        result = self._session.execute(
            delete(CopilotConversation).where(
                CopilotConversation.id == conversation_id,
                CopilotConversation.hotel_id == hotel_id,
                CopilotConversation.actor_user_id == actor_user_id,
                _live(retention_days),
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def purge_expired(self, retention_days: int, *, limit: int, hotel_id: int | None = None) -> int:
        """Delete up to *limit* expired conversations, oldest first; their turns go with them.

        Every hotel's, or one hotel's. Returns how many conversations were deleted.
        """
        expired = [CopilotConversation.last_activity_at <= func.now() - _retention(retention_days)]
        if hotel_id is not None:
            expired.append(CopilotConversation.hotel_id == hotel_id)
        batch = (
            select(CopilotConversation.id)
            .where(*expired)
            .order_by(CopilotConversation.last_activity_at.asc(), CopilotConversation.id.asc())
            .limit(limit)
            .scalar_subquery()
        )
        result = self._session.execute(
            delete(CopilotConversation).where(CopilotConversation.id.in_(batch))
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # --- reads -------------------------------------------------------------------------------

    def get_owned(
        self,
        hotel_id: int,
        actor_user_id: int,
        public_id: uuid.UUID,
        retention_days: int,
    ) -> CopilotConversation | None:
        """The caller's live conversation *public_id* at this hotel, or None."""
        return self._session.scalars(
            select(CopilotConversation).where(
                CopilotConversation.hotel_id == hotel_id,
                CopilotConversation.actor_user_id == actor_user_id,
                CopilotConversation.public_id == public_id,
                _live(retention_days),
            )
        ).one_or_none()

    def count_owned(self, hotel_id: int, actor_user_id: int, retention_days: int) -> int:
        return int(
            self._session.scalar(
                select(func.count())
                .select_from(CopilotConversation)
                .where(
                    CopilotConversation.hotel_id == hotel_id,
                    CopilotConversation.actor_user_id == actor_user_id,
                    _live(retention_days),
                )
            )
            or 0
        )

    def list_owned(
        self,
        hotel_id: int,
        actor_user_id: int,
        retention_days: int,
        *,
        offset: int,
        limit: int,
    ) -> list[tuple[CopilotConversation, str]]:
        """The caller's live conversations here, most recently used first, each with the first
        `PREVIEW_LENGTH` characters of its first question."""
        rows = self._session.execute(
            select(CopilotConversation, func.left(CopilotMessage.question, PREVIEW_LENGTH))
            .join(
                CopilotMessage,
                and_(
                    CopilotMessage.conversation_id == CopilotConversation.id,
                    CopilotMessage.hotel_id == CopilotConversation.hotel_id,
                    CopilotMessage.turn == 1,
                ),
            )
            .where(
                CopilotConversation.hotel_id == hotel_id,
                CopilotConversation.actor_user_id == actor_user_id,
                _live(retention_days),
            )
            .order_by(CopilotConversation.last_activity_at.desc(), CopilotConversation.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return [(conversation, str(preview)) for conversation, preview in rows]

    def turns(
        self, hotel_id: int, actor_user_id: int, conversation_id: int
    ) -> Sequence[CopilotMessage]:
        """Every stored turn of one of the caller's conversations here, in turn order.

        Ownership is applied here too, not only where the conversation was resolved: a turn is
        read only through a conversation the caller owns at this hotel.
        """
        return self._session.scalars(
            select(CopilotMessage)
            .join(
                CopilotConversation,
                and_(
                    CopilotConversation.id == CopilotMessage.conversation_id,
                    CopilotConversation.hotel_id == CopilotMessage.hotel_id,
                ),
            )
            .where(
                CopilotMessage.hotel_id == hotel_id,
                CopilotMessage.conversation_id == conversation_id,
                CopilotConversation.actor_user_id == actor_user_id,
            )
            .order_by(CopilotMessage.turn.asc())
        ).all()


__all__ = ["PREVIEW_LENGTH", "CopilotConversationRepository"]
