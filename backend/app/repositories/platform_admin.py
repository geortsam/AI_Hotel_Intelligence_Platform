"""Platform-grant persistence.

One of exactly two repositories that know a user exists -- the other being
:mod:`app.repositories.membership`, which answers the per-hotel question. Every *domain*
repository stays authorization-agnostic, and a structural test asserts it.

This repository has no ``hotel_id`` in any query, and no join to ``hotels`` or
``user_hotels``. That is not an omission: a platform grant says nothing about any property,
and a query here that reached a hotel would be the beginning of the bypass Stage 4.3 exists
to avoid.

Queries only; never commits.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.platform import PlatformAdmin


class PlatformAdminRepository:
    """Data access for platform-level grants."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def role_for(self, user_id: int) -> str | None:
        """The user's platform role, or None if they hold none.

        Read on every catalogue write, which is why it selects one column against the
        primary key rather than loading the row.
        """
        return self._session.scalars(
            select(PlatformAdmin.role).where(PlatformAdmin.user_id == user_id)
        ).one_or_none()


__all__ = ["PlatformAdminRepository"]
