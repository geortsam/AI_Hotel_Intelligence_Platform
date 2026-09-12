"""Hotel membership administration.

Stage 4.4. Stage 4.2 created memberships in exactly two ways -- a hotel's creator became its
owner, and everything else was an out-of-band INSERT. This makes the rest operational: list
who has access, add somebody, change what they may do, take access away.

**Identity is global; membership is not.** Adding a member never creates an account. The
person named by ``email`` must already have one, because two rows for one human would mean
two passwords, two audit trails and two answers to "who is this?".

This layer owns the transaction, exactly as every other service does. Its one unusual
responsibility is that two of its operations can leave a hotel with nobody able to administer
it, so both take a lock before they decide -- see :meth:`_owner_count`.

Authorization is NOT decided here. The route declares the minimum role,
``HotelScopeResolver`` resolves and authorizes, and
:class:`~app.services.authorization.MembershipPolicy` holds the ownership rule. What is left
in this module is the unit of work.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    GENERIC_CONFLICT_MESSAGE,
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    NotFoundError,
    internal_fault,
    is_audit_integrity_failure,
    sqlstate_of,
)
from app.models.enums import AuditAction, AuditResourceType, HotelRole
from app.models.hotel import Hotel
from app.models.membership import UserHotel
from app.models.user import User
from app.repositories.membership import MembershipRepository
from app.repositories.user import UserRepository
from app.schemas.common import Page
from app.schemas.membership import MemberCreate, MemberResponse, MemberUpdate
from app.services.audit import AuditTrail
from app.services.authorization import MembershipPolicy
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: What a caller sees when the account named in a URL does not exist, and equally when it
#: exists but belongs to no membership here. The two are deliberately one sentence: an
#: administrator of THIS hotel has no business learning which accounts exist elsewhere.
MEMBER_NOT_FOUND_MESSAGE = "No member of this hotel matches that user."

#: What a caller sees when they name an address with no account behind it. Distinct from the
#: message above, and safely so: registration already answers 409 for an address that exists,
#: so account existence is not a secret this endpoint is keeping.
NO_SUCH_ACCOUNT_MESSAGE = (
    "No account exists for that email address. The person must register before they can be "
    "added to a hotel."
)


class MembershipService:
    """Domain operations on a hotel's membership list."""

    def __init__(
        self,
        session: Session,
        memberships: MembershipRepository,
        users: UserRepository,
        scope: HotelScopeResolver,
        audit: AuditTrail,
    ) -> None:
        self._session = session
        self._memberships = memberships
        self._users = users
        self._scope = scope
        # Stage 4.5.12. Required: who may reach a property is the change most worth a record,
        # and a service that could be built without one could grant access silently.
        self._audit = audit

    # --- reads ----------------------------------------------------------------------------

    def list(
        self, hotel_public_id: uuid.UUID, *, page: int, page_size: int
    ) -> Page[MemberResponse]:
        """One page of this hotel's members.

        MANAGER rather than OWNER: running a property day to day means knowing who has access
        to it, and the listing grants nothing. Changing it is the owner's business.

        The count uses the same predicate as the page, so the total cannot contradict the
        rows it accompanies.
        """
        hotel = self._scope.require_hotel_with_role(hotel_public_id, HotelRole.MANAGER)
        offset = (page - 1) * page_size
        total = self._memberships.count_members(hotel.id)
        rows = self._memberships.members_page(hotel.id, limit=page_size, offset=offset)
        return Page.build(
            items=[self._render(membership, user) for membership, user in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    # --- writes ---------------------------------------------------------------------------

    def add(self, hotel_public_id: uuid.UUID, payload: MemberCreate) -> MemberResponse:
        """Grant an existing account a role at this hotel, and commit.

        An address with no account behind it is a 404. An address that is already a member is
        a 409, not a 404: the caller can see that person in their own listing, so pretending
        not to find them would be a lie about their own hotel rather than a kept secret.
        """
        hotel = self._scope.require_hotel_with_role(hotel_public_id, HotelRole.OWNER)
        user = self._users.get_by_email(payload.email.lower())
        if user is None:
            raise NotFoundError(NO_SUCH_ACCOUNT_MESSAGE)

        membership = UserHotel(user_id=user.id, hotel_id=hotel.id, role=payload.role.value)
        try:
            created = self._memberships.add(membership)
            # The membership is named by the ACCOUNT's public id, which is how the URL for
            # changing or removing it is formed. The email is not recorded: an audit row is
            # read by more people than the member listing is, and the public id already
            # identifies the person unambiguously to anyone entitled to resolve it.
            self._audit.record(
                AuditAction.MEMBERSHIP_CREATED,
                AuditResourceType.MEMBERSHIP,
                str(user.public_id),
                hotel_id=hotel.id,
                details={"role": payload.role.value},
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._render(created, user)

    def change_role(
        self, hotel_public_id: uuid.UUID, user_public_id: uuid.UUID, payload: MemberUpdate
    ) -> MemberResponse:
        """Change a member's role, and commit.

        The owner count is read under a lock held for the rest of the transaction, so the
        decision cannot be invalidated between being made and being committed.
        """
        hotel = self._scope.require_hotel_with_role(hotel_public_id, HotelRole.OWNER)
        user, membership = self._require_member(hotel, user_public_id)
        current = HotelRole(membership.role)

        MembershipPolicy.check_role_change(
            current=current,
            requested=payload.role,
            owner_count=self._owner_count(hotel),
            target_is_owner=current is HotelRole.OWNER,
        )

        try:
            updated = self._memberships.set_role(membership, payload.role)
            if current is not payload.role:
                # Only a real change. Reasserting the role somebody already holds changes
                # nothing, and "manager -> manager" in the history would be noise that makes
                # the rows that matter harder to find.
                self._audit.record(
                    AuditAction.MEMBERSHIP_ROLE_CHANGED,
                    AuditResourceType.MEMBERSHIP,
                    str(user.public_id),
                    hotel_id=hotel.id,
                    details={"old_role": current.value, "new_role": payload.role.value},
                )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._render(updated, user)

    def remove(self, hotel_public_id: uuid.UUID, user_public_id: uuid.UUID) -> None:
        """Revoke a member's access, and commit.

        Revoking is deleting the row -- ``user_hotels`` has no ``is_active``, deliberately, so
        that "no longer has access" has exactly one representation.
        """
        hotel = self._scope.require_hotel_with_role(hotel_public_id, HotelRole.OWNER)
        user, membership = self._require_member(hotel, user_public_id)
        removed_role = membership.role

        MembershipPolicy.check_removal(
            target_is_owner=HotelRole(membership.role) is HotelRole.OWNER,
            owner_count=self._owner_count(hotel),
        )

        try:
            self._memberships.delete(membership)
            # `removed_role` was read before the delete, because afterwards there is no row
            # to read it from -- revoking access here means deleting the membership, not
            # flagging it.
            self._audit.record(
                AuditAction.MEMBERSHIP_REMOVED,
                AuditResourceType.MEMBERSHIP,
                str(user.public_id),
                hotel_id=hotel.id,
                details={"role": removed_role},
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

    # --- internals ------------------------------------------------------------------------

    def _owner_count(self, hotel: Hotel) -> int:
        """How many owners this hotel has, with those rows locked until the commit.

        Taking the lock BEFORE the policy decides is the whole point: without it, two
        concurrent demotions of two different owners each see a count of 2, each conclude
        they are safe, and together leave a hotel nobody can administer.
        """
        return len(self._memberships.lock_owner_ids(hotel.id))

    def _require_member(self, hotel: Hotel, user_public_id: uuid.UUID) -> tuple[User, UserHotel]:
        """Resolve a member of THIS hotel, or raise the one not-found.

        An account that does not exist and an account that exists but belongs to another
        hotel produce the same error, so this hotel's administrator cannot use the endpoint
        to probe the user table.
        """
        user = self._users.get_by_public_id(user_public_id)
        if user is None:
            raise NotFoundError(MEMBER_NOT_FOUND_MESSAGE)
        membership = self._memberships.get_membership(user.id, hotel.id)
        if membership is None:
            raise NotFoundError(MEMBER_NOT_FOUND_MESSAGE)
        return user, membership

    @staticmethod
    def _render(membership: UserHotel, user: User) -> MemberResponse:
        """Build the response field by field. No internal key can reach a client from here."""
        return MemberResponse(
            user_public_id=user.public_id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
            role=HotelRole(membership.role),
            joined_at=membership.created_at,
        )

    def _translate(self, exc: IntegrityError) -> Exception:
        """Turn an integrity error into a domain error, leaking nothing.

        The driver message carries the constraint name, the statement and the bound
        parameters -- including, here, a colleague's internal user id. It is logged without
        the exception and discarded; only a written-for-clients sentence is returned.
        """
        state = sqlstate_of(exc)
        logger.warning("Membership write rejected by the database (SQLSTATE %s)", state)

        if is_audit_integrity_failure(exc):
            # Stage 4.5.16. The audit layer failed, not this domain -- so nothing below may
            # claim it. Before this guard, a booking deletion whose audit INSERT failed told
            # the client that payments still referenced the booking, which was false and
            # unactionable.
            return internal_fault(exc)
        if state == SQLSTATE_UNIQUE_VIOLATION:
            return ConflictError("That user is already a member of this hotel.")
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a membership constraint.")
        return ConflictError(GENERIC_CONFLICT_MESSAGE)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "MEMBER_NOT_FOUND_MESSAGE",
    "NO_SUCH_ACCOUNT_MESSAGE",
    "MembershipService",
]
