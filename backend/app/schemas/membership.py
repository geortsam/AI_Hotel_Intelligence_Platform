"""Payloads for hotel membership administration.

A membership joins a **global** identity to **one** property. That shape decides the whole
module: a member is addressed by the user's ``public_id`` (identity is global, so the member
and the account are the same person), while the hotel comes from the URL and never from a
payload -- a body that could name a hotel would be a body that could name a different one.

``password_hash`` is unreachable here, as it is in ``app.schemas.auth``: every response is
built field by field rather than from ``from_attributes``, so a column added to ``users``
later cannot leak through a membership listing by default.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import HotelRole
from app.schemas.auth import EmailField


class MemberResponse(BaseModel):
    """One member of one hotel: who they are, and what they may do here.

    Carries the user's public id -- the identifier the PATCH and DELETE routes take -- and
    the account fields an administrator needs to recognise a colleague. No internal BIGINT:
    not ``users.id``, not ``user_hotels.id``, not ``hotel_id``.

    ``is_active`` is the ACCOUNT's state, not the membership's. A disabled account keeps its
    memberships and simply cannot authenticate; showing it here is how an administrator sees
    that a listed member currently cannot sign in.
    """

    model_config = ConfigDict(frozen=True)

    user_public_id: uuid.UUID = Field(description="Public identifier of the member's account.")
    email: str
    full_name: str
    is_active: bool = Field(description="Whether the ACCOUNT is enabled; not a membership flag.")
    role: HotelRole = Field(description="What this member may do at this hotel.")
    joined_at: dt.datetime = Field(description="When the membership was created.")


class MemberCreate(BaseModel):
    """Payload for adding an EXISTING account to this hotel.

    Names the person by ``email`` rather than by public id because that is the identifier a
    human actually has: there is no user-search endpoint, and adding one would be a far larger
    surface than this stage needs. An unknown address is a 404 -- and discloses nothing new,
    since registration already answers 409 for an address that exists.

    **This never creates an account.** Identity is global; a second row for the same person
    would give them two logins and two password hashes for one human.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailField
    role: HotelRole = Field(description="The role to grant at this hotel.")


class MemberUpdate(BaseModel):
    """Payload for changing a member's role.

    ``role`` is required rather than optional. A membership has exactly one mutable field, so
    the partial-update convention the other resources follow would here mean "a PATCH that
    changes nothing" -- an operation with no meaning that the ownership rules would still have
    to reason about.
    """

    model_config = ConfigDict(extra="forbid")

    role: HotelRole


__all__ = ["MemberCreate", "MemberResponse", "MemberUpdate"]
