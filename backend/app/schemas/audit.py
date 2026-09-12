"""What one audit event looks like from outside.

Stage 4.5.12, extended by Stage 4.5.13. **Read-only: there is no request schema in this
module**, and its absence is the contract. Audit events are written by the services that
perform the mutations, from a closed vocabulary of literals; a client cannot post one, name an
action, backdate one, or claim to be somebody else, because no shape exists in which any of
that could be expressed.

**Every identifier here is public.** The actor is named by ``actor_public_id`` -- the same
UUID a hotel's member listing already shows -- and the subject of the event by
``resource_reference``, which holds a public UUID rendered as text or a natural key such as an
amenity code. No BIGINT appears in this module, and none can: the fields that hold one in the
database are not declared here at all.

**Two responses, one base, and the split is the point.** An audit event either belongs to a
property or belongs to none -- ``audit_events.hotel_id`` is nullable precisely so a password
change and a global catalogue edit are not falsely attributed to a tenant. So:

* :class:`AuditEventResponse` is the hotel surface. ``hotel_public_id`` is **required and
  non-nullable**, exactly as Stage 4.5.12 left it.
* :class:`PlatformAuditEventResponse` is the platform surface. It has **no hotel field at
  all**, because every row it can describe has ``hotel_id IS NULL``.

Making ``hotel_public_id`` optional on one shared model was the obvious alternative and is
worse: it would weaken a contract hotel clients already rely on in order to describe rows that
can never carry a hotel, and it would leave "null" meaning "platform-scoped" -- a distinction a
reader would have to know rather than see. Two models over one base state it instead, and the
nine common fields are still declared once.

**``actor_email`` discloses nothing new on either surface.** Hotel audit history requires
MANAGER, and ``GET /hotels/{id}/members`` already gives MANAGER the public-id-to-email mapping
for the same people; the platform surface requires a platform administrator, who may already
edit the vocabulary every property depends on. Including it is what makes the trail readable
without a second lookup per row. It is nullable for the same reason ``actor_public_id`` is: an
event no authenticated user caused would have neither.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditEventBase(BaseModel):
    """Everything an audit event says regardless of which surface reports it.

    Deliberately never returned on its own: it is the shared half of the two responses below,
    so a field can be added to the audit contract in exactly one place.
    """

    model_config = ConfigDict(from_attributes=True)

    public_id: uuid.UUID = Field(description="Identifies this event; a BIGINT never appears.")
    occurred_at: dt.datetime = Field(
        description="When it happened, stamped by the database, not by the caller."
    )

    action: str = Field(description="What happened, from the closed audit vocabulary.")
    resource_type: str = Field(description="What kind of thing it happened to.")
    resource_reference: str = Field(
        description="Which one: a public identifier, or a natural key such as a catalogue code."
    )

    actor_public_id: uuid.UUID | None = Field(
        default=None, description="Who did it. Null for an event no authenticated user caused."
    )
    actor_email: str | None = Field(
        default=None, description="The actor's account address, as the member listing shows it."
    )

    request_id: str | None = Field(
        default=None,
        description=(
            "The X-Request-ID of the request that caused this change, so the event and the "
            "application's log lines for that request can be read together."
        ),
    )

    details: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "A small object describing the change -- statuses before and after, the fields "
            "that moved, the amount and currency of a payment. Never a request body, and "
            "never a credential, a token, a card fragment or an internal key."
        ),
    )


class AuditEventResponse(AuditEventBase):
    """One recorded change at one property, as an operator reads it."""

    hotel_public_id: uuid.UUID = Field(
        description="The property this event belongs to -- always the one in the URL."
    )


class PlatformAuditEventResponse(AuditEventBase):
    """One recorded change that belongs to no property (Stage 4.5.13).

    **No hotel field, and that is the type stating the scope.** Every row this can describe has
    ``hotel_id IS NULL`` -- a password change, or an edit to one of the three global
    catalogues. A nullable hotel field here would invite a reader to wonder which rows have one.
    """


__all__ = ["AuditEventBase", "AuditEventResponse", "PlatformAuditEventResponse"]
