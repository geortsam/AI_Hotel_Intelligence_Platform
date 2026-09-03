"""The one place authorization is decided.

Stage 4.1 established *who* is calling. This establishes *which hotels* they may reach and
*what* they may do there. The split matters: authentication is about a credential,
authorization is about a relationship, and mixing them is how a permission check ends up
duplicated in forty-eight places with one of them subtly wrong.

**Two access policies live here, and they never speak to each other.**
:class:`HotelAccessPolicy` answers "may you act on this property?" from ``user_hotels``.
:class:`PlatformAccessPolicy` (Stage 4.3) answers "may you maintain what every property
shares?" from ``platform_admins``. Neither consults the other's table, and neither grants
what the other governs: a platform administrator gets no hotel access, and an owner gets no
catalogue authority. They share this module because authorization belongs in one auditable
place, not because they share a decision.

:class:`MembershipPolicy` (Stage 4.4) is a third thing and is deliberately NOT an access
policy. It holds the rules that decide whether a membership change is *coherent* -- chiefly
that a hotel may never end up with nobody who can administer it. Those are integrity rules
rather than "may this caller?" rules, so they take no user and read no table; the service
supplies the facts it has locked, and this decides. Keeping them here rather than inline in
the service is what makes "what protects ownership?" a question with one answer.

**Read the two failure modes carefully -- they differ on purpose.**

``authorize`` (membership) raises the *same* :class:`NotFoundError` the hotel resolver raises
for a hotel that does not exist. A 403 there would confirm the property exists, turning
``GET /hotels/{uuid}`` into an existence oracle and undoing eleven stages of making one
tenant's data invisible to another.

``require_role`` (rank) raises 403. Once membership is established the hotel's existence is
no longer a secret, so hiding the reason buys nothing -- and "you are in the wrong role" is
actionable where "not found" is not.

**This class never writes.** It reads a membership and decides; it does not commit, does not
mutate business data, and holds no session of its own beyond the repository it was given.
"""

from __future__ import annotations

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.enums import HotelRole, PlatformRole
from app.models.hotel import Hotel
from app.models.user import User
from app.repositories.membership import MembershipRepository
from app.repositories.platform_admin import PlatformAdminRepository

#: The message a non-member sees. Byte-identical to the hotel resolver's own not-found text,
#: because the two responses must be indistinguishable.
HOTEL_NOT_FOUND_MESSAGE = "Hotel not found."

#: Refusing the last owner's removal or demotion. It names the rule and the way out -- promote
#: somebody first -- because the caller is an owner of this hotel and there is nothing to hide
#: from them; it names no user, no count and no table.
LAST_OWNER_MESSAGE = (
    "This hotel would be left without an owner. Grant the owner role to another member "
    "before removing or demoting the last one."
)


class HotelAccessPolicy:
    """Decides whether the authenticated caller may act on a hotel, and at what level.

    Request-scoped: it is constructed with the current user, so no method needs to be handed
    an identity and no caller can pass the wrong one.
    """

    def __init__(self, user: User, memberships: MembershipRepository) -> None:
        self._user = user
        self._memberships = memberships
        #: Memoised for the request. A single endpoint can resolve the same hotel several
        #: times -- payments resolve the hotel and then the booking, for instance -- and
        #: re-querying the membership each time is pure waste.
        self._cache: dict[int, HotelRole] = {}

    @property
    def user(self) -> User:
        return self._user

    def role_at(self, hotel: Hotel) -> HotelRole | None:
        """The caller's role at this hotel, or None if they are not a member."""
        if hotel.id in self._cache:
            return self._cache[hotel.id]
        stored = self._memberships.role_for(self._user.id, hotel.id)
        if stored is None:
            return None
        role = HotelRole(stored)
        self._cache[hotel.id] = role
        return role

    def authorize(self, hotel: Hotel) -> HotelRole:
        """Require membership of *hotel*, returning the caller's role there.

        Raises the hotel's own not-found error when there is none. That is deliberate and is
        the security boundary: a non-member must not be able to tell a hotel they cannot
        reach from one that does not exist.
        """
        role = self.role_at(hotel)
        if role is None:
            raise NotFoundError(HOTEL_NOT_FOUND_MESSAGE)
        return role

    def require_role(self, hotel: Hotel, required: HotelRole) -> HotelRole:
        """Require membership **and** a role of at least *required*.

        The rank comparison is the whole authorization model. It works because the four roles
        are totally ordered; see ``HotelRole`` for what would break that.
        """
        role = self.authorize(hotel)
        if not role.outranks_or_equals(required):
            # The message names the required level but never the caller's own role: telling
            # someone they are a "viewer" leaks the membership model to an attacker who has
            # merely found a valid session.
            raise ForbiddenError(
                f"This operation requires the {required.value} role at this hotel."
            )
        return role


class PlatformAccessPolicy:
    """Decides whether the authenticated caller holds platform authority.

    Request-scoped, like :class:`HotelAccessPolicy`, and constructed with the current user so
    no method can be handed the wrong identity.

    **The role is read from the database on every call, never from the request.** The JWT
    carries no role claim (see :func:`app.core.security.create_access_token`) precisely so
    that a grant revoked a minute ago stops working now rather than when a token happens to
    expire -- and so that a client holding a signed token cannot describe its own authority.

    **This class knows nothing about hotels.** It takes no hotel, has no repository that can
    reach one, and is never consulted by :class:`HotelAccessPolicy`. Holding platform
    authority therefore cannot widen hotel access by any path, which is asserted structurally
    as well as behaviourally.
    """

    def __init__(self, user: User, admins: PlatformAdminRepository) -> None:
        self._user = user
        self._admins = admins

    @property
    def user(self) -> User:
        return self._user

    def role(self) -> PlatformRole | None:
        """The caller's platform role, or None if they hold none.

        Not memoised, unlike :meth:`HotelAccessPolicy.role_at`: that one is asked repeatedly
        within a request because a nested resource resolves its hotel more than once, while
        this is asked exactly once, by the route dependency that guards a catalogue write.
        """
        stored = self._admins.role_for(self._user.id)
        return None if stored is None else PlatformRole(stored)

    def is_platform_admin(self) -> bool:
        return self.role() is PlatformRole.PLATFORM_ADMIN

    def require_platform_admin(self) -> PlatformRole:
        """Require platform authority, or raise 403.

        403 rather than 404: unlike a hotel, the global catalogues are not secret -- every
        authenticated caller may already read them, so refusing a write reveals nothing that
        a GET would not. Hiding the reason here would buy nothing and would leave a caller
        unable to tell "you may not" from "it is not there".
        """
        role = self.role()
        if role is None:
            raise ForbiddenError(
                "This operation requires platform administrator privileges. Global catalogue "
                "entries are shared by every hotel, so no per-hotel role grants authority "
                "over them."
            )
        return role


class MembershipPolicy:
    """The rules that keep a hotel administrable, expressed once.

    **Not an authorization policy.** Whether the caller may administer membership at all is
    decided before this is reached, by ``require_role(HotelRole.OWNER)`` on the route and
    :class:`HotelAccessPolicy` beneath it. What is left is a question about the hotel's
    resulting state, and it has one rule:

        **a hotel always has at least one owner.**

    Every other guard this stage needs falls out of that rule combined with the existing role
    requirement, which is why there is no hierarchy here to get wrong:

    * the last owner cannot be removed, or demoted -- the rule, directly;
    * a member cannot promote themselves, because only an owner may write a membership and an
      owner is already at the top of the hierarchy;
    * a lower role cannot grant ownership, for the same reason.

    **Stateless and side-effect free.** It is given counts the caller has already locked, so
    it cannot itself race, and it can be tested without a database.
    """

    @staticmethod
    def check_role_change(
        *, current: HotelRole, requested: HotelRole, owner_count: int, target_is_owner: bool
    ) -> None:
        """Refuse a role change that would leave the hotel with no owner.

        ``owner_count`` must have been read under a lock the caller still holds, or the
        answer is a guess about a number another transaction may already be changing.
        """
        if current is requested:
            return
        if target_is_owner and requested is not HotelRole.OWNER and owner_count <= 1:
            raise ConflictError(LAST_OWNER_MESSAGE)

    @staticmethod
    def check_removal(*, target_is_owner: bool, owner_count: int) -> None:
        """Refuse a removal that would leave the hotel with no owner.

        Applies whether the caller is removing themselves or somebody else: the hotel does
        not care which, and a rule that treated the two differently would be one an owner
        could route around by asking a colleague.
        """
        if target_is_owner and owner_count <= 1:
            raise ConflictError(LAST_OWNER_MESSAGE)


__all__ = [
    "HOTEL_NOT_FOUND_MESSAGE",
    "LAST_OWNER_MESSAGE",
    "HotelAccessPolicy",
    "MembershipPolicy",
    "PlatformAccessPolicy",
]
