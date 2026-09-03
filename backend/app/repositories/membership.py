"""Membership persistence.

The only repository in this codebase that knows a user exists. Every *domain* repository
stays authorization-agnostic -- none of them takes a user, and a structural test asserts it.
Concentrating the user/hotel relation here is what keeps "may they?" out of forty-eight
domain query sites.

Queries and flushes; never commits.
"""

from __future__ import annotations

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import HotelRole
from app.models.hotel import Hotel
from app.models.membership import UserHotel
from app.models.user import User


class MembershipRepository:
    """Data access for user-to-hotel memberships."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, membership: UserHotel) -> UserHotel:
        """Stage a membership and flush so the database assigns its defaults.

        ``flush`` -- not ``commit`` -- so a hotel and its owner membership land in one
        transaction: nobody should be able to create a hotel and fail to own it.
        """
        self._session.add(membership)
        self._session.flush()
        self._session.refresh(membership)
        return membership

    def delete_for_hotel(self, hotel_id: int) -> None:
        """Remove every membership of a hotel that is about to be deleted.

        ``fk_user_hotels_hotel_id_hotels`` is ON DELETE RESTRICT, which exists to stop a
        property vanishing under its staff. But a membership is access-control metadata, not
        operating history: once the hotel is gone, access to it is meaningless. Without this
        a hotel could never be deleted at all -- its own creator's owner row would block it,
        which is a bug the Stage 4.2 integration suite caught immediately.

        The domain RESTRICTs that matter -- bookings, guests, rooms, revenue, expenses --
        are untouched and still refuse the delete, which is the protection actually intended.
        """
        self._session.execute(sql_delete(UserHotel).where(UserHotel.hotel_id == hotel_id))
        self._session.flush()

    def role_for(self, user_id: int, hotel_id: int) -> str | None:
        """The user's role at this hotel, or None if they are not a member.

        Read on every hotel-scoped request, which is why it selects one column against
        ``uq_user_hotels_user_id_hotel_id`` rather than loading the row.
        """
        return self._session.scalars(
            select(UserHotel.role).where(
                UserHotel.user_id == user_id, UserHotel.hotel_id == hotel_id
            )
        ).one_or_none()

    def hotel_ids_for(self, user_id: int) -> list[int]:
        """Every hotel this user belongs to.

        Backs the membership filter on ``GET /hotels``: a listing must not reveal that
        properties the caller has nothing to do with exist at all.
        """
        return list(
            self._session.scalars(
                select(UserHotel.hotel_id).where(UserHotel.user_id == user_id)
            ).all()
        )

    def count_for_user(self, user_id: int) -> int:
        """Total memberships, for the pagination envelope on the filtered hotel listing."""
        return len(self.hotel_ids_for(user_id))

    def hotels_page_for_user(self, user_id: int, *, limit: int, offset: int) -> list[Hotel]:
        """One page of the hotels this user belongs to, ordered as the unfiltered listing is.

        A join rather than an ``IN`` over a Python list: membership counts are unbounded in
        principle, and shipping every id to the client of the database and back is the shape
        that stops working at scale.
        """
        return list(
            self._session.scalars(
                select(Hotel)
                .join(UserHotel, UserHotel.hotel_id == Hotel.id)
                .where(UserHotel.user_id == user_id)
                .order_by(Hotel.name.asc(), Hotel.id.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    # --- Stage 4.4: administering the members of one hotel -------------------------------

    def count_members(self, hotel_id: int) -> int:
        """How many people have access to this hotel, for the pagination envelope."""
        return int(
            self._session.scalar(
                select(func.count()).select_from(UserHotel).where(UserHotel.hotel_id == hotel_id)
            )
            or 0
        )

    def members_page(
        self, hotel_id: int, *, limit: int, offset: int
    ) -> list[tuple[UserHotel, User]]:
        """One page of this hotel's memberships, each with the account it belongs to.

        Joined in one statement rather than loading memberships and then resolving each
        user: the listing is small but the N+1 shape is the one this project has already had
        to fix once, and it is cheaper to not write it than to find it later.

        Ordered by email so the page is stable across identical requests; ``user_id`` breaks
        ties that cannot occur today (``uq_users_email``) but would if that ever relaxed.
        """
        rows = self._session.execute(
            select(UserHotel, User)
            .join(User, User.id == UserHotel.user_id)
            .where(UserHotel.hotel_id == hotel_id)
            .order_by(User.email.asc(), UserHotel.user_id.asc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [(membership, user) for membership, user in rows]

    def get_membership(self, user_id: int, hotel_id: int) -> UserHotel | None:
        """The membership row itself, for a change that needs to modify or delete it.

        ``role_for`` returns only the role and is what the per-request authorization check
        uses; this returns the row, and is used only by the administration paths.
        """
        return self._session.scalars(
            select(UserHotel).where(UserHotel.user_id == user_id, UserHotel.hotel_id == hotel_id)
        ).one_or_none()

    def lock_owner_ids(self, hotel_id: int) -> list[int]:
        """Every owner of this hotel, with the rows LOCKED until the transaction ends.

        This is what makes the "a hotel always has an owner" rule hold under concurrency.
        Two requests each demoting a different one of two owners would otherwise both read
        "there are 2 owners", both decide they are safe, and commit a hotel with none.

        ``FOR UPDATE`` over the owner rows serialises exactly those requests: both need the
        same rows, so the second waits and re-reads a count that now reflects the first.
        Adding an owner concurrently is not a hazard -- it can only raise the count -- so no
        wider lock, and no serialisable isolation level, is needed.
        """
        return list(
            self._session.scalars(
                select(UserHotel.user_id)
                .where(
                    UserHotel.hotel_id == hotel_id,
                    UserHotel.role == HotelRole.OWNER.value,
                )
                .with_for_update()
            ).all()
        )

    def set_role(self, membership: UserHotel, role: HotelRole) -> UserHotel:
        """Change a membership's role and flush. The service owns the commit."""
        membership.role = role.value
        self._session.flush()
        self._session.refresh(membership)
        return membership

    def delete(self, membership: UserHotel) -> None:
        """Remove one membership and flush. The service owns the commit."""
        self._session.delete(membership)
        self._session.flush()


__all__ = ["MembershipRepository"]
