"""Shared authorization stubs for the layering tests.

From Stage 4.2 ``HotelScopeResolver`` takes a :class:`HotelAccessPolicy`. The per-domain
layering tests construct a resolver to assert things about *resolution* -- "an unknown hotel
is reported before any domain work happens" -- and are not about authorization at all. They
need a policy that gets out of the way.

Not a test module itself: no ``test_`` prefix, so pytest imports it without collecting it.
"""

from __future__ import annotations

from typing import Any

from app.models.enums import HotelRole
from app.services.authorization import HotelAccessPolicy


class AllowAllPolicy(HotelAccessPolicy):
    """A policy that authorizes everything.

    A real subclass rather than a duck-typed stand-in, so the resolver's type signature is
    satisfied without a ``type: ignore`` at every call site -- and so this file fails to
    import the day the policy's interface changes, instead of silently drifting.

    Deliberately **not** used by any authorization test: those exercise the real
    :class:`HotelAccessPolicy` against a live database. This exists so a *resolution* test
    does not have to care that authorization was added beneath it.
    """

    def __init__(self) -> None:
        # No super().__init__(): there is no user and no repository, because nothing here
        # consults either.
        pass

    def authorize(self, hotel: Any) -> HotelRole:
        return HotelRole.OWNER

    def require_role(self, hotel: Any, required: HotelRole) -> HotelRole:
        return HotelRole.OWNER

    def role_at(self, hotel: Any) -> HotelRole:
        return HotelRole.OWNER


__all__ = ["AllowAllPolicy"]
