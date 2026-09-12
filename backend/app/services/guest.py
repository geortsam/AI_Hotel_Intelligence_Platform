"""Guest business logic and unit-of-work boundaries.

Owns the transaction (``get_db`` does not commit) and owns the scoping rule: **a guest is
reached only through their own hotel.** Every public method resolves ``hotel_public_id``
first, so a guest of hotel A is not addressable through hotel B.

**PII handling is what makes this service differ from its siblings.**

Two departures from the pattern established in earlier stages, both deliberate:

1. *No SELECT-then-INSERT on email.* The schema carries
   ``uq_guests_hotel_id_email (hotel_id, email) WHERE email IS NOT NULL``, and that partial
   unique index is the sole authority. A pre-check would be both racy and redundant.

2. *No ``exc_info`` on integrity errors.* PostgreSQL renders a unique violation as
   ``DETAIL: Key (hotel_id, email)=(1, someone@example.com) already exists.`` Logging the
   exception would therefore write a guest's email address into application logs. This
   service logs the SQLSTATE and the constraint *name* only, and the message it returns to
   the client never echoes the value either.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    GENERIC_CONFLICT_MESSAGE,
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_DEPENDENCY_VIOLATIONS,
    SQLSTATE_NOT_NULL_VIOLATION,
    SQLSTATE_UNIQUE_VIOLATION,
    ConflictError,
    NotFoundError,
    constraint_name_of,
    sqlstate_of,
)
from app.models.guest import Guest
from app.models.hotel import Hotel
from app.repositories.guest import GuestRepository
from app.schemas.common import Page
from app.schemas.guest import GuestCreate, GuestResponse, GuestUpdate
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: The partial unique index on (hotel_id, email). Named so a conflict can be reported
#: precisely without quoting the offending address.
EMAIL_UNIQUE_CONSTRAINT = "uq_guests_hotel_id_email"


class GuestService:
    """Domain operations on guests, always within one hotel."""

    def __init__(
        self, session: Session, repository: GuestRepository, scope: HotelScopeResolver
    ) -> None:
        self._session = session
        self._repository = repository
        self._scope = scope

    # --- reads --------------------------------------------------------------------------

    def get(self, hotel_public_id: uuid.UUID, guest_public_id: uuid.UUID) -> GuestResponse:
        """Return one guest belonging to this hotel, or raise 404."""
        hotel = self._scope.require_hotel(hotel_public_id)
        return self._to_response(self._require_guest(hotel, guest_public_id), hotel)

    def list(self, hotel_public_id: uuid.UUID, *, page: int, page_size: int) -> Page[GuestResponse]:
        """One page of this hotel's guests, ordered by surname.

        A missing hotel is a 404 rather than an empty page: "this hotel has no guests" and
        "this hotel does not exist" are different answers.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        total = self._repository.count_for_hotel(hotel.id)
        rows = self._repository.list_page_for_hotel(
            hotel.id, limit=page_size, offset=(page - 1) * page_size
        )
        return Page.build(
            items=[self._to_response(row, hotel) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    # --- writes -------------------------------------------------------------------------

    def create(self, hotel_public_id: uuid.UUID, payload: GuestCreate) -> GuestResponse:
        """Create a guest under an existing hotel and commit.

        No email pre-check: the partial unique index is the authority, and asking first
        would be a race the database already forecloses.
        """
        hotel = self._scope.require_hotel(hotel_public_id)

        guest = Guest(hotel_id=hotel.id, **payload.model_dump())
        try:
            created = self._repository.add(guest)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._to_response(created, hotel)

    def update(
        self, hotel_public_id: uuid.UUID, guest_public_id: uuid.UUID, payload: GuestUpdate
    ) -> GuestResponse:
        """Apply a partial update and commit."""
        hotel = self._scope.require_hotel(hotel_public_id)
        guest = self._require_guest(hotel, guest_public_id)
        changes: dict[str, Any] = payload.model_dump(exclude_unset=True)

        if not changes:
            return self._to_response(guest, hotel)

        try:
            updated = self._repository.apply_changes(guest, changes)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc

        return self._to_response(updated, hotel)

    def delete(self, hotel_public_id: uuid.UUID, guest_public_id: uuid.UUID) -> None:
        """Delete a guest and commit, honouring the database's delete policies.

        No cascade is invented. Two tables reference guests, and BOTH block the delete --
        though for different reasons, and only one of them obviously:

        * ``bookings`` is ``ON DELETE RESTRICT`` -> 23503/23001, the expected refusal.

        * ``reviews`` is ``ON DELETE SET NULL`` over the COMPOSITE ``(guest_id, hotel_id)``.
          PostgreSQL nulls *every* column of the key, but ``reviews.hotel_id`` is NOT NULL,
          so the policy cannot fire and the delete fails with 23502 on that column instead.
          The declared SET NULL is therefore unreachable in this schema: a guest with
          reviews cannot be deleted either.

        Verified against PostgreSQL 18.6 rather than inferred from the declaration. Both
        outcomes stay the database's decision; this method only reports them accurately.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        guest = self._require_guest(hotel, guest_public_id)
        try:
            self._repository.delete(guest)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            state = sqlstate_of(exc)
            # 23502 counts as a dependency signal HERE and nowhere else: during a delete it
            # can only mean a referencing row could not be detached.
            if state in SQLSTATE_DEPENDENCY_VIOLATIONS or state == SQLSTATE_NOT_NULL_VIOLATION:
                raise ConflictError(
                    "This guest cannot be deleted because existing reservations or reviews "
                    "still reference them. Remove or reassign those records first."
                ) from exc
            raise self._translate(exc) from exc

    # --- internals ----------------------------------------------------------------------

    def _require_guest(self, hotel: Hotel, guest_public_id: uuid.UUID) -> Guest:
        """Resolve a guest **within this hotel**, or raise 404.

        The message names no guest and echoes no identifier beyond what the caller already
        sent, so a 404 cannot be used to confirm that a person exists at another property.
        """
        guest = self._repository.get_by_hotel_and_public_id(hotel.id, guest_public_id)
        if guest is None:
            raise NotFoundError("Guest not found for this hotel.")
        return guest

    @staticmethod
    def _to_response(guest: Guest, hotel: Hotel) -> GuestResponse:
        """Build the response, attaching the parent's public identifier."""
        return GuestResponse.model_validate(
            {
                "hotel_public_id": hotel.public_id,
                "public_id": guest.public_id,
                "first_name": guest.first_name,
                "last_name": guest.last_name,
                "email": guest.email,
                "phone": guest.phone,
                "country_code": guest.country_code,
                "preferred_language": guest.preferred_language,
                "date_of_birth": guest.date_of_birth,
                "marketing_opt_in": guest.marketing_opt_in,
                "notes": guest.notes,
                "created_at": guest.created_at,
                "updated_at": guest.updated_at,
            }
        )

    def _translate(self, exc: IntegrityError) -> Exception:
        """Turn a database integrity error into a domain error, leaking neither internals
        nor personal data.

        Note the absence of ``exc_info``: the driver's text contains the offending column
        values, and for this table those are a real person's details.
        """
        state = sqlstate_of(exc)
        constraint = constraint_name_of(exc)
        logger.warning("Guest integrity error (sqlstate=%s, constraint=%s)", state, constraint)

        if state == SQLSTATE_UNIQUE_VIOLATION:
            if constraint == EMAIL_UNIQUE_CONSTRAINT:
                # The address is deliberately not quoted back: it is personal data, and the
                # client already knows what it sent.
                return ConflictError("Another guest at this hotel already uses that email address.")
            return ConflictError("That value is already taken by another guest.")
        if state == SQLSTATE_CHECK_VIOLATION:
            return ConflictError("The supplied values violate a guest constraint.")
        if state in SQLSTATE_DEPENDENCY_VIOLATIONS:
            return ConflictError("This guest is still referenced by other records.")
        return ConflictError(GENERIC_CONFLICT_MESSAGE)


__all__ = ["DEFAULT_PAGE_SIZE", "EMAIL_UNIQUE_CONSTRAINT", "MAX_PAGE_SIZE", "GuestService"]
