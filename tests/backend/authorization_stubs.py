"""Shared collaborator stubs for the layering tests.

From Stage 4.2 ``HotelScopeResolver`` takes a :class:`HotelAccessPolicy`. The per-domain
layering tests construct a resolver to assert things about *resolution* -- "an unknown hotel
is reported before any domain work happens" -- and are not about authorization at all. They
need a policy that gets out of the way.

Not a test module itself: no ``test_`` prefix, so pytest imports it without collecting it.
"""

from __future__ import annotations

from typing import Any

from app.models.enums import HotelRole
from app.repositories.audit import AuditRepository
from app.services.audit import AuditTrail
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


def audit_trail(session: Any = None) -> AuditTrail:
    """A real :class:`AuditTrail` over whatever session the caller has.

    Stage 4.5.12 made the audit collaborator REQUIRED on every mutating service, rather than
    an optional argument defaulting to None. That is the point of it: a service that could be
    constructed without one would be a service that could write unaudited, and the layering
    tests below would be the first place that quietly happened.

    The real class, not a mock, so these tests keep exercising the real constructor. It is
    handed a stub session it never reaches: every test using this asserts a refusal that
    happens before anything is written.
    """
    return AuditTrail(AuditRepository(session))


__all__ = ["AllowAllPolicy", "audit_trail"]
