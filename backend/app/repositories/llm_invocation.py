"""Data access for `llm_invocations` (Stage 7.7). One write, no reads, no user.

The table records who asked, but this repository never learns who that is: it is handed a fully
built `LlmInvocation` and stages it. The caller's identity is bound one layer up, by
`app.services.llm_invocation_log.LlmInvocationLog`, constructed with the authenticated user by
the dependency chain -- the same arrangement `AuditTrail` uses over `AuditRepository`. That keeps
this module outside the short list of identity-aware repositories: it imports no user model and
takes no user, actor or principal argument, which the architecture suite asserts of every
domain repository.

No read method exists. Stage 7.7 offers no endpoint that lists invocations, and a query written
before its caller would be a query whose scope nobody reviewed.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.llm_invocation import LlmInvocation


class LlmInvocationRepository:
    """Appends invocation rows. Never updates -- the table refuses it -- and never reads."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, invocation: LlmInvocation) -> LlmInvocation:
        """Stage one row and flush, so the database assigns its public id and timestamp.

        ``flush``, not ``commit``: the copilot service owns the transaction, exactly as every
        writing service in this codebase does.
        """
        self._session.add(invocation)
        self._session.flush()
        return invocation


__all__ = ["LlmInvocationRepository"]
